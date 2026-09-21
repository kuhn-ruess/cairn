"""Posting a comment can take labels off the item in the same submit.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Why this is worth a test: the handshake it serves is a two-step gesture the
user has to remember — answer in a comment, then take ``Review AI Work`` off,
and forgetting the second half silently strands the task. The rules that make
one submit safe are what's pinned here: only labels the item actually carries
go, a name it no longer carries is ignored rather than an error, and a comment
with nothing ticked never touches the labels.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class CommentDropLabelsTests(unittest.TestCase):

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
        from application.models.tag import Tag
        from application.models.user import User
        self.Item, self.User = Item, User
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            user = User(username="drop-test", email="drop@test.invalid",
                        password_hash="x", level="premium")
            user.tags = [Tag(name="Cairn-Entwicklung"), Tag(name="Review AI Work")]
            user.save()
            self.user = user
            self.task = Item(owner=user, kind="task", title="a handed-over task",
                             tags=["Cairn-Entwicklung", "Review AI Work"]).save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    # -- helpers ---------------------------------------------------------

    def _post(self, data, **kw):
        return self.client.post(f"/tasks/{self.task.pk}/comment", data=data, **kw)

    def _reload(self):
        with self.app.app_context():
            return self.Item.objects(pk=self.task.pk).first()

    # -- the feature -----------------------------------------------------

    def test_a_ticked_label_comes_off_with_the_comment(self):
        resp = self._post({"body": "answered — over to you",
                           "remove_tags": "Review AI Work"})
        self.assertEqual(resp.status_code, 302)
        task = self._reload()
        self.assertEqual(task.tags, ["Cairn-Entwicklung"])
        self.assertEqual([c.body for c in task.comments], ["answered — over to you"])

    def test_nothing_ticked_leaves_the_labels_alone(self):
        self._post({"body": "just a note"})
        task = self._reload()
        self.assertEqual(task.tags, ["Cairn-Entwicklung", "Review AI Work"])
        self.assertEqual(len(task.comments), 1)

    def test_a_label_the_item_no_longer_carries_is_ignored(self):
        """A form rendered before the label was taken off elsewhere must still
        post its comment rather than fail on a stale checkbox."""
        resp = self._post({"body": "still fine",
                           "remove_tags": ["gone-already", "review ai work"]})
        self.assertEqual(resp.status_code, 302)
        task = self._reload()
        self.assertEqual(task.tags, ["Cairn-Entwicklung"])  # case-insensitive match
        self.assertEqual(len(task.comments), 1)

    def test_an_empty_comment_removes_nothing(self):
        """The label only goes when the comment it belongs to actually lands."""
        self._post({"body": "", "remove_tags": "Review AI Work"})
        task = self._reload()
        self.assertEqual(task.tags, ["Cairn-Entwicklung", "Review AI Work"])
        self.assertEqual(task.comments, [])

    def test_the_inspector_is_told_what_went(self):
        data = self._post(
            {"body": "over to you", "remove_tags": "Review AI Work"},
            headers={"X-Inspector-Request": "1"},
        ).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["removed_tags"], ["Review AI Work"])
        # The remaining set, so the panel's label picker can be corrected —
        # a stale selection there would re-add the label on the next save.
        self.assertEqual(data["tags"], ["Cairn-Entwicklung"])

    def test_the_comment_box_offers_every_label_the_task_carries(self):
        html = self.client.get(f"/tasks/{self.task.pk}").get_data(as_text=True)
        self.assertIn('name="remove_tags" value="Review AI Work"', html)
        self.assertIn('name="remove_tags" value="Cairn-Entwicklung"', html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
