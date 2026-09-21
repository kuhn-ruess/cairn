"""Hovering the comment chip in a list opens that task's comments.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

What is pinned here is the server half — the panel is filled on OPEN, from a
partial of its own, so a list render costs no more than before: the chip only
carries the URL, and the endpoint answers with the thread. The permission rule
is the same one every other comment route uses (``can_be_viewed_by``), which
matters because the URL is guessable from any task id.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class CommentPeekTests(unittest.TestCase):

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
        self.Item = Item
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            self.user = User(username="peek-test", email="peek@test.invalid",
                             password_hash="x", level="premium").save()
            self.other = User(username="peek-other", email="other@test.invalid",
                              password_hash="x", level="premium").save()
            self.task = Item(owner=self.user, kind="task", title="a talked-about task").save()
            self.task.add_comment(self.user, "first thing")
            self.task.add_comment(self.user, "second thing")
            self.quiet = Item(owner=self.user, kind="task", title="a quiet task").save()
            self.theirs = Item(owner=self.other, kind="task", title="not mine").save()
            self.theirs.add_comment(self.other, "private business")
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    # -- the panel's content ---------------------------------------------

    def test_the_panel_carries_the_whole_thread_newest_first(self):
        html = self.client.get(f"/tasks/{self.task.pk}/comments/peek").get_data(as_text=True)
        self.assertIn("first thing", html)
        self.assertIn("second thing", html)
        self.assertLess(html.index("second thing"), html.index("first thing"))
        self.assertIn("2 comments", html)
        self.assertIn("a talked-about task", html)

    def test_a_task_of_someone_elses_is_not_readable(self):
        resp = self.client.get(f"/tasks/{self.theirs.pk}/comments/peek")
        self.assertEqual(resp.status_code, 404)

    def test_a_missing_task_is_a_404_not_a_crash(self):
        resp = self.client.get("/tasks/000000000000000000000000/comments/peek")
        self.assertEqual(resp.status_code, 404)

    # -- what the list ships ---------------------------------------------

    def test_the_list_chip_carries_the_url_and_nothing_else(self):
        """The row hands the client a URL, not the comments: a hundred rows
        with threads on them must not turn into a hundred rendered threads."""
        html = self.client.get("/tasks/inbox").get_data(as_text=True)
        self.assertIn(f"/tasks/{self.task.pk}/comments/peek", html)
        self.assertIn("comment-peek-trigger", html)
        self.assertNotIn("second thing", html)

    def test_a_task_without_comments_has_no_chip(self):
        html = self.client.get("/tasks/inbox").get_data(as_text=True)
        self.assertNotIn(f"/tasks/{self.quiet.pk}/comments/peek", html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
