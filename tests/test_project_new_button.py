"""The "+" split button on a project page adds a section by default.

Laying out a project starts with its sections, so in a project the main
button is "Section" and Task sits in the menu beside it. A folder cannot hold
a section, so its main button stays "Task".
"""
import re
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class ProjectNewButtonTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()
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
            self.user = User(username="nb-test", email="nb-test@test.invalid",
                             password_hash="x", level="premium").save()
            self.folder = Item(owner=self.user, kind="folder",
                               title="Ablage").save()
            self.project = Item(owner=self.user, kind="project", title="Umzug",
                                project_type="parallel",
                                parent=self.folder).save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _main_button(self, item):
        html = self.client.get(f"/projects/{item.pk}").get_data(as_text=True)
        match = re.search(
            r'<a class="btn btn-primary"[^>]*>\s*<i class="bi bi-plus-lg"></i>'
            r'\s*(\w+)\s*</a>', html)
        self.assertIsNotNone(match, "no main new-item button on the page")
        return match.group(1)

    def test_a_project_adds_a_section_by_default(self):
        self.assertEqual(self._main_button(self.project), "Section")

    def test_a_folder_still_adds_a_task(self):
        self.assertEqual(self._main_button(self.folder), "Task")


if __name__ == "__main__":
    unittest.main()
