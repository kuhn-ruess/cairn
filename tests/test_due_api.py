"""REST API v1 — ``/due`` and the ``mine`` filter on ``/tasks``.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Both things under test are about what a list may NOT contain. ``/tasks`` is
deliberately wide — it also serves what is merely visible to you, every task in
a project somebody shared — and that is what made the CLI list other people's
work as if it were yours. ``mine=true`` is the narrowing, ``/due`` is born
narrow, and the day it is read against has to be the user's day, not UTC's.
"""
import unittest
from datetime import datetime, timedelta, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class DueApiTests(unittest.TestCase):

    TZ = "Europe/Berlin"

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
        from flask import g
        from application.models.item import Item
        from application.models.user import User
        self.Item, self.User = Item, User
        # viewable_container_pks caches on flask.g — per request in the real
        # app, but the class-level app context above outlives every request the
        # test client makes, so a stale set would leak from test to test.
        g.pop("_viewable_container_pks", None)
        self._wipe()
        self.client = self.app.test_client()
        self.me = self._user("me")
        self.other = self._user("other")
        _, self.token = self.me.issue_api_token("test")
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        self.Item.all_objects.delete()
        self.User.objects.delete()

    # -- helpers ---------------------------------------------------------

    def _user(self, name):
        return self.User(username=name, email=f"{name}@test.invalid",
                         password_hash="x", level="premium",
                         timezone=self.TZ).save()

    def _task(self, title, owner=None, **kw):
        return self.Item(owner=owner or self.me, kind="task",
                         title=title, **kw).save()

    def _get(self, path):
        return self.client.get(path, headers={
            "Authorization": f"Bearer {self.token}"})

    def _titles(self, path):
        r = self._get(path)
        self.assertEqual(r.status_code, 200)
        body = r.json
        return [t["title"] for t in body.get("tasks", body.get("items", []))]

    def _end_of_local_day(self, days=0):
        """Local midnight boundary as UTC — what the endpoint must use."""
        from zoneinfo import ZoneInfo
        z = ZoneInfo(self.TZ)
        day = self.now.astimezone(z).date() + timedelta(days=days + 1)
        return datetime(day.year, day.month, day.day, tzinfo=z).astimezone(timezone.utc)

    # -- /due ------------------------------------------------------------

    def test_a_token_is_required(self):
        self.assertEqual(self.client.get("/api/v1/due").status_code, 401)

    def test_late_and_today_are_due_and_tomorrow_is_not(self):
        self._task("late", due_date=self.now - timedelta(days=2))
        self._task("later today", due_date=self._end_of_local_day() - timedelta(minutes=5))
        self._task("tomorrow", due_date=self._end_of_local_day() + timedelta(minutes=5))
        self._task("undated")
        self.assertEqual(self._titles("/api/v1/due"), ["late", "later today"])

    def test_the_day_that_ends_is_the_users_day(self):
        """Tomorrow 00:30 Berlin is still 22:30 UTC *today* — a UTC day end
        would hand it over as due."""
        self._task("just past midnight",
                   due_date=self._end_of_local_day() + timedelta(minutes=30))
        self.assertEqual(self._titles("/api/v1/due"), [])
        self.assertEqual(self._titles("/api/v1/due?days=1"), ["just past midnight"])

    def test_oldest_debt_first(self):
        self._task("b", due_date=self.now - timedelta(days=1))
        self._task("a", due_date=self.now - timedelta(days=9))
        self._task("c", due_date=self.now)
        self.assertEqual(self._titles("/api/v1/due"), ["a", "b", "c"])

    def test_finished_parked_and_dropped_are_not_due(self):
        past = self.now - timedelta(days=1)
        self._task("open", due_date=past)
        self._task("done", due_date=past, status="done")
        self._task("dropped", due_date=past, status="dropped")
        self._task("someday", due_date=past, status="someday")
        self.assertEqual(self._titles("/api/v1/due"), ["open"])

    def test_a_project_with_a_due_date_is_not_a_due_task(self):
        self.Item(owner=self.me, kind="project", title="Projekt",
                  due_date=self.now - timedelta(days=1)).save()
        self.assertEqual(self._titles("/api/v1/due"), [])

    def test_handed_to_me_counts_merely_shared_does_not(self):
        past = self.now - timedelta(days=1)
        shared = self.Item(owner=self.other, kind="project", title="Ihr Projekt",
                           members=[self.me]).save()
        self._task("theirs", owner=self.other, parent=shared, due_date=past)
        self._task("mine to do", owner=self.other, parent=shared,
                   assigned_to=self.me, due_date=past)
        self.assertEqual(self._titles("/api/v1/due"), ["mine to do"])

    def test_overdue_narrows_and_the_counts_say_so(self):
        self._task("late", due_date=self.now - timedelta(days=1))
        self._task("later today", due_date=self._end_of_local_day() - timedelta(minutes=5))
        body = self._get("/api/v1/due").json
        self.assertEqual((body["count"], body["overdue_count"]), (2, 1))
        self.assertEqual([t["overdue"] for t in body["tasks"]], [True, False])
        self.assertEqual(self._titles("/api/v1/due?overdue=true"), ["late"])

    def test_until_names_the_last_day(self):
        from zoneinfo import ZoneInfo
        day = (self.now.astimezone(ZoneInfo(self.TZ)).date() + timedelta(days=3))
        self._task("in three days", due_date=self._end_of_local_day(3) - timedelta(minutes=5))
        self.assertEqual(self._titles("/api/v1/due"), [])
        self.assertEqual(self._titles(f"/api/v1/due?until={day.isoformat()}"),
                         ["in three days"])

    def test_nonsense_is_a_400(self):
        for path in ("/api/v1/due?days=bald", "/api/v1/due?until=irgendwann",
                     "/api/v1/due?limit=viele"):
            self.assertEqual(self._get(path).status_code, 400, path)

    # -- /tasks?mine -----------------------------------------------------

    def test_mine_leaves_out_what_is_only_shared_with_me(self):
        shared = self.Item(owner=self.other, kind="project", title="Ihr Projekt",
                           members=[self.me]).save()
        self._task("theirs", owner=self.other, parent=shared)
        self._task("handed to me", owner=self.other, parent=shared,
                   assigned_to=self.me)
        self._task("my own")
        self.assertEqual(sorted(self._titles("/api/v1/tasks?mine=true&kind=task")),
                         ["handed to me", "my own"])

    def test_without_mine_the_list_stays_wide(self):
        """The wide scope is deliberate — a member of a shared project must be
        able to list its tasks. Narrowing is the caller's choice."""
        shared = self.Item(owner=self.other, kind="project", title="Ihr Projekt",
                           members=[self.me]).save()
        self._task("theirs", owner=self.other, parent=shared)
        self.assertIn("theirs", self._titles("/api/v1/tasks?kind=task"))

    def test_the_spec_documents_the_new_route(self):
        spec = self.client.get("/api/v1/openapi.json").json
        self.assertIn("/due", spec["paths"])
        params = {p["name"] for p in spec["paths"]["/tasks"]["get"]["parameters"]}
        self.assertIn("mine", params)


if __name__ == "__main__":
    unittest.main()
