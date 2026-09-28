"""The bell keeps itself current: ``GET /notifications/bell``.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

The page polls this every half minute, so what is pinned is the cheap path: the
fingerprint the page was rendered with comes back as ``If-None-Match`` and,
with nothing new, is answered 304 with no body. Anything that changes what the
bell shows — a new entry, an entry read — changes the fingerprint.
"""
import unittest

from bson import ObjectId

from application.models.user import User

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


class NotificationEtagTest(unittest.TestCase):

    def test_new_entry_and_read_state_change_it(self):
        user = User(username="tester")
        empty = user.notification_etag
        user.add_notification(text="a", kind="comment", item_id=ObjectId())
        one = user.notification_etag
        self.assertNotEqual(empty, one)
        user.mark_notifications_read()
        self.assertNotEqual(one, user.notification_etag)

    def test_stable_while_nothing_changes(self):
        user = User(username="tester")
        user.add_notification(text="a", kind="comment")
        self.assertEqual(user.notification_etag, user.notification_etag)


@unittest.skipIf(connect is None, "mongoengine not installed")
class BellPollTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                User.objects.first()  # forces a real round trip
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            User.objects.delete()
        disconnect(alias="default")

    def setUp(self):
        with self.app.app_context():
            User.objects.delete()
            self.user = User(username="bell-test", email="bell@test.invalid",
                             password_hash="x", level="premium")
            self.user.add_notification(text="Old news", kind="comment",
                                       item_id=ObjectId())
            self.user.save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _notify(self, text):
        with self.app.app_context():
            user = User.objects(pk=self.user.pk).first()
            user.add_notification(text=text, kind="comment", item_id=ObjectId())
            user.save()

    def test_first_poll_returns_badge_and_list(self):
        resp = self.client.get("/notifications/bell")
        self.assertEqual(200, resp.status_code)
        data = resp.get_json()
        self.assertEqual(1, data["unread"])
        self.assertIn("Old news", data["html"])
        self.assertIn("notif-unread", data["html"])
        self.assertEqual(data["etag"], resp.headers["ETag"].strip('"'))

    def test_nothing_new_is_a_bodiless_304(self):
        etag = self.client.get("/notifications/bell").get_json()["etag"]
        resp = self.client.get("/notifications/bell",
                               headers={"If-None-Match": f'"{etag}"'})
        self.assertEqual(304, resp.status_code)
        self.assertEqual(b"", resp.data)

    def test_the_page_carries_the_fingerprint_the_poll_compares(self):
        """The first poll after a page load must be a 304, not a re-render."""
        etag = self.client.get("/notifications/bell").get_json()["etag"]
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn(f'data-etag="{etag}"', html)
        self.assertIn('id="notifMenu"', html)

    def test_a_new_notification_arrives(self):
        etag = self.client.get("/notifications/bell").get_json()["etag"]
        self._notify("Fresh comment")
        resp = self.client.get("/notifications/bell",
                               headers={"If-None-Match": f'"{etag}"'})
        self.assertEqual(200, resp.status_code)
        data = resp.get_json()
        self.assertEqual(2, data["unread"])
        self.assertIn("Fresh comment", data["html"])
        self.assertNotEqual(etag, data["etag"])

    def test_marking_read_elsewhere_clears_the_badge(self):
        etag = self.client.get("/notifications/bell").get_json()["etag"]
        self.client.post("/notifications/read")
        resp = self.client.get("/notifications/bell",
                               headers={"If-None-Match": f'"{etag}"'})
        self.assertEqual(200, resp.status_code)
        self.assertEqual(0, resp.get_json()["unread"])

    def test_logged_out_gets_no_list(self):
        resp = self.app.test_client().get("/notifications/bell")
        self.assertNotEqual(200, resp.status_code)


if __name__ == "__main__":
    unittest.main()
