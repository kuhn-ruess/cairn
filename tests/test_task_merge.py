"""Dropping a task onto another one nests it as that task's sub-task.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Two halves are pinned here, because the gesture only works if both hold:

* ``POST /tasks/merge`` — re-parents A under B, refuses a task onto itself and
  refuses a task onto one of its own descendants (that would cut the subtree
  out of the tree entirely), and leaves B expanded so the new child is visible.
* the project list's markup — every task row carries its OWN status
  (``data-task-status``) beside the bucket's (``data-target-status``). The list
  is built out of status buckets, and ``sortable-tasks.js`` may only refuse to
  nest where the bucket would actually re-bucket the task. Without the row's
  status it has nothing to compare against and the whole project list silently
  stops nesting — which is exactly the bug this pair prevents coming back.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class TaskMergeTests(unittest.TestCase):

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
            from application.models.user import User
            Item.all_objects.delete()
            User.objects.delete()
        disconnect(alias="default")

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        self.Item = Item
        self.User = User
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            self.user = User(username="merge-test", email="merge@test.invalid",
                             password_hash="x", level="premium").save()
            self.other = User(username="merge-other", email="mother@test.invalid",
                              password_hash="x", level="premium").save()
            self.project = Item(owner=self.user, kind="project", title="A project",
                                project_type="parallel").save()
            self.alpha = Item(owner=self.user, kind="task", title="Alpha",
                              parent=self.project, sort_order=1).save()
            self.bravo = Item(owner=self.user, kind="task", title="Bravo",
                              parent=self.project, sort_order=2).save()
            self.kid = Item(owner=self.user, kind="task", title="Alpha's kid",
                            parent=self.alpha, sort_order=1).save()
            self.theirs = Item(owner=self.other, kind="task", title="Not mine").save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _merge(self, source, target):
        return self.client.post("/tasks/merge", data={
            "source_id": str(source), "target_id": str(target)})

    def _reload(self, item):
        with self.app.app_context():
            return self.Item.objects(pk=item.pk).first()

    # -- the gesture ------------------------------------------------------

    def test_a_task_dropped_on_another_becomes_its_sub_task(self):
        resp = self._merge(self.alpha.pk, self.bravo.pk)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._reload(self.alpha).parent.pk, self.bravo.pk)

    def test_the_sub_tasks_it_already_had_come_along(self):
        """Alpha keeps its own child — nesting moves the subtree, not one row."""
        self._merge(self.alpha.pk, self.bravo.pk)
        self.assertEqual(self._reload(self.kid).parent.pk, self.alpha.pk)

    def test_the_target_stays_a_task(self):
        """A task holds child tasks directly; it is not turned into a project."""
        self._merge(self.alpha.pk, self.bravo.pk)
        self.assertEqual(self._reload(self.bravo).kind, "task")

    def test_the_target_is_left_expanded_so_the_new_child_shows(self):
        self._merge(self.alpha.pk, self.bravo.pk)
        with self.app.app_context():
            user = self.User.objects(pk=self.user.pk).first()
            self.assertIn(str(self.bravo.pk), list(user.collapsed_items or []))

    # -- what it refuses --------------------------------------------------

    def test_a_task_cannot_be_nested_under_itself(self):
        resp = self._merge(self.alpha.pk, self.alpha.pk)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._reload(self.alpha).parent.pk, self.project.pk)

    def test_a_task_cannot_be_nested_under_its_own_descendant(self):
        """Alpha under Alpha's kid would cut the pair loose from the tree."""
        resp = self._merge(self.alpha.pk, self.kid.pk)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._reload(self.alpha).parent.pk, self.project.pk)
        self.assertEqual(self._reload(self.kid).parent.pk, self.alpha.pk)

    def test_someone_elses_task_is_refused(self):
        resp = self._merge(self.theirs.pk, self.bravo.pk)
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self._reload(self.theirs).parent)

    def test_a_missing_task_is_refused(self):
        resp = self._merge("000000000000000000000000", self.bravo.pk)
        self.assertEqual(resp.status_code, 400)

    # -- the markup the client compares against ---------------------------

    def test_the_project_list_ships_each_row_its_own_status(self):
        html = self.client.get(f"/projects/{self.project.pk}").get_data(as_text=True)
        self.assertIn('data-target-status="open"', html)
        self.assertIn(f'data-task-id="{self.alpha.pk}" data-task-status="open"', html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
