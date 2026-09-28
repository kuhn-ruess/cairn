"""The project inspector carries the same label token field as a task's.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

It used to render a bare ``<select multiple>`` ("⌘/Ctrl-click for
multi-select") while the task inspector had the chip field. The widget is the
shared ``tag-toggle.js``, so the markup half is what is pinned here — plus the
server half the widget needs: a label typed into it that doesn't exist yet has
to be accepted and created, and an exclusive group holds, exactly as on a task.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class ProjectInspectorLabelTests(unittest.TestCase):

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
        self.Item, self.User = Item, User
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            self.user = User(username="pil-test", email="pil@test.invalid",
                             password_hash="x", level="premium").save()
            self.user.tags = [Tag(name="Cairn", color="#fabd2f"),
                              Tag(name="Prio", exclusive_children=True),
                              Tag(name="Hoch", parent="Prio"),
                              Tag(name="Niedrig", parent="Prio")]
            self.user.save()
            self.project = Item(owner=self.user, kind="project", title="A project",
                                project_type="parallel").save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _inspector(self):
        return self.client.get(f"/projects/{self.project.pk}/edit?inspector=1")

    def _save(self, tags):
        return self.client.post(
            f"/projects/{self.project.pk}/edit?inspector=1",
            data={"name": "A project", "kind": "project", "status": "active",
                  "tags": tags},
            headers={"X-Inspector-Request": "1"})

    def _reload(self):
        with self.app.app_context():
            return self.Item.objects(pk=self.project.pk).first()

    def test_the_labels_render_as_the_token_field(self):
        html = self._inspector().get_data(as_text=True)
        self.assertIn('data-tag-toggle="1"', html)
        self.assertIn("data-tag-meta=", html)
        self.assertNotIn("multi-select", html)

    def test_a_typed_new_label_is_created_and_stored(self):
        resp = self._save(["Cairn", "Brandneu"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._reload().tags, ["Cairn", "Brandneu"])
        with self.app.app_context():
            user = self.User.objects(pk=self.user.pk).first()
            self.assertIsNotNone(user.tag_by_name("Brandneu"))

    def test_an_exclusive_group_keeps_one_label(self):
        self._save(["Hoch", "Niedrig"])
        self.assertEqual(self._reload().tags, ["Hoch"])


if __name__ == "__main__":
    unittest.main()
