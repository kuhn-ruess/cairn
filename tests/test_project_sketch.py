"""services/capture: the project page's Markdown sketch.

Run with ``python -m unittest discover tests``. Needs a MongoDB for the test
database (``bktodo_test``); skipped when there isn't one.

A sketch is a nested list typed into a project: new lines become tasks,
indentation becomes sub-tasks, and anything that already exists is left
exactly as it is (only used as the anchor for the lines under it).
"""
import unittest


class ProjectSketchTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            from mongoengine import disconnect
            from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
        except ImportError:  # pragma: no cover (no mongoengine installed)
            raise unittest.SkipTest("mongoengine not installed")
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()          # forces a real round trip
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")
        cls.ctx = cls.app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "ctx"):
            cls.ctx.pop()

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        self.Item = Item
        self._wipe()
        self.owner = User(username="sk-bastian", email="sk-bastian@example.com",
                          password_hash="x").save()
        self.project = Item(owner=self.owner, kind="project",
                            title="Relaunch", status="active").save()

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        from application.models.item import Item
        from application.models.user import User
        for u in User.objects(username="sk-bastian"):
            Item.all_objects(owner=u).delete()
            u.delete()

    def _sketch(self, text):
        from application.services.capture import sketch_tasks
        return sketch_tasks(text, self.owner, self.project)

    def _child(self, parent, title):
        return self.Item.objects(parent=parent, title=title).first()

    def test_nesting_becomes_sub_tasks(self):
        stats = self._sketch("- Konzept\n  - Anforderungen\n    - Interviews\n- Umsetzung")
        self.assertEqual(stats, {"tasks_created": 4, "tasks_skipped": 0})
        konzept = self._child(self.project, "Konzept")
        self.assertEqual(konzept.kind, "task")      # stays a task, holds sub-tasks
        anf = self._child(konzept, "Anforderungen")
        self.assertIsNotNone(self._child(anf, "Interviews"))
        self.assertIsNotNone(self._child(self.project, "Umsetzung"))

    def test_existing_tasks_are_left_untouched(self):
        old = self.Item(owner=self.owner, kind="task", title="Konzept",
                        parent=self.project, priority=1, status="next").save()
        stats = self._sketch("- konzept p4 +neu\n  - Anforderungen")
        self.assertEqual(stats, {"tasks_created": 1, "tasks_skipped": 1})
        old.reload()
        self.assertEqual((old.priority, old.status, list(old.tags)), (1, "next", []))
        self.assertEqual(self.Item.objects(parent=self.project).count(), 1)
        self.assertIsNotNone(self._child(old, "Anforderungen"))

    def test_same_sketch_twice_creates_nothing_new(self):
        text = "- A\n  - B\n- C"
        self._sketch(text)
        self.assertEqual(self._sketch(text), {"tasks_created": 0, "tasks_skipped": 3})

    def test_sketch_extends_a_section(self):
        section = self.Item(owner=self.owner, kind="section", title="Phase 1",
                            parent=self.project).save()
        self._sketch("- Phase 1\n  - Kickoff")
        self.assertIsNotNone(self._child(section, "Kickoff"))

    def test_outline_round_trips(self):
        from application.services.capture import sketch_outline
        self._sketch("- A\n  - B\n- C")
        done = self._child(self.project, "C")
        done.status = "done"
        done.save()
        self.assertEqual(sketch_outline(self.project), "- A\n  - B")

    def test_page_and_post(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(self.owner.pk)
            sess["_fresh"] = True
        pid = str(self.project.pk)
        page = client.get(f"/projects/{pid}?view=sketch")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="project-sketch"', page.data)
        resp = client.post(f"/projects/{pid}/sketch", data={"text": "- A\n  - B"})
        self.assertEqual(resp.status_code, 302)
        self.assertIsNotNone(self._child(self._child(self.project, "A"), "B"))
        listing = client.get(f"/projects/{pid}")
        self.assertEqual(listing.status_code, 200)
        self.assertIn(b"bi-markdown", listing.data)


if __name__ == "__main__":
    unittest.main()
