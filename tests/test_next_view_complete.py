"""Every task marked NEXT is on the page — and the two rules that hold one back.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

"Es werden in der Ansicht nicht alle Tasks mit next angezeigt." The rail that
offers next-actions on Today (the Plan timeline's *Next actions* and the same
rail under the single-day List view) used to stop at **50** pills behind a
**200**-document query window — and the count in the rail head was the
truncated number, so there was nothing on the page to notice. A next-action
that is not on the page is one that does not get done, which is why the cap is
gone rather than raised: ``/tasks/next`` already reads the identical set
unbounded off the same ``owner+status`` index.

The other cases pinned here are the shapes a container/grouping filter is most
likely to drop on its way through — **no project at all**, a nested folder, a
section, no duration, no plan date — under all three rail groupings, because a
task that belongs nowhere is exactly the one a "group by project" pass has no
bucket for.

The last two tests pin what is *deliberately* held back, so that changing it is
a decision somebody makes rather than a regression somebody causes:

* a **blocked** next-action (future defer date, pending follow-up, or waiting
  behind an earlier sibling in a sequential project) is not on Today at all —
  it is not actionable yet. It is still listed on ``/tasks/next``, greyed, and
  under *Scheduled*;
* a next-action **delegated to somebody else** is off the personal day-planner,
  and stays on ``/tasks/next`` because the owner still owns the outcome.
"""
import unittest
from datetime import datetime, timedelta, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class NextViewCompleteTests(unittest.TestCase):

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
        self.now = datetime.now(timezone.utc)
        with self.app.app_context():
            self._wipe()
            self.user = User(username="nv-test", email="nv@test.invalid",
                             password_hash="x", level="premium").save()
            self.mate = User(username="nv-mate", email="nv-mate@test.invalid",
                             password_hash="x").save()
            self.folder = Item(owner=self.user, kind="folder", title="Work").save()
            self.project = Item(owner=self.user, kind="project", title="Webshop",
                                parent=self.folder, project_type="parallel").save()
            self.deep = Item(owner=self.user, kind="folder", title="Deep",
                             parent=self.folder).save()
            self.deep_project = Item(owner=self.user, kind="project",
                                     title="Deep project", parent=self.deep,
                                     project_type="parallel").save()
            self.section = Item(owner=self.user, kind="section", title="Phase 1",
                                parent=self.project).save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    # -- helpers -----------------------------------------------------------

    def _next_task(self, title, **kw):
        from application.models.item import Item
        with self.app.app_context():
            return Item(owner=self.user, kind="task", status="next",
                        title=title, **kw).save()

    def _page(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, url)
        return resp.get_data(as_text=True)

    def _rail_ids(self, url):
        """The task ids the *Next actions* rail actually draws on ``url``.

        Read off the pills themselves (``plan-next-item``), not off the page,
        so a task that is only mentioned in a group head or a drag handler
        doesn't count as shown.
        """
        import re
        html = self._page(url)
        return set(re.findall(
            r'class="[^"]*plan-next-item[^"]*"[^>]*data-task-id="([0-9a-f]{24})"',
            html))

    def _list_ids(self, url):
        """The task ids rendered as rows on ``url`` (the shared row template)."""
        import re
        return set(re.findall(r'data-task-id="([0-9a-f]{24})"', self._page(url)))

    PLAN = "/tasks/today?view=plan&pgroup=none"
    LIST_RAIL = "/tasks/today?view=list&days=1&group=none"

    # -- nothing is capped -------------------------------------------------

    def test_a_next_rail_longer_than_fifty_shows_every_pill(self):
        """The regression: 60 next-actions used to render as 50, counted as 50."""
        made = {str(self._next_task(f"Task {i:02d}").pk) for i in range(60)}
        self.assertEqual(len(made), 60)
        self.assertEqual(self._rail_ids(self.PLAN), made)
        self.assertEqual(self._rail_ids(self.LIST_RAIL), made)

    def test_the_rail_count_matches_the_pills_it_drew(self):
        import re
        for i in range(55):
            self._next_task(f"Task {i:02d}")
        html = self._page(self.PLAN)
        drawn = len(re.findall(r'class="[^"]*plan-next-item', html))
        head = re.search(
            r'Next actions\s*<span class="plan-rail-count">(\d+)</span>', html)
        self.assertIsNotNone(head)
        self.assertEqual(int(head.group(1)), drawn)
        self.assertEqual(drawn, 55)

    def test_more_than_two_hundred_next_actions_all_arrive(self):
        """The 200-document query window cut the set before the rail even
        filtered it, so a big queue lost pills the 50-cap never reached."""
        made = {str(self._next_task(f"Bulk {i:03d}").pk) for i in range(205)}
        self.assertEqual(self._rail_ids(self.PLAN), made)

    # -- the shapes a grouping pass has no bucket for -----------------------

    def _one_of_each_shape(self):
        """A next-action of every shape that could fall out of a grouped rail."""
        return {
            "no project": self._next_task("Nothing at all"),
            "in a project": self._next_task("In Webshop", parent=self.project),
            "in a section": self._next_task("In Phase 1", parent=self.section),
            "in a nested folder": self._next_task("Deep down",
                                                  parent=self.deep_project),
            "filed straight on a folder": self._next_task("On the folder",
                                                          parent=self.folder),
            "with a duration": self._next_task("Timed", parent=self.project,
                                               estimated_minutes=45),
            "deferred, date already passed": self._next_task(
                "Unblocked again", parent=self.project,
                defer_date=self.now - timedelta(days=2)),
        }

    def test_every_shape_is_on_the_next_view(self):
        shapes = self._one_of_each_shape()
        shown = self._list_ids("/tasks/next")
        for label, task in shapes.items():
            self.assertIn(str(task.pk), shown, label)

    def test_every_shape_is_in_the_rail_under_every_grouping(self):
        shapes = self._one_of_each_shape()
        want = {str(t.pk) for t in shapes.values()}
        for pgroup in ("none", "project", "customer"):
            self.assertEqual(
                self._rail_ids(f"/tasks/today?view=plan&pgroup={pgroup}"), want,
                f"pgroup={pgroup}")

    def test_a_task_with_no_project_survives_todays_list_groupings(self):
        """"Group by project"/"by customer" must have a bucket for the task
        that belongs to neither, or marking it next makes it disappear."""
        orphan = self._next_task("Nothing at all")
        for group in ("none", "project", "customer", "status", "priority",
                      "tag", "due"):
            url = f"/tasks/today?view=list&days=1&group={group}"
            self.assertIn(str(orphan.pk), self._rail_ids(url), f"group={group}")

    def test_a_next_action_planned_for_today_is_on_the_day_not_in_the_rail(self):
        """It is not missing — it moved. The rail is what is NOT on the day
        yet, so a planned task would otherwise be drawn twice."""
        planned = self._next_task("Planned", parent=self.project,
                                  plan_date=self.now)
        self.assertIn(str(planned.pk), self._list_ids(self.LIST_RAIL))
        self.assertNotIn(str(planned.pk), self._rail_ids(self.LIST_RAIL))

    # -- what is held back on purpose --------------------------------------

    def test_a_blocked_next_action_is_off_today_but_listed_under_next(self):
        from application.models.item import Item
        with self.app.app_context():
            seq = Item(owner=self.user, kind="project", title="In order",
                       parent=self.folder, project_type="sequential").save()
        blocked = {
            "deferred into the future": self._next_task(
                "Starts Monday", parent=self.project,
                defer_date=self.now + timedelta(days=3)),
            "pending follow-up": self._next_task(
                "Chased already", parent=self.project,
                followup_at=self.now + timedelta(days=2)),
            "behind an earlier sibling": None,
        }
        self._next_task("First in line", parent=seq, sort_order=1)
        blocked["behind an earlier sibling"] = self._next_task(
            "Second in line", parent=seq, sort_order=2)

        on_today = self._rail_ids(self.PLAN) | self._list_ids(self.LIST_RAIL)
        under_next = self._list_ids("/tasks/next")
        for label, task in blocked.items():
            self.assertNotIn(str(task.pk), on_today, label)
            self.assertIn(str(task.pk), under_next, label)

    def test_a_next_action_delegated_away_stays_on_the_next_view_only(self):
        """The Plan view is a personal day-planner; the Next list is the
        owner's commitments, and handing work over doesn't end one."""
        handed_over = self._next_task("Yours now", parent=self.project,
                                      assigned_to=self.mate)
        self.assertNotIn(str(handed_over.pk), self._rail_ids(self.PLAN))
        self.assertIn(str(handed_over.pk), self._list_ids("/tasks/next"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
