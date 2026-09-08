"""The "Sync now" buttons — what the server tells them while a round runs.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

The bug this guards: a round handed to a worker has NOT run yet, so everything
the response can say about it is the previous round's. A page that renders
that reads as a result — it snaps back to the old time, and only a reload ever
corrects it. So the answer says ``queued``, and the state endpoint is what the
page waits on.
"""
import sys
import types
import unittest
from datetime import datetime, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class SyncNowTests(unittest.TestCase):

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
        # Deliberately NO app context held open around the requests: Flask
        # reuses an outer one, and Flask-Login caches the signed-in user on
        # ``g`` — every request would then answer with the user as it was at
        # the first one, which is the very thing these tests are about.

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            from application.models.user import User
            User.objects(username="ui-test").delete()
        disconnect(alias="default")

    def setUp(self):
        from application.models.user import User
        self.User = User
        with self.app.app_context():
            User.objects(username="ui-test").delete()
            user = User(username="ui-test", email="u@example.com",
                        level="premium", notes_mirror_enabled=True,
                        notes_mirror_url="https://nas.example/x",
                        notes_mirror_status="3 file(s) written",
                        last_notes_mirror_at=datetime(2026, 9, 8, 6, 0,
                                                      tzinfo=timezone.utc))
            user.set_password("x")
            user.save()
            self.user = user
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True
        self._real_tasks = sys.modules.get("application.services.sync_tasks")

    def tearDown(self):
        if self._real_tasks is not None:
            sys.modules["application.services.sync_tasks"] = self._real_tasks
        else:
            sys.modules.pop("application.services.sync_tasks", None)

    def finished(self, *, when=None, status=None):
        """What the worker's round leaves behind when it lands."""
        fields = {}
        if when:
            fields["set__last_notes_mirror_at"] = when
        if status:
            fields["set__notes_mirror_status"] = status
        with self.app.app_context():
            self.User.objects(pk=self.user.pk).update(**fields)

    def with_broker(self):
        """A worker that takes the round and says nothing else about it."""
        fake = types.ModuleType("application.services.sync_tasks")
        fake.enqueue_notes_mirror = lambda *a, **kw: None
        sys.modules["application.services.sync_tasks"] = fake

    def test_a_round_handed_to_a_worker_says_so(self):
        self.with_broker()
        data = self.client.post(
            "/settings/notes-mirror/sync",
            headers={"X-Requested-With": "XMLHttpRequest"}).get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["queued"])
        # And what it reports is the round BEFORE — which is exactly why the
        # page must not render it as the result.
        self.assertEqual(data["at"], "2026-09-08T06:00:00+00:00")

    def test_the_state_endpoint_answers_when_the_round_lands(self):
        self.with_broker()
        before = self.client.post(
            "/settings/notes-mirror/sync",
            headers={"X-Requested-With": "XMLHttpRequest"}).get_json()

        during = self.client.get("/settings/notes-mirror/state").get_json()
        self.assertEqual(during["at"], before["at"])
        self.assertEqual(during["status"], before["status"])

        self.finished(when=datetime(2026, 9, 8, 14, 30, tzinfo=timezone.utc),
                      status="2 attachment(s) taken from the share")
        after = self.client.get("/settings/notes-mirror/state").get_json()
        self.assertNotEqual(after["at"], before["at"])
        self.assertEqual(after["status"], "2 attachment(s) taken from the share")

    def test_a_failed_round_is_an_answer_too(self):
        """It never stamps a new time, so a page watching only the clock would
        wait forever. The status moving is the other half of "it landed"."""
        self.with_broker()
        before = self.client.post(
            "/settings/notes-mirror/sync",
            headers={"X-Requested-With": "XMLHttpRequest"}).get_json()
        self.finished(status="Failed: PROPFIND /: HTTP 401")
        after = self.client.get("/settings/notes-mirror/state").get_json()
        self.assertEqual(after["at"], before["at"])
        self.assertNotEqual(after["status"], before["status"])


if __name__ == "__main__":
    unittest.main()
