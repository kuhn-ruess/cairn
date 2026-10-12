"""A task whose ownership I handed to somebody else leaves my Today.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

The report: a task I was the owner of, and no longer am, was still in my Today.

Handing the ownership away keeps the old owner in ``members`` on purpose, so
they don't lose sight of the task (``_apply_form``). The Today list itself is
scoped to owner OR assignee and was fine. The Plan side of the page was not:
the timeline, the Next rail, carry-over and "Plan my day" all ran on the wide
scope (which includes ``members``) and then let ``assigned_to=None`` through as
"mine". A task now owned by somebody else and assigned to nobody matched both,
and stayed on the day of the person who gave it away.
"""
import unittest
from datetime import datetime, timedelta, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class OwnerHandedAwayTests(unittest.TestCase):

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
            cls._wipe()
        disconnect(alias="default")

    @staticmethod
    def _wipe():
        from application.models.item import Item
        from application.models.user import User
        Item.all_objects.delete()
        User.objects.delete()

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        now = datetime.now(timezone.utc)
        with self.app.app_context():
            self._wipe()
            self.me = User(username="oh-me", email="oh-me@test.invalid",
                           password_hash="x", level="premium").save()
            self.other = User(username="oh-other", email="oh-other@test.invalid",
                              password_hash="x", level="premium").save()
            self.project = Item(owner=self.me, kind="project", title="Website",
                                project_type="parallel", status="active").save()
            # Everything that puts a task on the day: NEXT, planned for today,
            # and one planned yesterday so carry-over would pick it up.
            self.handed = Item(owner=self.me, kind="task", status="next",
                               title="Weggegebene Aufgabe", parent=self.project,
                               plan_date=now).save()
            self.stale = Item(owner=self.me, kind="task", status="open",
                              title="Gestern liegen geblieben", parent=self.project,
                              plan_date=now - timedelta(days=1)).save()
            # Control: my own unassigned work must stay.
            self.mine = Item(owner=self.me, kind="task", status="next",
                             title="Meine Aufgabe", parent=self.project,
                             plan_date=now).save()
            # Assigned to the other person first: the current assignee is always
            # a valid owner, so the real form path works without a shared team.
            for t in (self.handed, self.stale):
                t.assign_to(self.other, by_user=self.me)
                t.save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.me.pk)
            sess["_fresh"] = True
        for t in (self.handed, self.stale):
            self._hand_over(t)
        # The inspector POST above carries empty date fields; put the days
        # back so the task really is "on my day" when the views are asked.
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.handed.pk).update(set__plan_date=now)
            Item.objects(pk=self.stale.pk).update(
                set__plan_date=now - timedelta(days=1))

    def _hand_over(self, task):
        """Make ``other`` the owner through the inspector, like the UI does."""
        resp = self.client.post(f"/tasks/{task.pk}/edit", data={
            "title": task.title, "note": "", "project": str(self.project.pk),
            "status": task.status, "priority": "", "defer_date": "",
            "due_date": "", "plan_date": "", "start_date": "", "end_date": "",
            "estimated_minutes": "", "location": "", "waiting_on": "",
            "followup_at": "", "followup_every_days": "",
            "review_interval_days": "", "repeat_every_days": "",
            "repeat_unit": "day", "repeat_monthday": "",
            "assigned_to": str(self.other.pk), "owner": str(self.other.pk),
        })
        self.assertIn(resp.status_code, (200, 204, 302))

    def _reload(self, task):
        from application.models.item import Item
        with self.app.app_context():
            return Item.objects(pk=task.pk).first()

    def test_the_hand_over_leaves_exactly_the_state_of_the_report(self):
        """Guard against a false green: owned by the other person, assigned to
        nobody, and I stay a member (kept on purpose, not cleaned up)."""
        t = self._reload(self.handed)
        self.assertEqual(t.owner.pk, self.other.pk)
        self.assertIsNone(t.assigned_to)
        self.assertIn(self.me.pk, [m.pk for m in t.members])
        self.assertTrue(t.plan_date)

    def test_not_in_the_next_rail(self):
        from application.models.user import User
        from application.views.tasks import _next_actions_pool
        with self.app.test_request_context():
            me = User.objects(pk=self.me.pk).first()
            pool, delegated = _next_actions_pool(me, datetime.now(timezone.utc), set())
        titles = {p["title"] for p in pool}
        self.assertNotIn("Weggegebene Aufgabe", titles)
        self.assertIn("Meine Aufgabe", titles)
        self.assertEqual(delegated, 0)

    def test_not_on_the_plan_timeline(self):
        from application.models.user import User
        from application.services.tzutil import resolve_tz
        from application.views.tasks import _plan_day_tasks
        with self.app.test_request_context():
            me = User.objects(pk=self.me.pk).first()
            tz = resolve_tz(me)
            day = datetime.now(timezone.utc).astimezone(tz).date()
            titles = {t.title for t in _plan_day_tasks(me, day, tz, is_today=True)}
        self.assertNotIn("Weggegebene Aufgabe", titles)
        self.assertIn("Meine Aufgabe", titles)

    def test_carry_over_leaves_it_alone(self):
        """Not mine any more, so not mine to move onto today either."""
        from application.models.user import User
        from application.services.tzutil import resolve_tz
        from application.views.tasks import _carry_over
        before = self._reload(self.stale).plan_date
        with self.app.test_request_context():
            me = User.objects(pk=self.me.pk).first()
            tz = resolve_tz(me)
            day = datetime.now(timezone.utc).astimezone(tz).date()
            day_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=tz)
            _carry_over(me, day_start)
        self.assertEqual(self._reload(self.stale).plan_date, before)

    def test_neither_today_view_shows_it(self):
        for view in ("list", "plan"):
            html = self.client.get(f"/tasks/today?view={view}").get_data(as_text=True)
            self.assertIn("Meine Aufgabe", html, view)
            self.assertNotIn("Weggegebene Aufgabe", html, view)
            self.assertNotIn("Gestern liegen geblieben", html, view)

    def test_the_new_owner_has_it(self):
        from application.models.user import User
        from application.views.tasks import _next_actions_pool
        with self.app.test_request_context():
            other = User.objects(pk=self.other.pk).first()
            pool, _ = _next_actions_pool(other, datetime.now(timezone.utc), set())
        self.assertIn("Weggegebene Aufgabe", {p["title"] for p in pool})


if __name__ == "__main__":
    unittest.main()
