"""Plan my day respects task dependencies (``Item.depends_on``).

Run with ``python -m unittest discover tests``. The pure half needs nothing;
the rest needs a MongoDB (``TestConfig`` points at ``bktodo_test``) and is
skipped without one.

From the request on the task: even when the task a planned task depends on is
not on the day list, the Plan view must point it out, the order must be right
(the blocker before the task waiting for it), and the blocker should come onto
the day with one click.

What is pinned here:

* the order: ``order_by_dependencies`` puts a blocker before its dependent and
  lends it the dependent's urgency; ``drop_unplaced_chains`` unplaces a task
  whose blocker got no slot;
* a task whose blocker is not on the day is NOT given a time by Plan my day
  (it stays on the day, untimed, in Due) and the answer counts it as
  ``waiting``;
* a task held back only by its dependencies is shown in Due (it used to vanish
  from the rail) with "Waits for" and, when the blocker is the user's own task,
  a "Pull onto day" button; pulling it (``/unschedule`` with the day) and
  planning again places the blocker first;
* a Next action that a due task waits for is planned before it;
* somebody else's blocker is named but never offered to pull.
"""
import unittest
from datetime import datetime

from application.services.auto_plan import (
    drop_unplaced_chains, order_by_dependencies)

try:
    from tests.test_plan_nothing_vanishes import BERLIN, DAY, PlanBase
except Exception:  # pragma: no cover (no mongoengine installed)
    PlanBase = None


class OrderByDependenciesTests(unittest.TestCase):

    def test_a_blocker_comes_before_its_dependent(self):
        order, held = order_by_dependencies(["a", "b"], {"a": ["b"]})
        self.assertEqual(order, ["b", "a"])
        self.assertEqual(held, set())

    def test_the_blocker_takes_the_urgency_of_its_dependent(self):
        """b is the least urgent on its own, but a (the most urgent) waits for
        it, so it goes first, not after c."""
        order, _ = order_by_dependencies(["a", "c", "b"], {"a": ["b"]})
        self.assertEqual(order, ["b", "a", "c"])

    def test_a_blocker_off_the_list_holds_the_whole_chain(self):
        order, held = order_by_dependencies(
            ["a", "b", "c"], {"a": ["b"], "b": ["x"]})
        self.assertEqual(order, ["c"])
        self.assertEqual(held, {"a", "b"})

    def test_a_chain_on_the_list_is_ordered_end_to_end(self):
        order, _ = order_by_dependencies(
            ["a", "b", "c"], {"a": ["b"], "b": ["c"]})
        self.assertEqual(order, ["c", "b", "a"])

    def test_no_dependencies_keeps_the_order(self):
        self.assertEqual(order_by_dependencies(["a", "b", "c"], {})[0],
                         ["a", "b", "c"])

    def test_a_cycle_does_not_hang(self):
        order, _ = order_by_dependencies(["a", "b"], {"a": ["b"], "b": ["a"]})
        self.assertEqual(sorted(order), ["a", "b"])

    def test_an_unplaced_blocker_unplaces_its_chain(self):
        starts = drop_unplaced_chains(
            ["c", "b", "a", "d"], [None, 1, 2, 3], {"a": ["b"], "b": ["c"]})
        self.assertEqual(starts, [None, None, None, 3])


@unittest.skipIf(PlanBase is None, "mongoengine not installed")
class PlanDependencyTests(PlanBase if PlanBase else unittest.TestCase):

    MORNING = datetime(2026, 9, 24, 8, 0, tzinfo=BERLIN) if PlanBase else None

    def _task(self, title, **kw):
        from application.models.item import Item
        kw.setdefault("owner", self.user)
        with self.app.app_context():
            return Item(kind="task", title=title, parent=self.project, **kw).save()

    def _depend(self, task, on):
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=task.pk).first().add_dependency(
                Item.objects(pk=on.pk).first())

    def _local(self, task):
        pd = self._reload(task).plan_date
        return pd.astimezone(BERLIN) if pd else None

    def _midnight(self):
        return datetime.combine(DAY, datetime.min.time()).replace(tzinfo=BERLIN)

    def test_a_blocker_on_the_day_is_planned_before_its_dependent(self):
        """The afternoon task waits for the morning one: priority would put it
        first, the dependency puts it after."""
        self._reload(self.afternoon).update(set__priority=1)
        self._depend(self.morning, on=self.afternoon)
        body = self._repack(self.MORNING)
        self.assertEqual(body["waiting"], 0)
        self.assertLess(self._local(self.afternoon), self._local(self.morning))

    def test_a_blocker_off_the_day_keeps_the_dependent_untimed(self):
        other = self._task("Angebot einholen")
        self._depend(self.morning, on=other)
        body = self._repack(self.MORNING)
        self.assertEqual(body["waiting"], 1)
        local = self._local(self.morning)
        self.assertEqual(local.date(), DAY)
        self.assertEqual((local.hour, local.minute), (0, 0))
        self.assertIsNone(self._reload(other).plan_date)

    def test_the_waiting_task_is_in_due_with_the_blocker_named(self):
        other = self._task("Angebot einholen")
        self._depend(self.morning, on=other)
        self._reload(self.morning).update(set__plan_date=self._midnight())
        _, backlog = self._rails(self.MORNING)
        self.assertIn("Rechnung schreiben", backlog)
        html = self._plan_page(self.MORNING)
        self.assertIn("Waits for", html)
        self.assertIn("Angebot einholen", html)
        self.assertIn(f'data-task-id="{other.pk}"', html)
        self.assertIn("plan-dep-pull", html)

    def test_pull_onto_day_then_plan_places_the_blocker_first(self):
        other = self._task("Angebot einholen", estimated_minutes=30)
        self._depend(self.morning, on=other)
        resp = self.client.post(f"/tasks/{other.pk}/unschedule",
                                data={"date": DAY.isoformat()})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._local(other), self._midnight())
        body = self._repack(self.MORNING)
        self.assertEqual(body["waiting"], 0)
        self.assertLess(self._local(other), self._local(self.morning))
        self.assertTrue(self._local(other).hour)

    def test_a_next_action_it_waits_for_is_planned_before_it(self):
        nxt = self._task("Zugang anfragen", status="next", estimated_minutes=30)
        self._depend(self.morning, on=nxt)
        body = self._repack(self.MORNING)
        self.assertEqual(body["waiting"], 0)
        self.assertLess(self._local(nxt), self._local(self.morning))

    def test_a_done_blocker_blocks_nothing(self):
        other = self._task("Angebot einholen", status="done")
        self._depend(self.morning, on=other)
        body = self._repack(self.MORNING)
        self.assertEqual(body["waiting"], 0)
        self.assertTrue(self._local(self.morning).hour)
        self.assertNotIn("Waits for", self._plan_page(self.MORNING))

    def test_somebody_elses_blocker_is_named_but_not_offered(self):
        from application.models.user import User
        with self.app.app_context():
            colleague = User(username="colleague", email="c@test.invalid",
                             password_hash="x", level="premium").save()
        other = self._task("Freigabe geben", owner=colleague)
        self._depend(self.morning, on=other)
        html = self._plan_page(self.MORNING)
        self.assertIn("Freigabe geben", html)
        self.assertNotIn("plan-dep-pull", html)

    def test_a_timed_dependent_before_its_blocker_is_flagged(self):
        """Dragged by hand: the morning task waits for the afternoon one."""
        self._depend(self.morning, on=self.afternoon)
        html = self._plan_page(self.MORNING)
        self.assertIn("after this one", html)


if __name__ == "__main__":
    unittest.main()
