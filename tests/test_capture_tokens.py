"""services/capture — the tokens on a line marked in a note or the scratchpad.

Run with ``python -m unittest discover tests``. Needs a MongoDB for the test
database (``bktodo_test``); skipped when there isn't one, like the other
DB-backed cases here.

Capture runs every marked line through the quick-add parser, so this is the
second place the ``@`` rule has to hold: ``@bastian anrufen`` in a note must
ASSIGN the task to Bastian (which notifies him), not tag it "bastian".
"""
import unittest


class CaptureTokenTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            from mongoengine import disconnect
            from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
        except ImportError:  # pragma: no cover - no mongoengine installed
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
        from application.models.tag import Tag
        from application.models.team import Team
        from application.models.user import User
        self.Item, self.User, self.Team = Item, User, Team
        self._wipe()
        self.owner = User(username="c-bastian", email="c-bastian@example.com",
                          display_name="Bastian", password_hash="x",
                          tags=[Tag(name="Buchhaltung", color="#6c757d")]).save()
        self.mate = User(username="c-sven", email="c-sven@example.com",
                         display_name="Sven", password_hash="x").save()
        self.team = Team(name="c-team", admin=self.owner).save()
        self.mate.team = self.team
        self.mate.save()

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        from application.models.item import Item
        from application.models.team import Team
        from application.models.user import User
        for t in Team.objects(name="c-team"):
            t.delete()
        for u in User.objects(username__in=("c-bastian", "c-sven")):
            Item.all_objects(owner=u).delete()
            u.delete()

    def _capture(self, text):
        from application.services import capture
        capture.create_tasks(text, self.owner)

    def test_at_on_a_captured_line_assigns_the_person(self):
        self._capture("@c-sven Rechnung prüfen")
        task = self.Item.objects(owner=self.owner, kind="task").first()
        self.assertIsNotNone(task)
        self.assertEqual(task.title, "Rechnung prüfen")
        self.assertEqual(task.assigned_to, self.mate)
        self.assertEqual(list(task.tags), [])

    def test_plus_on_a_captured_line_tags_the_task(self):
        self._capture("Rechnung prüfen +Buchhaltung")
        task = self.Item.objects(owner=self.owner, kind="task").first()
        self.assertEqual(task.title, "Rechnung prüfen")
        self.assertEqual(list(task.tags), ["Buchhaltung"])
        self.assertIsNone(task.assigned_to)

    def test_both_sigils_on_one_captured_line(self):
        self._capture("- [ ] Rechnung prüfen @c-sven +Buchhaltung")
        task = self.Item.objects(owner=self.owner, kind="task").first()
        self.assertEqual(task.title, "Rechnung prüfen")
        self.assertEqual(task.assigned_to, self.mate)
        self.assertEqual(list(task.tags), ["Buchhaltung"])

    def test_the_person_gets_told(self):
        """An assignment from a captured line is a real assignment, so the bell
        entry is what makes ``@name`` in a note mean the same as in a comment."""
        self._capture("@c-sven Rechnung prüfen")
        mate = self.User.objects(username="c-sven").first()
        self.assertEqual([n.kind for n in mate.notifications], ["assigned"])


if __name__ == "__main__":
    unittest.main()
