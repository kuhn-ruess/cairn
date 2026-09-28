"""Posting a comment can book time in the same submit.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

What is pinned: the box names the project the time goes to and that is the
project it lands on, the comment becomes the entry's note, an empty field books
nothing, and an unreadable duration refuses the whole submit — the comment must
not land while the hours it was about silently don't.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class CommentLogTimeTests(unittest.TestCase):

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

    def tearDown(self):
        # One app context per test: Flask-Login caches the user on ``g``, and a
        # context shared across tests would carry the previous test's user.
        self.ctx.pop()

    @staticmethod
    def _wipe():
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        for doc in (TimeBucket, TimeTrackingProject, Customer, Team, User):
            doc.objects.delete()
        Item.all_objects.delete()

    def setUp(self):
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        self.Item, self.Bucket = Item, TimeBucket
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._wipe()
        self.user = User(username="clog", email="clog@test.invalid",
                         password_hash="x", level="team_admin").save()
        team = Team(name="T", admin=self.user, admins=[self.user]).save()
        self.user.team = team
        self.user.save()
        acme = Customer(team=team, name="Acme").save()
        self.project = TimeTrackingProject(team=team, customer=acme,
                                           name="Support").save()
        folder = Item(owner=self.user, kind="project", title="Acme work",
                      time_project=self.project).save()
        self.task = Item(owner=self.user, kind="task", title="fix it",
                         parent=folder).save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _post(self, data, **kw):
        return self.client.post(f"/tasks/{self.task.pk}/comment", data=data, **kw)

    def _entries(self):
        return [e for b in self.Bucket.objects for e in b.entries]

    def _comments(self):
        return self.Item.objects(pk=self.task.pk).first().comments

    def test_the_box_names_where_the_time_goes(self):
        html = self.client.get(f"/tasks/{self.task.pk}").get_data(as_text=True)
        self.assertIn('name="log_duration"', html)
        self.assertIn(f'name="log_project_id" value="{self.project.pk}"', html)
        self.assertIn("Acme › Support", html)

    def test_a_duration_books_an_entry_with_the_comment_as_note(self):
        resp = self._post({"body": "called the customer", "log_duration": "1h30m",
                           "log_project_id": str(self.project.pk)})
        self.assertEqual(resp.status_code, 302)
        [entry] = self._entries()
        self.assertEqual(entry.minutes, 90)
        self.assertEqual(entry.project_id, str(self.project.pk))
        self.assertEqual(entry.note, "called the customer")
        self.assertEqual(entry.source_item_id, str(self.task.pk))
        self.assertEqual(len(self._comments()), 1)

    def test_no_duration_books_nothing(self):
        self._post({"body": "just a note",
                    "log_project_id": str(self.project.pk)})
        self.assertEqual(self._entries(), [])
        self.assertEqual(len(self._comments()), 1)

    def test_an_unreadable_duration_refuses_the_comment_too(self):
        self._post({"body": "lost?", "log_duration": "a while"})
        self.assertEqual(self._entries(), [])
        self.assertEqual(self._comments(), [])

    def test_the_inspector_gets_the_booking_and_a_fresh_time_block(self):
        resp = self._post({"body": "done", "log_duration": "30"},
                          headers={"X-Inspector-Request": "1"})
        data = resp.get_json()
        self.assertEqual(data["logged_minutes"], 30)
        self.assertIn("Time tracking", data["time_block"])
        bad = self._post({"body": "x", "log_duration": "zz"},
                         headers={"X-Inspector-Request": "1"})
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(bad.get_json()["ok"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
