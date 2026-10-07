"""A task's detail page shows its linked notes, each with a preview.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Notes are linked to a task through the symmetric ``related`` list, the same as
related tasks. The page lists only the notes the VIEWER may open: a task shared
with somebody must not hand them its owner's private notes.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class TaskLinkedNotesTests(unittest.TestCase):

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
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            self.user = User(username="ln-test", email="ln@test.invalid",
                             password_hash="x", level="premium").save()
            self.other = User(username="ln-other", email="ln-other@test.invalid",
                              password_hash="x", level="premium").save()
            self.task = Item(owner=self.user, kind="task", title="a task with notes").save()
            self.bare = Item(owner=self.user, kind="task", title="a task without notes").save()
            self.note = Item(owner=self.user, kind="note", title="Meeting prep",
                             note="- **Agenda** for the call\n- second point").save()
            self.task.relate_to(self.note)
        self.client = self.app.test_client()
        self.login(self.user)

    def login(self, user):
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(user.pk)
            sess["_fresh"] = True

    def page(self, task):
        resp = self.client.get(f"/tasks/{task.pk}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_a_linked_note_is_listed_with_its_preview(self):
        html = self.page(self.task)
        self.assertIn('id="linked-notes"', html)
        self.assertIn("Linked notes", html)
        self.assertIn("Meeting prep", html)
        self.assertIn(f"/notes/{self.note.pk}", html)
        # The preview is plain text: markdown and bullets are gone.
        self.assertIn("Agenda for the call second point", html)

    def test_no_linked_notes_no_section(self):
        html = self.page(self.bare)
        self.assertNotIn('id="linked-notes"', html)

    def test_a_related_task_is_not_listed_as_a_note(self):
        from application.models.item import Item
        with self.app.app_context():
            other_task = Item(owner=self.user, kind="task", title="just a task").save()
            self.bare.relate_to(other_task)
        html = self.page(self.bare)
        self.assertNotIn('id="linked-notes"', html)
        self.assertIn('id="related-tasks"', html)

    def test_a_trashed_note_is_not_listed(self):
        from datetime import datetime, timezone
        with self.app.app_context():
            self.note.trashed_at = datetime.now(timezone.utc)
            self.note.save()
        html = self.page(self.task)
        self.assertNotIn('id="linked-notes"', html)

    def test_a_note_the_viewer_may_not_open_stays_hidden(self):
        # The task is assigned to `other`, who can open it, but the note
        # linked to it is the owner's private note.
        from application.models.item import Item
        with self.app.app_context():
            self.task.assigned_to = self.other
            self.task.save()
            shared = Item(owner=self.user, kind="note", title="Shared brief",
                          note="for both of us", members=[self.other]).save()
            self.task.relate_to(shared)
        self.login(self.other)
        html = self.page(self.task)
        self.assertNotIn("Meeting prep", html)
        self.assertNotIn("Agenda for the call", html)
        self.assertIn("Shared brief", html)
        self.assertIn("Linked notes <span class=\"text-muted\">(1)</span>", html)

    def count_queries(self, task):
        from pymongo.collection import Collection
        original = Collection.find
        calls = []

        def counting_find(coll, *a, **kw):
            calls.append(coll.name)
            return original(coll, *a, **kw)

        Collection.find = counting_find
        try:
            html = self.page(task)
        finally:
            Collection.find = original
        return len(calls), html

    def test_more_notes_cost_no_more_queries(self):
        from application.models.item import Item
        with self.app.app_context():
            for i in range(10):
                n = Item(owner=self.user, kind="note", title=f"bulk note {i}",
                         note=f"body {i}").save()
                self.bare.relate_to(n)
        one, _ = self.count_queries(self.task)
        many, html = self.count_queries(self.bare)
        self.assertIn("bulk note 9", html)
        self.assertEqual(one, many, "a linked note costs a query of its own")


if __name__ == "__main__":
    unittest.main()
