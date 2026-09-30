"""The project picker in the Quick Add dialog (``base.html``, ``#quickAddProject``).

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Reported as "die Projektliste im Quick Add zeigt Notiz-Folder, und sollte
suchbar sein". A task can only be filed into the TASK tree, so no folder of
the notes tree may be offered there: not an own one, not a nested one, not a
notebook somebody shared, and not anything that hangs underneath one. And the
list is long, so it carries the same type-to-filter box as the inspector's
picker. Driven through the rendered page, because that is where both show.
"""
import re
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class QuickAddProjectPickerTests(unittest.TestCase):

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
        cls.ctx = cls.app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()
        disconnect(alias="default")

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        self.Item, self.User = Item, User
        self._wipe()
        self.kuhn = self._user("kuhn")
        self.ruess = self._user("ruess")
        Item = self.Item
        # The task tree.
        self.work = Item(owner=self.kuhn, kind="folder", folder_domain="tasks",
                         title="Arbeit", status="active").save()
        self.website = Item(owner=self.kuhn, kind="project", title="Website",
                            parent=self.work, status="active").save()
        # The notes tree: own, nested, and a notebook Rueß shares with Kuhn.
        self.kunden = Item(owner=self.kuhn, kind="folder", folder_domain="notes",
                           title="Kundennotizen", status="active").save()
        self.acme = Item(owner=self.kuhn, kind="folder", folder_domain="notes",
                         title="Acmenotizen", parent=self.kunden,
                         status="active").save()
        self.shared = Item(owner=self.ruess, kind="folder", folder_domain="notes",
                           title="Geteiltes Notizbuch", status="active",
                           members=[self.kuhn]).save()
        # A task-tree folder that ended up INSIDE the notes tree (a parent is
        # never checked for its domain on the project/API create paths).
        self.stray = Item(owner=self.kuhn, kind="folder", folder_domain="tasks",
                          title="Verirrter Ordner", parent=self.acme,
                          status="active").save()

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        from application.models.item import Item
        from application.models.user import User
        Item.all_objects.delete()
        User.objects.delete()

    def _user(self, name):
        return self.User(username=name, email=f"{name}@test.invalid",
                         password_hash="x", level="premium").save()

    def _picker_html(self):
        from flask import g
        # Both caches live on ``g``, and a test request reuses the class's app
        # context: without dropping them the page would be rendered for the
        # previous test's user and containers.
        g.pop("_login_user", None)
        g.pop("_viewable_container_pks", None)
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(self.kuhn.pk)
            sess["_fresh"] = True
        resp = client.get("/notes/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        m = re.search(r'<select id="quickAddProject".*?</select>', html, re.S)
        self.assertIsNotNone(m, "quick-add project picker not on the page")
        return m.group(0)

    def test_offers_the_task_tree(self):
        picker = self._picker_html()
        self.assertIn(str(self.work.pk), picker)
        self.assertIn(str(self.website.pk), picker)

    def test_no_folder_of_the_notes_tree(self):
        picker = self._picker_html()
        for item in (self.kunden, self.acme, self.shared):
            self.assertNotIn(str(item.pk), picker, item.title)
            self.assertNotIn(item.title, picker)

    def test_nothing_that_hangs_inside_the_notes_tree(self):
        picker = self._picker_html()
        self.assertNotIn(str(self.stray.pk), picker)

    def test_the_picker_is_searchable(self):
        picker = self._picker_html()
        self.assertIn("data-searchable-select", picker)


if __name__ == "__main__":
    unittest.main()
