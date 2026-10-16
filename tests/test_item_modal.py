"""Tasks and notes open in a dialog over the page (static/js/item-modal.js).

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

The dialog frames the item's ordinary page. Three things have to hold on the
server for that to work:

- framed (``?embed=1`` or ``Sec-Fetch-Dest: iframe``, the second one is what a
  redirect inside the frame still carries), the page leaves out the navbar, the
  sidebar and the footer, and unframed it keeps them: a deep link is the same
  URL and must still be the full page;
- the inspector beside the task leaves the note out (the page edits it in its
  own column) and its save must then not touch the note. Before, it carried a
  second copy of the note, and saving the inspector after editing the note on
  the left wrote the stale copy back;
- the inspector in the side panel still saves the note as before.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover (no mongoengine installed)
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class ItemModalTests(unittest.TestCase):

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
            self.me = User(username="im-me", email="im-me@test.invalid",
                           password_hash="x", level="premium").save()
            self.task = Item(owner=self.me, kind="task", status="open",
                             title="Write the report", note="Original note").save()
            self.note = Item(owner=self.me, kind="note", title="Meeting notes",
                             note="- First point").save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.me.pk)
            sess["_fresh"] = True

    def _html(self, url, **headers):
        resp = self.client.get(url, headers=headers)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_framed_task_page_has_no_chrome(self):
        for html in (self._html(f"/tasks/{self.task.pk}?embed=1"),
                     self._html(f"/tasks/{self.task.pk}", **{"Sec-Fetch-Dest": "iframe"})):
            self.assertIn("item-embed", html.split("<head>")[0])
            self.assertNotIn('class="navbar', html)
            self.assertNotIn("app-nav-sidebar", html)
            self.assertNotIn("Impressum", html)
            self.assertIn("task-detail-panes", html)

    def test_framed_note_page_has_no_chrome(self):
        html = self._html(f"/notes/{self.note.pk}?embed=1")
        self.assertNotIn('class="navbar', html)
        self.assertNotIn("app-nav-sidebar", html)

    def test_deep_link_is_still_the_full_page(self):
        html = self._html(f"/tasks/{self.task.pk}")
        self.assertNotIn("item-embed", html.split("<head>")[0])
        self.assertIn('class="navbar', html)
        self.assertIn("app-nav-sidebar", html)
        self.assertIn("js/item-modal.js", html)

    def test_inline_inspector_has_no_note_field(self):
        html = self._html(f"/tasks/{self.task.pk}")
        inline = html.split('id="inline-inspector"', 1)[1]
        self.assertIn('name="inspector_omit" value="note"', inline)
        self.assertNotIn('name="note" class="form-control" rows="3"', inline)

    def _save(self, **extra):
        data = {
            "title": "Write the report", "project": "", "status": "open",
            "priority": "", "defer_date": "", "due_date": "", "plan_date": "",
            "start_date": "", "end_date": "", "estimated_minutes": "",
            "location": "", "waiting_on": "", "followup_at": "",
            "followup_every_days": "", "review_interval_days": "",
            "repeat_every_days": "", "repeat_unit": "day", "repeat_monthday": "",
            "assigned_to": "", "owner": str(self.me.pk),
        }
        data.update(extra)
        resp = self.client.post(f"/tasks/{self.task.pk}/edit", data=data,
                                headers={"X-Inspector-Request": "1"})
        self.assertEqual(resp.status_code, 200)
        from application.models.item import Item
        with self.app.app_context():
            return Item.objects(pk=self.task.pk).first().note

    def test_inline_save_leaves_the_note_alone(self):
        # The note was edited on the left meanwhile; the inspector never had it.
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.task.pk).update(set__note="Edited on the left")
        self.assertEqual(self._save(inspector_omit="note"), "Edited on the left")

    def test_panel_save_still_writes_the_note(self):
        self.assertEqual(self._save(note="From the panel"), "From the panel")


if __name__ == "__main__":
    unittest.main()
