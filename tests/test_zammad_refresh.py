"""The inspector's "Refresh from Zammad" button: one ticket, pulled on demand.

Run with ``python -m unittest discover tests``. The end-to-end half needs a
MongoDB (``TestConfig`` points at ``bktodo_test``) and is skipped without one.
Nothing here ever talks to a real Zammad — ``zammad_import._request`` is
replaced by a fixture that answers the three endpoints a ticket costs.

What is pinned down, and why each of it:

* the button only exists where there IS a ticket (``Item.zammad_ticket_id``),
  because the one thing worse than no refresh button is one that 400s;
* the refresh goes through the SAME merge the sync uses, so a click and the
  background run can't end up saying different things about a ticket;
* it does **not** move ``last_zammad_sync_at``. That is the watermark the next
  incremental run reads: bumping it because somebody looked at one ticket would
  make the run skip everything else that changed meanwhile;
* the connection it uses is the TEAM's wherever there is one — the same
  connection, go-live date and billing target the ticket already came in
  through;
* and a failure says something the clicker can act on instead of a status line.
"""
import unittest

from application.services import zammad_refresh as zr
from application.services.zammad_import import ZammadError

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


TICKET = {
    "id": 4711,
    "number": "70001",
    "title": "Drucker im 2. OG klemmt",
    "state": "open",
    "owner_id": 9,
}


def _fixture_request(seen=None):
    """A stand-in for ``zammad_import._request``: one ticket, no articles,
    no tags. Records every URL it was asked for, so a test can assert the
    refresh really costs ONE ticket and not a walk of the instance."""
    def _request(url, token, *, params=None, method="GET", body=None):
        if seen is not None:
            seen.append(url)
        if "/tickets/" in url:
            return dict(TICKET)
        if "/ticket_articles/by_ticket/" in url:
            return []
        if url.endswith("/tags"):
            return {"tags": []}
        raise AssertionError(f"unexpected Zammad call: {url}")
    return _request


class TicketIdentity(unittest.TestCase):
    """``Item.zammad_ticket_id`` is what decides the button is offered."""

    def _item(self, source, external_id):
        from application.models.item import Item
        return Item(kind="task", title="t", source=source,
                    external_id=external_id)

    def test_a_zammad_task_names_its_ticket(self):
        self.assertEqual(
            self._item("zammad", "zammad:4711").zammad_ticket_id(), "4711")

    def test_another_source_has_none(self):
        self.assertEqual(
            self._item("jira", "CHMK-417").zammad_ticket_id(), "")

    def test_a_task_nobody_imported_has_none(self):
        self.assertEqual(self._item("", "").zammad_ticket_id(), "")

    def test_a_zammad_task_without_the_stamp_has_none(self):
        # external_ref alone is the human Ticket#; it doesn't address the API.
        self.assertEqual(
            self._item("zammad", "70001").zammad_ticket_id(), "")


class Explanations(unittest.TestCase):
    """A status line is not an answer. These are."""

    def test_a_gone_ticket_says_so(self):
        msg = zr.explain(ZammadError("HTTP 404 from https://z/api/v1/tickets/4711: {}"),
                         "4711")
        self.assertIn("4711", msg)
        self.assertIn("deleted", msg)

    def test_a_refused_token_points_at_the_settings_page(self):
        msg = zr.explain(ZammadError("HTTP 401 from https://z/api/v1/tickets/4711: x"),
                         "4711")
        self.assertIn("token", msg.lower())
        self.assertIn("Settings", msg)

    def test_an_unreachable_zammad_says_unreachable(self):
        msg = zr.explain(ZammadError("network error: timed out"), "4711")
        self.assertIn("Couldn't reach Zammad", msg)


@unittest.skipIf(connect is None, "mongoengine not installed")
class RefreshOneTicket(unittest.TestCase):

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

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            from application.models.item import Item
            from application.models.team import Team
            from application.models.user import User
            Item.all_objects.delete()
            Team.objects.delete()
            User.objects.delete()
        disconnect(alias="default")

    def setUp(self):
        from application.models.item import Item
        from application.models.team import Team
        from application.models.user import User
        from application.services.crypto import encrypt_for_user
        self.Item = Item
        with self.app.app_context():
            Item.all_objects.delete()
            Team.objects.delete()
            User.objects.delete()
            self.user = User(username="zrefresh", email="z@test.invalid",
                             password_hash="x", level="premium",
                             zammad_enabled=True,
                             zammad_url="https://zammad.test.invalid",
                             # No hours in these tests: the refresh under test is
                             # the ticket, and the time path has its own.
                             zammad_import_time=False).save()
            self.user.zammad_token = encrypt_for_user(self.user, "tok-123")
            self.user.save()
            self.task = Item(owner=self.user, kind="task",
                             title="Drucker klemmt (alter Titel)",
                             source="zammad", external_id="zammad:4711",
                             external_ref="70001", status="open").save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _patch_zammad(self, seen=None, request=None):
        from application.services import zammad_import as zi
        original = zi._request
        zi._request = request or _fixture_request(seen)
        self.addCleanup(lambda: setattr(zi, "_request", original))

    # -- the happy path ---------------------------------------------------

    def test_the_inspector_offers_the_button_on_a_zammad_task(self):
        html = self.client.get(
            f"/tasks/{self.task.pk}/edit?inspector=1").get_data(as_text=True)
        self.assertIn("data-zammad-refresh", html,
                      "the inspector no longer offers the Zammad refresh")
        self.assertIn(f"/tasks/{self.task.pk}", html)

    def test_the_button_reopens_the_inspector_fragment_not_the_page(self):
        # After a refresh the panel is re-fetched from this URL. The bare
        # /tasks/<id> is the full detail page, which landed a whole Cairn
        # inside the panel.
        html = self.client.get(
            f"/tasks/{self.task.pk}/edit?inspector=1").get_data(as_text=True)
        self.assertIn(f'data-reopen-url="/tasks/{self.task.pk}/edit"', html)

    def test_the_detail_url_answers_an_inspector_request_with_the_fragment(self):
        for kw in ({"query_string": {"inspector": "1"}},
                   {"headers": {"X-Inspector-Request": "1"}}):
            with self.subTest(**kw):
                resp = self.client.get(f"/tasks/{self.task.pk}", **kw)
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                self.assertNotIn("<html", html.lower())
                self.assertNotIn("<main", html)
                self.assertIn("inspector-form", html)
        # The plain detail page is still the page.
        html = self.client.get(f"/tasks/{self.task.pk}").get_data(as_text=True)
        self.assertIn("<main", html)

    def test_a_task_from_elsewhere_gets_no_button(self):
        with self.app.app_context():
            other = self.Item(owner=self.user, kind="task", title="hand-made",
                              external_ref="CHMK-417", source="jira").save()
        html = self.client.get(
            f"/tasks/{other.pk}/edit?inspector=1").get_data(as_text=True)
        self.assertNotIn("data-zammad-refresh", html)

    def test_the_click_takes_the_new_title_and_costs_one_ticket(self):
        seen = []
        self._patch_zammad(seen)
        resp = self.client.post(f"/tasks/{self.task.pk}/zammad-refresh")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["title"], TICKET["title"])
        self.task.reload()
        self.assertEqual(self.task.title, TICKET["title"])
        # One ticket, not a pass over the instance: ticket + articles + tags.
        self.assertTrue(any("/tickets/4711" in u for u in seen), seen)
        self.assertFalse([u for u in seen if u.rstrip("/").endswith("/tickets")],
                         f"the refresh listed the whole instance: {seen}")

    def test_a_ticket_closed_over_there_closes_the_task(self):
        closed = dict(TICKET, state="closed")

        def _request(url, token, *, params=None, method="GET", body=None):
            if "/tickets/" in url:
                return closed
            if "/ticket_articles/by_ticket/" in url:
                return []
            return {"tags": []}
        self._patch_zammad(request=_request)
        self.client.post(f"/tasks/{self.task.pk}/zammad-refresh")
        self.task.reload()
        self.assertEqual(self.task.status, "done")
        self.assertEqual(self.task.completed_via, "zammad")

    def test_it_does_not_move_the_incremental_watermark(self):
        # Moving it here would make the NEXT background run skip everything
        # that changed while this one ticket was being looked at.
        self._patch_zammad()
        self.client.post(f"/tasks/{self.task.pk}/zammad-refresh")
        self.user.reload()
        self.assertIsNone(self.user.last_zammad_sync_at)

    def test_a_second_task_is_never_created(self):
        self._patch_zammad()
        self.client.post(f"/tasks/{self.task.pk}/zammad-refresh")
        self.client.post(f"/tasks/{self.task.pk}/zammad-refresh")
        with self.app.app_context():
            self.assertEqual(
                self.Item.objects(external_id="zammad:4711").count(), 1)

    # -- the ways it can go wrong ----------------------------------------

    def test_a_task_that_never_came_from_zammad_is_refused(self):
        with self.app.app_context():
            other = self.Item(owner=self.user, kind="task", title="hand-made").save()
        resp = self.client.post(f"/tasks/{other.pk}/zammad-refresh")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Zammad", resp.get_json()["error"])

    def test_somebody_elses_task_is_not_refreshable(self):
        from application.models.user import User
        with self.app.app_context():
            stranger = User(username="stranger", email="s@test.invalid",
                            password_hash="x", level="premium").save()
        other = self.app.test_client()
        with other.session_transaction() as sess:
            sess["_user_id"] = str(stranger.pk)
            sess["_fresh"] = True
        self.assertEqual(
            other.post(f"/tasks/{self.task.pk}/zammad-refresh").status_code, 404)

    def test_no_connection_is_a_400_naming_where_to_fix_it(self):
        with self.app.app_context():
            self.user.zammad_url = ""
            self.user.zammad_token = ""
            self.user.save()
        resp = self.client.post(f"/tasks/{self.task.pk}/zammad-refresh")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Settings", resp.get_json()["error"])

    def test_a_gone_ticket_is_a_502_with_the_readable_reason(self):
        def _request(url, token, *, params=None, method="GET", body=None):
            raise ZammadError("HTTP 404 from " + url + ": {}")
        self._patch_zammad(request=_request)
        resp = self.client.post(f"/tasks/{self.task.pk}/zammad-refresh")
        self.assertEqual(resp.status_code, 502)
        self.assertIn("4711", resp.get_json()["error"])
        # And the task is untouched — a failed refresh changes nothing here.
        self.task.reload()
        self.assertEqual(self.task.title, "Drucker klemmt (alter Titel)")

    # -- which connection speaks for the task -----------------------------

    def test_the_team_connection_wins_where_there_is_one(self):
        from application.models.team import Team
        from application.services.crypto import encrypt_for
        with self.app.app_context():
            team = Team(name="Kunden GmbH", admin=self.user,
                        zammad_url="https://team.zammad.test.invalid").save()
            team.zammad_token = encrypt_for(team, "team-token")
            team.zammad_import_time = False
            team.save()
            self.user.tt_default_team = str(team.pk)
            self.user.save()
            item = self.Item.objects(pk=self.task.pk).first()
            base, token, opts, via = zr.connection_for(item)
        self.assertEqual(via, "team")
        self.assertEqual(base, "https://team.zammad.test.invalid/api/v1")
        self.assertEqual(token, "team-token")
        self.assertIsNotNone(opts.team)

    def test_without_a_team_connection_the_owner_s_own_is_used(self):
        with self.app.app_context():
            item = self.Item.objects(pk=self.task.pk).first()
            base, token, opts, via = zr.connection_for(item)
        self.assertEqual(via, "user")
        self.assertEqual(base, "https://zammad.test.invalid/api/v1")
        self.assertEqual(token, "tok-123")
        self.assertIsNone(opts.team)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
