"""REST API v1 — the /time endpoints (billing module).

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Driven through the real Flask test client with a real token, because the parts
worth testing here are the ones that live *between* the view and the service:
that the team axis is honoured (a token cannot read a team it is not in), that
the merge is closed to non-managers and to read-only tokens, and that a project
list is bounded by what its caller may see. Getting any of those wrong exposes
another team's books to a token that should never have seen them.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class TimeApiTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()  # forces a real round trip
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")
        cls.ctx = cls.app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()
        disconnect(alias="default")

    def setUp(self):
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        self.Customer, self.Project, self.Bucket = (
            Customer, TimeTrackingProject, TimeBucket)
        self.Team, self.User = Team, User
        self._wipe()
        self.client = self.app.test_client()

        self.boss = self._user("boss")
        self.mate = self._user("mate", level="premium")
        self.outsider = self._user("outsider")
        self.team = Team(name="Kuhn & Rueß", admin=self.boss,
                         admins=[self.boss]).save()
        # Membership is a reference on the USER, not a list on the team
        # (services.permissions.participating_teams).
        self.mate.team = self.team
        self.mate.save()
        self.other_team = Team(name="Somebody else", admin=self.outsider,
                               admins=[self.outsider]).save()

        _, self.boss_token = self.boss.issue_api_token("test")
        _, self.mate_token = self.mate.issue_api_token("test")
        _, self.ro_token = self.boss.issue_api_token("ro", read_only=True)
        _, self.outsider_token = self.outsider.issue_api_token("test")

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        for doc in (self.Bucket, self.Project, self.Customer,
                    self.Team, self.User):
            doc.objects.delete()

    # -- helpers ---------------------------------------------------------

    def _user(self, name, level="team_admin"):
        return self.User(username=name, email=f"{name}@test.invalid",
                         password_hash="x", level=level).save()

    def _customer(self, name, team=None, **kw):
        return self.Customer(team=team or self.team, name=name, **kw).save()

    def _project(self, customer, name, **kw):
        return self.Project(team=customer.team, customer=customer,
                            name=name, **kw).save()

    def _log(self, project, period, minutes, **kw):
        from application.models.timetracking import TimeEntry
        from application.services.timetracking import _append_entries
        _append_entries(project.team, project.customer, period, [
            TimeEntry(project_id=str(project.id), project_name=project.name,
                      minutes=minutes, **kw)])

    def _get(self, path, token=None, **kw):
        return self.client.get(path, headers={
            "Authorization": f"Bearer {token or self.boss_token}"}, **kw)

    def _post(self, path, token=None, json=None):
        return self.client.post(path, json=json or {}, headers={
            "Authorization": f"Bearer {token or self.boss_token}"})

    # -- auth / scoping --------------------------------------------------

    def test_a_token_is_required(self):
        self.assertEqual(self.client.get("/api/v1/time/customers").status_code, 401)

    def test_teams_lists_only_the_callers_own(self):
        r = self._get("/api/v1/time/teams")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([t["name"] for t in r.json["items"]], ["Kuhn & Rueß"])
        self.assertEqual(r.json["default_team_id"], str(self.team.pk))

    def test_another_teams_books_are_not_reachable(self):
        """The team id is caller-supplied, so this is the whole boundary."""
        self._customer("Theirs", team=self.other_team)
        r = self._get(f"/api/v1/time/customers?team={self.other_team.pk}")
        self.assertEqual(r.status_code, 404)

    def test_a_user_without_a_team_gets_403_not_an_empty_list(self):
        loner = self._user("loner")
        _, tok = loner.issue_api_token("test")
        r = self._get("/api/v1/time/customers", token=tok)
        self.assertEqual(r.status_code, 403)

    # -- customers -------------------------------------------------------

    def test_customers_list_filters_and_hides_archived(self):
        self._customer("Quartz Healthcare")
        self._customer("Quartz Healthcare Germany")
        self._customer("Old Client", archived=True)
        names = [c["name"] for c in self._get("/api/v1/time/customers").json["items"]]
        self.assertEqual(names, ["Quartz Healthcare", "Quartz Healthcare Germany"])
        self.assertIn("Old Client", [c["name"] for c in self._get(
            "/api/v1/time/customers?include_archived=1").json["items"]])
        self.assertEqual([c["name"] for c in self._get(
            "/api/v1/time/customers?q=germany").json["items"]],
            ["Quartz Healthcare Germany"])

    def test_a_customer_reports_every_key_it_answers_to(self):
        c = self._customer("Quartz Healthcare", external_id="mite:41",
                           external_ids=["zammad:org:7"])
        body = self._get(f"/api/v1/time/customers/{c.pk}").json
        self.assertEqual(body["external_id"], "mite:41")
        self.assertEqual(body["external_ids"], ["zammad:org:7"])

    def test_unknown_customer_is_404(self):
        from bson import ObjectId
        self.assertEqual(
            self._get(f"/api/v1/time/customers/{ObjectId()}").status_code, 404)
        # A malformed id must be an ordinary 404, not a 500.
        self.assertEqual(
            self._get("/api/v1/time/customers/not-an-id").status_code, 404)

    # -- duplicates ------------------------------------------------------

    def test_duplicates_groups_by_name_heaviest_first(self):
        keep = self._customer("Quartz Healthcare", external_id="mite:41")
        dupe = self._customer("quartz healthcare", external_id="zammad:org:7")
        self._customer("Quartz Healthcare Germany")   # a different client
        self._log(self._project(keep, "Default Projekt"), "2026-08-05", 60)
        self._project(dupe, "Quartz Healthcare", intake=True)

        body = self._get("/api/v1/time/customers/duplicates").json

        self.assertEqual(body["count"], 1)
        group = body["items"][0]
        self.assertEqual([c["id"] for c in group["customers"]],
                         [str(keep.pk), str(dupe.pk)])   # history first
        self.assertEqual(group["customers"][0]["minutes"], 60)
        self.assertEqual(group["customers"][1]["projects"][0]["intake"], True)

    def test_no_duplicates_is_an_empty_list(self):
        self._customer("Quartz Healthcare")
        self.assertEqual(self._get(
            "/api/v1/time/customers/duplicates").json["items"], [])

    # -- merge -----------------------------------------------------------

    def test_merge_moves_the_hours_and_reports_what_moved(self):
        keep = self._customer("Quartz Healthcare", external_id="mite:41")
        dupe = self._customer("Quartz Healthcare", external_id="zammad:org:7")
        self._log(self._project(keep, "Default Projekt"), "2026-08-05", 60)
        self._log(self._project(dupe, "Beratung"), "2026-08-05", 90)

        r = self._post(f"/api/v1/time/customers/{dupe.pk}/merge",
                       json={"into": str(keep.pk)})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["stats"]["entries_moved"], 1)
        self.assertEqual(r.json["into"]["external_ids"], ["zammad:org:7"])
        self.assertIsNone(self.Customer.objects(pk=dupe.pk).first())
        self.assertEqual(sum(b.total_minutes for b in
                             self.Bucket.objects(team=self.team)), 150)

    def test_merge_needs_an_explicit_target(self):
        c = self._customer("Acme")
        r = self._post(f"/api/v1/time/customers/{c.pk}/merge")
        self.assertEqual(r.status_code, 400)
        self.assertIn("into", r.json["error"])

    def test_merge_into_itself_is_refused(self):
        c = self._customer("Acme")
        r = self._post(f"/api/v1/time/customers/{c.pk}/merge",
                       json={"into": str(c.pk)})
        self.assertEqual(r.status_code, 400)

    def test_merge_is_closed_to_non_managers(self):
        keep, dupe = self._customer("Acme"), self._customer("Acme GmbH")
        r = self._post(f"/api/v1/time/customers/{dupe.pk}/merge",
                       token=self.mate_token, json={"into": str(keep.pk)})
        self.assertEqual(r.status_code, 403)
        self.assertIsNotNone(self.Customer.objects(pk=dupe.pk).first())

    def test_merge_is_closed_to_read_only_tokens(self):
        """The token check fires before the endpoint, so a read-only token is
        refused even though its owner manages the team."""
        keep, dupe = self._customer("Acme"), self._customer("Acme GmbH")
        r = self._post(f"/api/v1/time/customers/{dupe.pk}/merge",
                       token=self.ro_token, json={"into": str(keep.pk)})
        self.assertEqual(r.status_code, 403)
        self.assertIsNotNone(self.Customer.objects(pk=dupe.pk).first())

    def test_merge_cannot_reach_across_teams(self):
        mine = self._customer("Acme")
        theirs = self._customer("Acme", team=self.other_team)
        r = self._post(f"/api/v1/time/customers/{mine.pk}/merge",
                       json={"into": str(theirs.pk)})
        self.assertEqual(r.status_code, 404)
        self.assertIsNotNone(self.Customer.objects(pk=theirs.pk).first())

    # -- projects / entries ----------------------------------------------

    def test_projects_are_team_wide_like_the_management_page(self):
        """Structure is visible to every member — ``views.timetracking.index``
        shows any member the whole customer/project list, so bounding this by
        the narrower billing scope would have the API and the UI disagree."""
        c = self._customer("Acme")
        self._project(c, "Beratung")
        self.assertEqual(len(self._get("/api/v1/time/projects").json["items"]), 1)
        self.assertEqual(len(self._get("/api/v1/time/projects",
                                       token=self.mate_token).json["items"]), 1)

    def test_logged_time_is_bounded_by_the_billing_scope(self):
        """The other half of the rule: a plain member owns no containers, so no
        project is linked to them and the report scope is empty."""
        from datetime import datetime, timezone
        c = self._customer("Acme")
        p = self._project(c, "Beratung")
        self._log(p, "2026-08-05", 60,
                  spent_on=datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc))
        url = "/api/v1/time/entries?from=2026-08-01&to=2026-08-31"
        self.assertEqual(self._get(url).json["count"], 1)
        self.assertEqual(self._get(url, token=self.mate_token).json["count"], 0)

    def test_projects_can_be_narrowed_to_one_customer(self):
        a, b = self._customer("Acme"), self._customer("Beta")
        self._project(a, "One")
        self._project(b, "Two")
        items = self._get(
            f"/api/v1/time/projects?customer_id={a.pk}").json["items"]
        self.assertEqual([p["name"] for p in items], ["One"])

    def test_entries_honour_the_range_and_total_only_active_time(self):
        from datetime import datetime, timezone
        c = self._customer("Acme")
        p = self._project(c, "Beratung", hourly_rate_cents=9500)
        when = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)
        self._log(p, "2026-08-05", 60, spent_on=when, note="in range")
        self._log(p, "2026-08-05", 30, spent_on=when, note="cancelled",
                  status="cancelled")
        self._log(p, "2026-09-02", 45,
                  spent_on=datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc))

        body = self._get("/api/v1/time/entries"
                         "?from=2026-08-01&to=2026-08-31").json

        self.assertEqual(body["from"], "2026-08-01")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["total_minutes"], 60)
        self.assertEqual(body["items"][0]["note"], "in range")
        self.assertEqual(body["items"][0]["customer"], "Acme")

    def test_cancelled_time_stays_findable(self):
        from datetime import datetime, timezone
        c = self._customer("Acme")
        p = self._project(c, "Beratung")
        when = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)
        self._log(p, "2026-08-05", 30, spent_on=when, status="cancelled")
        body = self._get("/api/v1/time/entries"
                         "?from=2026-08-01&to=2026-08-31&state=cancelled").json
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"][0]["status"], "cancelled")

    # -- docs ------------------------------------------------------------

    def test_every_time_route_is_in_the_published_spec(self):
        """The spec is served publicly; a route missing from it is a route
        nobody can discover."""
        spec = self.client.get("/api/v1/openapi.json").json
        documented = {p.replace("{customer_id}", "<cid>")
                      for p in spec["paths"] if p.startswith("/time")}
        registered = {r.rule.replace("/api/v1", "")
                      for r in self.app.url_map.iter_rules()
                      if r.rule.startswith("/api/v1/time")}
        self.assertEqual(documented, registered)


if __name__ == "__main__":
    unittest.main()
