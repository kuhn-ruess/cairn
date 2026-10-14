"""The reporting right: who reads whose time, and which menu entries show.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

A person without the reporting right (not a manager, not a biller) saw the
whole module bar, People and Unassigned included, and a report of everybody's
time on the projects below their own containers. The report stays theirs, but
holds only the time they logged themselves, bounded on the server: a hand-made
``?user=`` or ``user_id`` cannot reach a colleague's entries. People and
Unassigned are the team's book and are gone from the bar for them (and 403).
Managers and billers see exactly what they saw before.
"""
import unittest
from datetime import datetime, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover (no mongoengine installed)
    connect = None


RANGE = "period=custom&from=2026-09-01&to=2026-09-30"


@unittest.skipIf(connect is None, "mongoengine not installed")
class ReportScopeTests(unittest.TestCase):

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
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        from application.services.timetracking import add_entry
        self.docs = (TimeBucket, TimeTrackingProject, Customer, Team, Item, User)
        self._wipe()
        mk = lambda name: User(username=name, email=f"{name}@test.invalid",
                               password_hash="x", level="premium").save()
        self.boss, self.biller = mk("boss"), mk("biller")
        self.mate, self.other = mk("mate"), mk("other")
        self.team = Team(name="Example Team", admin=self.boss,
                         admins=[self.boss], billers=[self.biller]).save()
        for u in (self.biller, self.mate, self.other):
            u.team = self.team
            u.save()
        customer = Customer(team=self.team, name="Example Customer").save()
        project = TimeTrackingProject(team=self.team, customer=customer,
                                      name="Support").save()
        # The plain member owns the container the billing project is linked
        # to: under the old subtree scope that was enough to read the
        # colleague's time on it.
        Item(kind="project", title="Support work", owner=self.mate,
             time_project=project).save()
        when = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
        add_entry(project=project, user=self.mate, minutes=30, spent_on=when,
                  note="mine-entry")
        add_entry(project=project, user=self.other, minutes=45, spent_on=when,
                  note="colleague-entry")

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        for doc in self.docs:
            doc.objects.delete()

    def _client(self, user):
        # The class pushes one app context and every request reuses it, so
        # Flask-Login's per-context user cache would outlive the request and
        # answer the next one as whoever asked first.
        from flask import g
        g.pop("_login_user", None)
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.pk)
            sess["_fresh"] = True
        return client

    def _report(self, user, extra=""):
        url = f"/time/report?team={self.team.pk}&{RANGE}{extra}"
        r = self._client(user).get(url)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def _api(self, user, extra=""):
        _, token = user.issue_api_token("test")
        r = self.app.test_client().get(
            f"/api/v1/time/entries?team={self.team.pk}"
            f"&from=2026-09-01&to=2026-09-30{extra}",
            headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 200)
        return r.json

    # -- the service -----------------------------------------------------

    def test_the_right_is_the_billers_and_the_managers(self):
        from application.services.timetracking import can_report, report_bounds
        self.assertTrue(can_report(self.boss, self.team))
        self.assertTrue(can_report(self.biller, self.team))
        self.assertFalse(can_report(self.mate, self.team))
        self.assertEqual(report_bounds(self.mate, self.team),
                         (None, str(self.mate.pk)))
        self.assertIsNone(report_bounds(self.biller, self.team)[1])

    # -- the web report --------------------------------------------------

    def test_without_the_right_the_report_holds_only_own_time(self):
        html = self._report(self.mate)
        self.assertIn("mine-entry", html)
        self.assertNotIn("colleague-entry", html)

    def test_a_user_param_cannot_reach_a_colleague(self):
        html = self._report(self.mate, f"&user={self.other.pk}")
        self.assertNotIn("colleague-entry", html)
        self.assertIn("mine-entry", html)
        self.assertNotIn("colleague-entry",
                         self._report(self.mate, "&user=none"))

    def test_the_csv_export_is_bounded_the_same_way(self):
        r = self._client(self.mate).get(
            f"/time/report.csv?team={self.team.pk}&{RANGE}&user={self.other.pk}")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("mine-entry", body)
        self.assertNotIn("colleague-entry", body)

    def test_with_the_right_the_report_is_unchanged(self):
        for user in (self.boss, self.biller):
            html = self._report(user)
            self.assertIn("mine-entry", html)
            self.assertIn("colleague-entry", html)
            only = self._report(user, f"&user={self.other.pk}")
            self.assertIn("colleague-entry", only)
            self.assertNotIn("mine-entry", only)

    def test_the_user_picker_shows_only_with_the_right(self):
        self.assertNotIn('name="user" class="form-select',
                         self._report(self.mate))
        self.assertIn('name="user" class="form-select',
                      self._report(self.biller))

    # -- the API ---------------------------------------------------------

    def test_the_api_follows_the_same_bounds(self):
        mine = self._api(self.mate)
        self.assertEqual([i["note"] for i in mine["items"]], ["mine-entry"])
        sneaky = self._api(self.mate, f"&user_id={self.other.pk}")
        self.assertEqual([i["note"] for i in sneaky["items"]], ["mine-entry"])
        everyone = self._api(self.biller)
        self.assertEqual(sorted(i["note"] for i in everyone["items"]),
                         ["colleague-entry", "mine-entry"])

    # -- the menu --------------------------------------------------------

    def _nav_links(self, user, page="/time/report"):
        html = self._client(user).get(
            f"{page}?team={self.team.pk}").get_data(as_text=True)
        return {name: (f'href="/time/{path}' in html)
                for name, path in (("people", "people"),
                                   ("unassigned", "unassigned"),
                                   ("report", "report"),
                                   ("trash", "trash"),
                                   ("manage", "?team="),
                                   ("import", "import"))}

    def test_without_the_right_the_bar_holds_only_what_one_has(self):
        for page in ("/time/report", "/time/day", "/time/trash", "/time/"):
            links = self._nav_links(self.mate, page)
            self.assertFalse(links["people"], page)
            self.assertFalse(links["unassigned"], page)
            self.assertFalse(links["import"], page)
            self.assertTrue(links["report"], page)
            self.assertTrue(links["trash"], page)

    def test_with_the_right_the_bar_is_complete(self):
        for user in (self.boss, self.biller):
            for page in ("/time/report", "/time/day"):
                links = self._nav_links(user, page)
                self.assertTrue(links["people"], (user.username, page))
                self.assertTrue(links["unassigned"], (user.username, page))
                self.assertTrue(links["report"], (user.username, page))

    def test_the_hidden_pages_refuse_without_the_right(self):
        client = self._client(self.mate)
        for path in ("people", "unassigned"):
            self.assertEqual(
                client.get(f"/time/{path}?team={self.team.pk}").status_code,
                403, path)
        biller = self._client(self.biller)
        for path in ("people", "unassigned"):
            self.assertEqual(
                biller.get(f"/time/{path}?team={self.team.pk}").status_code,
                200, path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
