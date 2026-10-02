"""REST API v1: status lists, the newest comment and ``awaiting_reply`` on
``GET /tasks``, and the comment order of ``GET /tasks/<id>``.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

What these pin down is what an inbox poller reads off ONE list call: which
statuses are in, who wrote the newest comment, and whether that was somebody
else. The newest comment is the newest by ``created_at``; an import can append
an older reply after a newer one, so list position is not the answer.
"""
import unittest
from datetime import datetime, timedelta, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover: no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class TaskListApiTests(unittest.TestCase):

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
        from flask import g
        from application.models.item import Comment, Item
        from application.models.user import User
        self.Item, self.User, self.Comment = Item, User, Comment
        g.pop("_viewable_container_pks", None)
        self._wipe()
        self.client = self.app.test_client()
        self.me = self._user("me")
        self.other = self._user("other")
        _, self.token = self.me.issue_api_token("test")
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        self.Item.all_objects.delete()
        self.User.objects.delete()

    # -- helpers ---------------------------------------------------------

    def _user(self, name):
        return self.User(username=name, email=f"{name}@test.invalid",
                         password_hash="x", level="premium",
                         display_name=name.title()).save()

    def _task(self, title, **kw):
        return self.Item(owner=self.me, assigned_to=self.me, kind="task",
                         title=title, **kw).save()

    def _comment(self, author=None, minutes_ago=0, external_author=None):
        return self.Comment(author=author, external_author=external_author,
                            body="x",
                            created_at=self.now - timedelta(minutes=minutes_ago))

    def _get(self, path):
        return self.client.get(path, headers={
            "Authorization": f"Bearer {self.token}"})

    def _titles(self, query):
        r = self._get(f"/api/v1/tasks?{query}")
        self.assertEqual(r.status_code, 200, r.json)
        return sorted(t["title"] for t in r.json["items"])

    def _row(self, title, query="compact=true"):
        for t in self._get(f"/api/v1/tasks?{query}").json["items"]:
            if t["title"] == title:
                return t
        self.fail(f"{title} not listed")

    # -- status / status_not ---------------------------------------------

    def _statuses(self):
        for st in ("open", "next", "waiting", "waiting_reply", "done"):
            self._task(st, status=st)

    def test_a_single_status_works_as_before(self):
        self._statuses()
        self.assertEqual(self._titles("status=next"), ["next"])

    def test_status_takes_a_comma_list(self):
        self._statuses()
        self.assertEqual(self._titles("status=open,next,waiting_reply"),
                         ["next", "open", "waiting_reply"])

    def test_status_not_excludes(self):
        self._statuses()
        self.assertEqual(self._titles("status_not=waiting,done"),
                         ["next", "open", "waiting_reply"])

    def test_status_not_and_open_both_apply(self):
        self._statuses()
        self.assertEqual(self._titles("open=true&status_not=waiting"),
                         ["next", "open", "waiting_reply"])

    def test_status_not_narrows_a_status_list(self):
        self._statuses()
        self.assertEqual(self._titles("status=open,next&status_not=next"),
                         ["open"])

    def test_an_unknown_status_in_a_list_is_a_400(self):
        for q in ("status=open,bogus", "status_not=bogus"):
            r = self._get(f"/api/v1/tasks?{q}")
            self.assertEqual(r.status_code, 400, q)
            self.assertIn("bogus", r.json["error"])

    # -- the newest comment in the list ----------------------------------

    def test_the_newest_comment_is_the_newest_by_time_not_by_position(self):
        self._task("t", comments=[
            self._comment(self.other, minutes_ago=1),
            self._comment(self.me, minutes_ago=30),  # appended late, older
        ])
        for query in ("compact=true", "compact=false"):
            row = self._row("t", query)
            self.assertEqual(row["last_comment_author_id"], str(self.other.pk))
            self.assertEqual(row["last_comment_author_name"], "Other")
            at = datetime.fromisoformat(row["last_comment_at"])
            # Mongo keeps milliseconds.
            self.assertLess(abs(at - (self.now - timedelta(minutes=1))),
                            timedelta(milliseconds=1))

    def test_an_external_comment_has_no_author_id_but_a_name(self):
        self._task("t", comments=[
            self._comment(external_author="Kunde GmbH")])
        row = self._row("t")
        self.assertIsNone(row["last_comment_author_id"])
        self.assertEqual(row["last_comment_author_name"], "Kunde GmbH")

    def test_no_comments_means_nulls(self):
        self._task("t")
        row = self._row("t")
        for key in ("last_comment_at", "last_comment_author_id",
                    "last_comment_author_name"):
            self.assertIsNone(row[key], key)

    def test_the_list_does_not_query_per_row_for_comment_authors(self):
        for i in range(15):
            self._task(f"t{i}", comments=[
                self._comment(self.other if i % 2 else self.me, minutes_ago=i)])
        from pymongo.collection import Collection
        original, calls = Collection.find, []

        def counting_find(coll, *a, **kw):
            calls.append(coll.name)
            return original(coll, *a, **kw)

        Collection.find = counting_find
        try:
            r = self._get("/api/v1/tasks?compact=true&kind=task")
        finally:
            Collection.find = original
        self.assertEqual(len(r.json["items"]), 15)
        self.assertLessEqual(len(calls), 6, calls)

    # -- awaiting_reply ---------------------------------------------------

    def _awaiting_setup(self):
        self._task("no comments")
        self._task("mine last", comments=[
            self._comment(self.other, minutes_ago=10),
            self._comment(self.me, minutes_ago=1)])
        self._task("theirs last", comments=[
            self._comment(self.me, minutes_ago=10),
            self._comment(self.other, minutes_ago=1)])
        self._task("external last", comments=[
            self._comment(self.me, minutes_ago=10),
            self._comment(external_author="Kunde", minutes_ago=1)])
        # By position mine is last, by time theirs is.
        self._task("theirs newer but earlier in the list", comments=[
            self._comment(self.other, minutes_ago=1),
            self._comment(self.me, minutes_ago=10)])
        # By position theirs is last, by time mine is.
        self._task("mine newer but earlier in the list", comments=[
            self._comment(self.me, minutes_ago=1),
            self._comment(self.other, minutes_ago=10)])

    def test_awaiting_reply_keeps_what_someone_else_spoke_last_on(self):
        self._awaiting_setup()
        self.assertEqual(self._titles("awaiting_reply=true"), [
            "external last", "no comments", "theirs last",
            "theirs newer but earlier in the list"])

    def test_awaiting_reply_is_per_caller(self):
        self._awaiting_setup()
        _, other_token = self.other.issue_api_token("t")
        for t in self.Item.objects:
            t.assigned_to = self.other
            t.save()
        r = self.client.get("/api/v1/tasks?awaiting_reply=true&assigned_to_me=true",
                            headers={"Authorization": f"Bearer {other_token}"})
        self.assertEqual(sorted(t["title"] for t in r.json["items"]), [
            "external last", "mine last",
            "mine newer but earlier in the list", "no comments"])

    def test_awaiting_reply_filters_before_the_limit(self):
        """Filtering after the page was cut would hand back a short page."""
        for i in range(5):
            self._task(f"mine {i}", comments=[self._comment(self.me)])
        for i in range(3):
            self._task(f"theirs {i}", comments=[self._comment(self.other)])
        r = self._get("/api/v1/tasks?awaiting_reply=true&limit=3")
        self.assertEqual(sorted(t["title"] for t in r.json["items"]),
                         ["theirs 0", "theirs 1", "theirs 2"])

    def test_the_inbox_call(self):
        self._task("waiting, theirs", status="waiting",
                   comments=[self._comment(self.other)])
        self._task("open, theirs", status="open",
                   comments=[self._comment(self.other)])
        self._task("open, mine", status="open",
                   comments=[self._comment(self.me)])
        self.assertEqual(self._titles(
            "assigned_to_me=true&status_not=waiting&awaiting_reply=true"
            "&kind=task&compact=true&limit=200"), ["open, theirs"])

    # -- detail ----------------------------------------------------------

    def test_detail_comments_are_oldest_first_by_time(self):
        t = self._task("t", comments=[
            self._comment(self.other, minutes_ago=1),
            self._comment(self.me, minutes_ago=30),
            self._comment(external_author="Kunde", minutes_ago=10)])
        body = self._get(f"/api/v1/tasks/{t.pk}").json
        self.assertEqual([c["author_name"] for c in body["comments"]],
                         ["Me", "Kunde", "Other"])
        self.assertEqual(body["last_comment_author_id"], str(self.other.pk))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
