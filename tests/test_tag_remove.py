"""Taking one label off an item from the list it is showing in.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

``tasks.remove_tag`` shares ``_drop_labels`` with the comment box, so the rules
that matter are the forgiving ones: a list can be seconds stale, and the same ×
clicked twice — or a label another tab already took off — must be a no-op
rather than an error, or the control would punish the user for the page being
old. The markup half is pinned too, because the control only reaches the other
list views through the shared macro.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class TagRemoveTests(unittest.TestCase):

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
        from application.models.tag import Tag
        self.Item = Item
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            self.user = User(username="lbl-test", email="lbl@test.invalid",
                             password_hash="x", level="premium").save()
            self.user.tags = [Tag(name="Cairn", color="#fabd2f"),
                              Tag(name="Review AI Work", color="#83a598")]
            self.user.save()
            self.other = User(username="lbl-other", email="lblo@test.invalid",
                              password_hash="x", level="premium").save()
            self.project = Item(owner=self.user, kind="project", title="A project",
                                project_type="parallel").save()
            self.task = Item(owner=self.user, kind="task", title="A labelled task",
                             parent=self.project, sort_order=1,
                             tags=["Cairn", "Review AI Work"]).save()
            self.theirs = Item(owner=self.other, kind="task", title="Not mine",
                               tags=["Cairn"]).save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _remove(self, task_id, tag):
        return self.client.post(f"/tasks/{task_id}/tags/remove", data={"tag": tag},
                                headers={"X-Requested-With": "XMLHttpRequest"})

    def _reload(self, item):
        with self.app.app_context():
            return self.Item.objects(pk=item.pk).first()

    # -- the removal -------------------------------------------------------

    def test_it_takes_off_the_one_label_and_leaves_the_rest(self):
        resp = self._remove(self.task.pk, "Review AI Work")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._reload(self.task).tags, ["Cairn"])

    def test_it_answers_with_what_is_left(self):
        payload = self._remove(self.task.pk, "Cairn").get_json()
        self.assertEqual(payload["removed"], ["Cairn"])
        self.assertEqual(payload["tags"], ["Review AI Work"])

    def test_the_name_is_matched_case_insensitively(self):
        self._remove(self.task.pk, "review ai work")
        self.assertEqual(self._reload(self.task).tags, ["Cairn"])

    def test_a_label_the_task_no_longer_carries_is_a_no_op_not_an_error(self):
        """Clicking the same × twice, or one a second tab already cleared."""
        self._remove(self.task.pk, "Cairn")
        resp = self._remove(self.task.pk, "Cairn")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["removed"], [])
        self.assertEqual(self._reload(self.task).tags, ["Review AI Work"])

    def test_an_empty_name_touches_nothing(self):
        resp = self._remove(self.task.pk, "")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._reload(self.task).tags, ["Cairn", "Review AI Work"])

    # -- what it refuses ---------------------------------------------------

    def test_someone_elses_task_is_refused(self):
        resp = self._remove(self.theirs.pk, "Cairn")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self._reload(self.theirs).tags, ["Cairn"])

    def test_a_missing_task_is_a_404_not_a_crash(self):
        resp = self._remove("000000000000000000000000", "Cairn")
        self.assertEqual(resp.status_code, 404)

    # -- the control in the list -------------------------------------------

    def test_the_task_list_renders_a_remove_control_per_label(self):
        html = self.client.get(f"/projects/{self.project.pk}").get_data(as_text=True)
        self.assertIn("tag-chip-remove", html)
        self.assertIn(f'data-tag-remove-url="/tasks/{self.task.pk}/tags/remove"', html)
        self.assertIn('data-tag-name="Review AI Work"', html)

    def test_the_control_is_a_button_so_it_cannot_start_a_drag(self):
        """SortableJS lets buttons through as clicks (kanban filters them, the
        list views drag by the grip alone) — a span here would grab the row."""
        html = self.client.get(f"/projects/{self.project.pk}").get_data(as_text=True)
        idx = html.index("tag-chip-remove")
        self.assertIn("<button", html[max(0, idx - 200):idx])

    def test_the_board_view_carries_it_too(self):
        html = self.client.get(f"/projects/{self.project.pk}?view=kanban").get_data(as_text=True)
        self.assertIn("tag-chip-remove", html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
