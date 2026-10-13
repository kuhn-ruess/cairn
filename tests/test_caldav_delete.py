"""views/caldav: a DELETE from Reminders must not trash a finished task.

Reminders clears completed reminders in bulk ("Clear completed", or the
"Clear" button under a list's completed reminders), and each one goes out as a
CalDAV DELETE. Cairn trashed every one of them, together with its sub tasks:
finished work vanished from Cairn although the user had only tidied a list on
the phone. A closed task is now left alone and simply no longer served; an
open task deleted on the device still goes to the trash.

Run with ``python -m unittest discover tests``. Needs a MongoDB
(``TestConfig`` points at ``bktodo_test``) and is skipped without one.
"""
import base64
import unittest
from datetime import datetime, timedelta, timezone

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover (no mongoengine installed)
    disconnect = None

PASSWORD = "reminders-pw"


@unittest.skipIf(disconnect is None, "mongoengine not installed")
class CalDavDeleteTests(unittest.TestCase):

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
        cls.ctx = cls.app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls):
        cls._wipe()
        cls.ctx.pop()
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
        self.Item = Item
        self._wipe()
        self.user = User(username="owner", email="owner@test.invalid",
                         level="premium")
        self.user.set_password(PASSWORD)
        self.user.save()
        self.project = Item(owner=self.user, kind="project", title="Project",
                            status="active").save()
        self.done = Item(owner=self.user, kind="task", parent=self.project,
                         title="Finished task", status="done").save()
        self.sub = Item(owner=self.user, kind="task", parent=self.done,
                        title="Sub task", status="open").save()
        self.open = Item(owner=self.user, kind="task", parent=self.project,
                         title="Open task", status="open").save()
        self.client = self.app.test_client()
        cred = base64.b64encode(f"owner:{PASSWORD}".encode()).decode()
        self.headers = {"Authorization": f"Basic {cred}",
                        "User-Agent": "iOS/26.0 (23A341) remindd/1.0"}

    def fresh(self, item):
        return self.Item.all_objects(pk=item.pk).first()

    def delete(self, item):
        resp = self.client.delete(
            f"/caldav/calendars/owner/{self.project.pk}/cairn-{item.pk}.ics",
            headers=self.headers)
        self.assertEqual(resp.status_code, 204)

    def listed(self):
        resp = self.client.open(
            f"/caldav/calendars/owner/{self.project.pk}/", method="PROPFIND",
            headers={**self.headers, "Depth": "1"})
        self.assertEqual(resp.status_code, 207)
        return resp.get_data(as_text=True)

    def test_clearing_a_completed_reminder_trashes_nothing(self):
        self.delete(self.done)
        self.assertIsNone(self.fresh(self.done).trashed_at)
        self.assertIsNone(self.fresh(self.sub).trashed_at)
        self.assertEqual(self.fresh(self.done).status, "done")

    def test_a_cleared_task_is_no_longer_served(self):
        self.assertIn(f"cairn-{self.done.pk}.ics", self.listed())
        self.delete(self.done)
        body = self.listed()
        self.assertNotIn(f"cairn-{self.done.pk}.ics", body)
        self.assertIn(f"cairn-{self.sub.pk}.ics", body)

    def test_a_cleared_task_comes_back_when_reopened(self):
        self.delete(self.done)
        t = self.fresh(self.done)
        t.status = "open"
        t.save()
        self.assertIn(f"cairn-{self.done.pk}.ics", self.listed())

    def test_finished_again_after_the_clear_it_is_served_again(self):
        self.delete(self.done)
        t = self.fresh(self.done)
        t.completed_at = datetime.now(timezone.utc) + timedelta(seconds=5)
        t.save()
        self.assertIn(f"cairn-{self.done.pk}.ics", self.listed())

    def test_a_dropped_task_is_not_trashed_either(self):
        t = self.fresh(self.open)
        t.status = "dropped"
        t.save()
        self.delete(self.open)
        self.assertIsNone(self.fresh(self.open).trashed_at)

    def test_deleting_an_open_reminder_still_trashes_the_task(self):
        self.delete(self.open)
        t = self.fresh(self.open)
        self.assertIsNotNone(t.trashed_at)
        self.assertEqual(t.trashed_source, "caldav")
        self.assertEqual(t.trashed_via, "CalDAV (Apple Reminders)")


if __name__ == "__main__":
    unittest.main()
