"""The Plan view never loses a task, and an unfinished one goes back to Next.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Both halves of this file come from one report: *"replan hat gerade, 19 uhr die
tasks aus der sicht verschoben. es braucht noch einen button welchen nicht
erledigten task wieder in die next liste schiebt"*.

**Nothing vanishes.** "Re-pack" at 19:00 with working hours ending at 18:00 put
``window_start`` (= now, snapped up) past ``window_end``, so ``plan_day``
returned ``None`` for every candidate — and the repack branch then cleared each
task's ``plan_date``. A task with no plan date, no due date and no flag matches
nothing in ``_plan_day_tasks``, so it didn't even fall back into the Unplanned
rail: the whole day was simply gone from the page. It is **not** a timezone
defect (the window and ``now`` are both resolved in the user's zone); it is an
empty remaining window. Two rules fix it and are pinned here: a day that is over
is a no-op, and a task that does not fit keeps its place ON the day, at midnight
— which is "planned for today, no time yet", exactly what the Unplanned rail
shows and what carry-over writes. Drag-to-unschedule had the same hole and gets
the same answer.

**Back to Next** rides the one-click NEXT endpoint (``POST /tasks/<id>/next``)
with ``clear_plan=1`` instead of inventing a second path. It clears the plan
date, because a task parked on a day that is over is what makes it stale, and
the ordinary NEXT arrow deliberately does not send the flag.
"""
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None

BERLIN = ZoneInfo("Europe/Berlin")
DAY = datetime(2026, 9, 24, tzinfo=BERLIN).date()


def _clock(fixed):
    """A ``datetime`` stand-in whose ``now()`` is ``fixed`` (patched into the
    view module, which is the only place the planner reads the clock)."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    return Clock


@unittest.skipIf(connect is None, "mongoengine not installed")
class PlanBase(unittest.TestCase):

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
        with self.app.app_context():
            self._wipe()
            self.user = User(username="plan-test", email="plan@test.invalid",
                            password_hash="x", level="premium",
                            timezone="Europe/Berlin",
                            plan_day_start=9, plan_day_end=18).save()
            self.project = Item(owner=self.user, kind="project", title="Website",
                                project_type="parallel").save()
            # Two tasks with a time on the day, nothing else that would keep
            # them on the plate (no due date, not flagged) — which is what made
            # the old bug total.
            self.morning = Item(owner=self.user, kind="task",
                                title="Rechnung schreiben", parent=self.project,
                                plan_date=datetime(2026, 9, 24, 10, 0, tzinfo=BERLIN),
                                estimated_minutes=60).save()
            self.afternoon = Item(owner=self.user, kind="task",
                                  title="Server patchen", parent=self.project,
                                  plan_date=datetime(2026, 9, 24, 14, 0, tzinfo=BERLIN),
                                  estimated_minutes=30).save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    # -- helpers -----------------------------------------------------------

    def _reload(self, task):
        from application.models.item import Item
        with self.app.app_context():
            return Item.objects(pk=task.pk).first()

    def _plan_page(self, at):
        with mock.patch("application.views.tasks.datetime", _clock(at)):
            resp = self.client.get(
                f"/tasks/today?date={DAY.isoformat()}&view=plan")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _repack(self, at, **extra):
        data = {"date": DAY.isoformat(), "mode": "repack"}
        data.update(extra)
        with mock.patch("application.views.tasks.datetime", _clock(at)):
            resp = self.client.post("/tasks/auto-plan", data=data)
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def _rails(self, at):
        """The day's buckets as the timeline computes them: which tasks are
        timed and which sit in the Unplanned rail."""
        from application.views.tasks import _plan_classify, _plan_day_tasks
        with self.app.test_request_context(), \
                mock.patch("application.views.tasks.datetime", _clock(at)):
            tasks = _plan_day_tasks(self.user, DAY, BERLIN, is_today=True)
            cls = _plan_classify(tasks, [], BERLIN, DAY, at)
        return ({it["title"] for it in cls["timed"]},
                {it["title"] for it in cls["backlog"]})


class EveningRepackTests(PlanBase):
    """19:00, hours 9–18 — the exact report."""

    EVENING = datetime(2026, 9, 24, 19, 0, tzinfo=BERLIN)

    def test_the_day_is_over_so_nothing_is_re_packed(self):
        body = self._repack(self.EVENING)
        self.assertTrue(body["day_over"])
        self.assertEqual((body["planned"], body["skipped"]), (0, 0))
        self.assertEqual(body["window"], "09:00–18:00")

    def test_every_task_is_still_on_the_page_after_a_re_pack(self):
        """The regression itself: this page came back empty before the fix."""
        self._repack(self.EVENING)
        html = self._plan_page(self.EVENING)
        self.assertIn("Rechnung schreiben", html)
        self.assertIn("Server patchen", html)

    def test_the_times_are_left_exactly_as_they_were(self):
        self._repack(self.EVENING)
        self.assertEqual(self._reload(self.morning).plan_date.astimezone(BERLIN).hour, 10)
        self.assertEqual(self._reload(self.afternoon).plan_date.astimezone(BERLIN).hour, 14)

    def test_plan_my_day_is_the_same_no_op_in_the_evening(self):
        """The non-destructive button gets the same honest answer rather than
        "planned 0, 2 didn't fit"."""
        with mock.patch("application.views.tasks.datetime", _clock(self.EVENING)):
            resp = self.client.post("/tasks/auto-plan", data={"date": DAY.isoformat()})
        self.assertTrue(resp.get_json()["day_over"])


class NoRoomLeftTests(PlanBase):
    """17:45, hours 9–18 — a quarter of an hour left, so nothing fits, but the
    window is not empty and the re-pack does run."""

    LATE = datetime(2026, 9, 24, 17, 45, tzinfo=BERLIN)

    def test_what_does_not_fit_lands_in_unplanned_not_in_nothing(self):
        body = self._repack(self.LATE)
        self.assertEqual((body["planned"], body["skipped"]), (0, 2))
        timed, backlog = self._rails(self.LATE)
        self.assertEqual(timed, set())
        self.assertEqual(backlog, {"Rechnung schreiben", "Server patchen"})

    def test_an_unplaced_task_keeps_its_day_at_midnight(self):
        """"On this day, no time yet" — the value carry-over writes, and the
        only one the Unplanned rail can be drawn from."""
        self._repack(self.LATE)
        for task in (self.morning, self.afternoon):
            local = self._reload(task).plan_date.astimezone(BERLIN)
            self.assertEqual(local.date(), DAY)
            self.assertEqual((local.hour, local.minute), (0, 0))

    def test_they_are_still_rendered_on_the_plan_page(self):
        self._repack(self.LATE)
        html = self._plan_page(self.LATE)
        self.assertIn("Rechnung schreiben", html)
        self.assertIn("Server patchen", html)

    def test_a_task_that_still_fits_is_placed(self):
        """The guard must not turn the re-pack off: with room for the short one
        it is timed, and only the long one goes to Unplanned."""
        body = self._repack(datetime(2026, 9, 24, 17, 15, tzinfo=BERLIN))
        self.assertEqual((body["planned"], body["skipped"]), (1, 1))
        timed, backlog = self._rails(datetime(2026, 9, 24, 17, 30, tzinfo=BERLIN))
        self.assertEqual(timed, {"Server patchen"})
        self.assertEqual(backlog, {"Rechnung schreiben"})


class MiddayRepackTests(PlanBase):

    def test_a_morning_re_pack_still_packs_the_day(self):
        body = self._repack(datetime(2026, 9, 24, 9, 30, tzinfo=BERLIN))
        self.assertEqual((body["planned"], body["skipped"]), (2, 0))
        for task in (self.morning, self.afternoon):
            local = self._reload(task).plan_date.astimezone(BERLIN)
            self.assertEqual(local.date(), DAY)
            self.assertTrue(local.hour or local.minute)


class UnscheduleKeepsTheDayTests(PlanBase):
    """Dragging a card into the Unplanned rail had the same hole: the pill was
    drawn client-side and gone again on the next render."""

    def test_with_a_date_the_task_stays_on_the_day_without_a_time(self):
        resp = self.client.post(f"/tasks/{self.morning.pk}/unschedule",
                                data={"date": DAY.isoformat()})
        self.assertEqual(resp.status_code, 200)
        local = self._reload(self.morning).plan_date.astimezone(BERLIN)
        self.assertEqual((local.date(), local.hour, local.minute), (DAY, 0, 0))
        _, backlog = self._rails(datetime(2026, 9, 24, 11, 0, tzinfo=BERLIN))
        self.assertIn("Rechnung schreiben", backlog)

    def test_without_a_date_it_still_clears_the_plan_date(self):
        resp = self.client.post(f"/tasks/{self.morning.pk}/unschedule", data={})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self._reload(self.morning).plan_date)

    def test_a_nonsense_date_is_refused(self):
        resp = self.client.post(f"/tasks/{self.morning.pk}/unschedule",
                                data={"date": "irgendwann"})
        self.assertEqual(resp.status_code, 400)
        self.assertIsNotNone(self._reload(self.morning).plan_date)


class BackToNextTests(PlanBase):
    """The button: an unfinished task goes back into the next list."""

    def _back(self, task):
        return self.client.post(f"/tasks/{task.pk}/next", data={"clear_plan": "1"})

    def test_one_post_marks_it_next_and_drops_the_stale_plan_date(self):
        body = self._back(self.morning).get_json()
        self.assertTrue(body["is_next"])
        self.assertTrue(body["plan_cleared"])
        task = self._reload(self.morning)
        self.assertEqual(task.status, "next")
        self.assertIsNone(task.plan_date)

    def test_it_leaves_the_day_and_turns_up_in_next_actions(self):
        at = datetime(2026, 9, 24, 19, 0, tzinfo=BERLIN)
        self._back(self.morning)
        timed, backlog = self._rails(at)
        self.assertNotIn("Rechnung schreiben", timed | backlog)
        from application.views.tasks import _next_actions_pool
        with self.app.test_request_context():
            pool, _ = _next_actions_pool(self.user, at, set())
        self.assertIn("Rechnung schreiben", {it["title"] for it in pool})

    def test_the_duration_the_card_carried_is_not_touched(self):
        self._back(self.morning)
        self.assertEqual(self._reload(self.morning).estimated_minutes, 60)

    def test_the_plain_next_arrow_keeps_the_task_on_its_day(self):
        """Marking something next is not the same decision as giving up on
        today — only the Plan view's control sends ``clear_plan``."""
        body = self.client.post(f"/tasks/{self.morning.pk}/next",
                                data={}).get_json()
        self.assertFalse(body["plan_cleared"])
        self.assertIsNotNone(self._reload(self.morning).plan_date)

    def test_the_control_sits_on_the_timeline_card_and_the_unplanned_pill(self):
        at = datetime(2026, 9, 24, 19, 0, tzinfo=BERLIN)
        html = self._plan_page(at)
        self.assertIn("task-next-back", html)
        # ... and on a pill in the Unplanned rail too (no time on the day).
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.afternoon.pk).update_one(
                set__plan_date=datetime(2026, 9, 24, 0, 0, tzinfo=BERLIN))
        html = self._plan_page(at)
        self.assertEqual(html.count("task-next-back"), 2)

    def test_a_completed_card_has_no_back_to_next_button(self):
        """Nothing to put back: a done task keeps its slot, struck through."""
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.afternoon.pk).update_one(set__status="done")
            Item.objects(pk=self.morning.pk).update_one(set__status="done")
        html = self._plan_page(datetime(2026, 9, 24, 19, 0, tzinfo=BERLIN))
        self.assertNotIn("task-next-back", html)


class DelegatedInNextTests(PlanBase):
    """*"eine option delegierte task anzuzeigen ist die loesung, man muss aber
    sehen mit einem badge das der task delegiert ist"* — the Next rail hides a
    task you handed to somebody else, the Next page doesn't; a switch on the
    rail brings it back, badged with who has it."""

    def setUp(self):
        super().setUp()
        from application.models.item import Item
        from application.models.user import User
        with self.app.app_context():
            self.helper = User(username="helper", email="helper@test.invalid",
                               display_name="Helferlein",
                               password_hash="x", level="premium").save()
            Item(owner=self.user, kind="task", title="Delegiert",
                 status="next", assigned_to=self.helper,
                 assigned_by=self.user).save()

    def _set(self, show):
        self.client.post("/tasks/today/show-delegated", data={"show": show})

    def test_hidden_by_default_and_the_switch_says_how_many(self):
        html = self._plan_page(datetime(2026, 9, 24, 8, 0, tzinfo=BERLIN))
        self.assertNotIn("Delegiert<", html)
        self.assertIn("Delegated 1", html)
        self.assertNotIn("plan-rail-delegated", html)

    def test_switched_on_it_is_there_with_its_badge(self):
        self._set("1")
        html = self._plan_page(datetime(2026, 9, 24, 8, 0, tzinfo=BERLIN))
        self.assertIn("Delegiert<", html)
        self.assertIn("plan-rail-delegated", html)
        self.assertIn("Helferlein", html)
        self._set("0")
        html = self._plan_page(datetime(2026, 9, 24, 8, 0, tzinfo=BERLIN))
        self.assertNotIn("Delegiert<", html)

    def test_somebody_elses_task_assigned_elsewhere_never_shows(self):
        """Only work YOU delegated comes back — not every visible task that is
        with a third person."""
        from application.models.item import Item
        from application.views.tasks import _next_actions_pool
        with self.app.app_context():
            Item(owner=self.helper, kind="task", title="Fremd", status="next",
                 assigned_to=self.helper).save()
        self._set("1")
        with self.app.test_request_context():
            from application.models.user import User
            user = User.objects(pk=self.user.pk).first()
            pool, delegated = _next_actions_pool(
                user, datetime(2026, 9, 24, 8, 0, tzinfo=BERLIN), set())
        self.assertEqual(delegated, 1)
        self.assertEqual({it["title"] for it in pool}, {"Delegiert"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
