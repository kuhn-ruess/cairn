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
        self.assertEqual(stats, {"tasks_created": 4, "tasks_skipped": 0, "sections_created": 0})
        konzept = self._child(self.project, "Konzept")
        self.assertEqual(konzept.kind, "task")      # stays a task, holds sub-tasks
        anf = self._child(konzept, "Anforderungen")
        self.assertIsNotNone(self._child(anf, "Interviews"))
        self.assertIsNotNone(self._child(self.project, "Umsetzung"))

    def test_existing_tasks_are_left_untouched(self):
        old = self.Item(owner=self.owner, kind="task", title="Konzept",
                        parent=self.project, priority=1, status="next").save()
        stats = self._sketch("- konzept p4 +neu\n  - Anforderungen")
        self.assertEqual(stats, {"tasks_created": 1, "tasks_skipped": 1, "sections_created": 0})
        old.reload()
        self.assertEqual((old.priority, old.status, list(old.tags)), (1, "next", []))
        self.assertEqual(self.Item.objects(parent=self.project).count(), 1)
        self.assertIsNotNone(self._child(old, "Anforderungen"))

    def test_same_sketch_twice_creates_nothing_new(self):
        text = "- A\n  - B\n- C"
        self._sketch(text)
        self.assertEqual(self._sketch(text), {"tasks_created": 0, "tasks_skipped": 3, "sections_created": 0})

    def test_sketch_extends_a_section(self):
        section = self.Item(owner=self.owner, kind="section", title="Phase 1",
                            parent=self.project).save()
        self._sketch("- Phase 1\n  - Kickoff")
        self.assertIsNotNone(self._child(section, "Kickoff"))

    def test_heading_makes_a_section(self):
        stats = self._sketch("- Lose\n\n## Phase 1\n- Kickoff\n  - Agenda\n"
                             "## Phase 2\n- Abnahme")
        self.assertEqual(stats, {"tasks_created": 4, "tasks_skipped": 0,
                                 "sections_created": 2})
        self.assertEqual(self._child(self.project, "Lose").kind, "task")
        p1 = self._child(self.project, "Phase 1")
        self.assertEqual(p1.kind, "section")
        self.assertIsNotNone(self._child(self._child(p1, "Kickoff"), "Agenda"))
        p2 = self._child(self.project, "Phase 2")
        self.assertEqual(p2.kind, "section")
        self.assertIsNotNone(self._child(p2, "Abnahme"))
        self.assertIsNone(self._child(self.project, "Abnahme"))

    def test_heading_uses_an_existing_section(self):
        section = self.Item(owner=self.owner, kind="section", title="Phase 1",
                            parent=self.project, status="active").save()
        stats = self._sketch("# phase 1\n- Kickoff")
        self.assertEqual(stats["sections_created"], 0)
        self.assertEqual(self.Item.objects(parent=self.project).count(), 1)
        self.assertIsNotNone(self._child(section, "Kickoff"))
        section.reload()
        self.assertEqual(section.title, "Phase 1")

    def test_hash_token_is_not_a_heading(self):
        self._sketch("- Anruf\n#Relaunch")
        self.assertEqual(self.Item.objects(parent=self.project,
                                           kind="section").count(), 0)

    def test_outline_shows_sections_as_headings(self):
        from application.services.capture import sketch_outline
        text = "- Lose\n\n## Phase 1\n- Kickoff\n  - Agenda"
        self._sketch(text)
        self.assertEqual(sketch_outline(self.project), text)
        # Saved back unchanged it creates nothing.
        self.assertEqual(self._sketch(sketch_outline(self.project)),
                         {"tasks_created": 0, "tasks_skipped": 3,
                          "sections_created": 0})

    def test_outline_round_trips(self):
        from application.services.capture import sketch_outline
        self._sketch("- A\n  - B\n- C")
        done = self._child(self.project, "C")
        done.status = "done"
        done.save()
        self.assertEqual(sketch_outline(self.project), "- A\n  - B")

    def test_moving_a_line_to_another_section_creates_nothing(self):
        from application.services.capture import sketch_outline
        self._sketch("## Phase 1\n- Kickoff\n  - Agenda\n## Phase 2\n- Abnahme")
        p1 = self._child(self.project, "Phase 1")
        kickoff = self._child(p1, "Kickoff")
        stats = self._sketch("## Phase 1\n## Phase 2\n- Abnahme\n- Kickoff\n"
                             "  - Agenda\n  - Raum buchen")
        self.assertEqual(stats, {"tasks_created": 1, "tasks_skipped": 3,
                                 "sections_created": 0})
        self.assertEqual(self.Item.objects(title="Kickoff").count(), 1)
        self.assertEqual(self.Item.objects(title="Agenda").count(), 1)
        # The existing task stays where it is; what is new under it lands
        # beneath it there.
        kickoff.reload()
        self.assertEqual(kickoff.parent.pk, p1.pk)
        self.assertIsNotNone(self._child(kickoff, "Raum buchen"))
        self.assertIn("## Phase 1\n- Kickoff", sketch_outline(self.project))

    def test_moving_a_line_to_another_parent_creates_nothing(self):
        self._sketch("- A\n  - B\n- C")
        b = self._child(self._child(self.project, "A"), "B")
        stats = self._sketch("- A\n- C\n  - B")
        self.assertEqual(stats["tasks_created"], 0)
        self.assertEqual(self.Item.objects(title="B").count(), 1)
        b.reload()
        self.assertEqual(b.parent.title, "A")

    def test_deleting_a_line_changes_nothing(self):
        self._sketch("- A\n  - B\n## Phase 1\n- C")
        stats = self._sketch("## Phase 1")
        self.assertEqual(stats["tasks_created"], 0)
        self.assertEqual(self.Item.objects(kind="task", title__in=["A", "B", "C"])
                         .count(), 3)

    def test_same_title_in_two_places_is_still_new(self):
        # The existing one is still listed at its place, so the second line
        # is a new task, not the existing one moved.
        self._sketch("## Phase 1\n- Testen")
        stats = self._sketch("- Testen\n## Phase 1\n- Testen")
        self.assertEqual(stats["tasks_created"], 1)
        self.assertEqual(self.Item.objects(title="Testen").count(), 2)

    def test_a_done_task_elsewhere_does_not_block_a_new_line(self):
        self._sketch("## Phase 1\n- Testen")
        done = self.Item.objects(title="Testen").first()
        done.status = "done"
        done.save()
        stats = self._sketch("## Phase 2\n- Testen")
        self.assertEqual(stats["tasks_created"], 1)

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
