"""REST API v1 — the ``/notifications`` feed and ``/tokens``.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

This feed is what an agent polls to learn that somebody handed it work, so the
two properties worth pinning are the ones it would be useless without: an
``@mention`` reaches it even though a mention alone never puts the task in the
caller's ``_scope()`` (that is the whole reason the endpoint exists rather than
a ``/tasks?assigned=me`` query), and marking read claims ONLY the ids sent —
two readers share this feed, and one that swallowed the rest would silently eat
the other's work.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class _ApiHarness(unittest.TestCase):
    """App context, a wiped database and an agent account with a token."""

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
        self.client = self.app.test_client()
        self.agent = self._user("agent")
        self.human = self._user("human")
        _, self.token = self.agent.issue_api_token("test")

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        self.Item.all_objects.delete()
        self.User.objects.delete()

    # -- helpers ---------------------------------------------------------

    def _user(self, name):
        return self.User(username=name, email=f"{name}@test.invalid",
                         password_hash="x", level="premium").save()

    def _get(self, path):
        return self.client.get(path, headers={
            "Authorization": f"Bearer {self.token}"})

    def _post(self, path, payload=None):
        return self.client.post(path, json=payload or {}, headers={
            "Authorization": f"Bearer {self.token}"})

    def _mention(self, title):
        """A comment mentioning the agent on a task it does not own."""
        from application.services.notifications import notify_mention
        task = self.Item(owner=self.human, kind="task", title=title).save()
        notify_mention(self.agent, by_user=self.human, task=task)
        self.agent.save()
        self.agent.reload()
        return task

    def _feed(self, path="/api/v1/notifications"):
        r = self._get(path)
        self.assertEqual(r.status_code, 200)
        return r.json["items"]


class NotificationsApiTests(_ApiHarness):

    # -- reading ---------------------------------------------------------

    def test_a_token_is_required(self):
        self.assertEqual(self.client.get("/api/v1/notifications").status_code, 401)

    def test_a_mention_reaches_the_feed_with_the_task_it_is_about(self):
        task = self._mention("look at this")
        rows = self._feed()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "mention")
        self.assertEqual(rows[0]["item_id"], str(task.pk))
        self.assertFalse(rows[0]["read"])
        # The point of the feed: that task is invisible to a /tasks query.
        listed = self._get("/api/v1/tasks").json["items"]
        self.assertEqual(listed, [])

    def test_newest_first(self):
        self._mention("first")
        self._mention("second")
        self.assertEqual([r["text"].split("“")[-1].rstrip("”")
                          for r in self._feed()], ["second", "first"])

    def test_unread_narrows_to_what_nobody_claimed(self):
        self._mention("one")
        self._mention("two")
        rows = self._feed()
        self._post("/api/v1/notifications/read", {"ids": [rows[0]["id"]]})
        left = self._feed("/api/v1/notifications?unread=true")
        self.assertEqual([r["id"] for r in left], [rows[1]["id"]])

    def test_kind_filters(self):
        self._mention("mentioned")
        from application.services.notifications import notify_team_invite
        from application.models.team import Team
        notify_team_invite(self.agent, team=Team(name="T"),
                           inviter=self.human, link="/x")
        self.agent.save()
        self.assertEqual([r["kind"] for r in
                          self._feed("/api/v1/notifications?kind=mention")],
                         ["mention"])

    # -- claiming --------------------------------------------------------

    def test_marking_read_claims_only_the_ids_sent(self):
        self._mention("mine")
        self._mention("yours")
        rows = self._feed()
        r = self._post("/api/v1/notifications/read", {"ids": [rows[0]["id"]]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["unread"], 1)
        by_id = {x["id"]: x for x in self._feed()}
        self.assertTrue(by_id[rows[0]["id"]]["read"])
        self.assertFalse(by_id[rows[1]["id"]]["read"])

    def test_no_ids_marks_everything_read(self):
        self._mention("one")
        self._mention("two")
        r = self._post("/api/v1/notifications/read")
        self.assertEqual(r.json["unread"], 0)

    def test_an_empty_id_list_is_refused_rather_than_read_as_all(self):
        self._mention("one")
        r = self._post("/api/v1/notifications/read", {"ids": []})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._feed()[0]["read"], False)

    def test_a_read_only_token_may_not_claim(self):
        self._mention("one")
        _, ro = self.agent.issue_api_token("ro", read_only=True)
        r = self.client.post("/api/v1/notifications/read", json={},
                             headers={"Authorization": f"Bearer {ro}"})
        self.assertEqual(r.status_code, 403)


@unittest.skipIf(connect is None, "mongoengine not installed")
class TokenMintingTests(_ApiHarness):
    """POST /tokens exists so an account that only ever speaks over the API can
    issue itself a second credential — both browser paths need a session. The
    property that keeps it from being an escalation is that a read-only token
    cannot reach it at all."""

    def test_a_minted_token_works_and_is_returned_exactly_once(self):
        r = self._post("/api/v1/tokens", {"name": "second"})
        self.assertEqual(r.status_code, 201)
        raw = r.json["token"]
        probe = self.client.get("/api/v1/notifications",
                                headers={"Authorization": f"Bearer {raw}"})
        self.assertEqual(probe.status_code, 200)
        listed = self._get("/api/v1/tokens").json["items"]
        self.assertIn("second", [t["name"] for t in listed])
        self.assertNotIn("token", listed[-1])

    def test_a_read_only_token_cannot_mint(self):
        _, ro = self.agent.issue_api_token("ro", read_only=True)
        r = self.client.post("/api/v1/tokens", json={"name": "escalate"},
                             headers={"Authorization": f"Bearer {ro}"})
        self.assertEqual(r.status_code, 403)

    def test_a_minted_read_only_token_is_read_only(self):
        raw = self._post("/api/v1/tokens",
                         {"name": "ro", "read_only": True}).json["token"]
        r = self.client.post("/api/v1/notifications/read", json={},
                             headers={"Authorization": f"Bearer {raw}"})
        self.assertEqual(r.status_code, 403)

    def test_revoking_stops_the_token(self):
        made = self._post("/api/v1/tokens", {"name": "throwaway"}).json
        raw, tid = made["token"], made["id"]
        self.assertEqual(self.client.delete(
            f"/api/v1/tokens/{tid}",
            headers={"Authorization": f"Bearer {self.token}"}).status_code, 200)
        probe = self.client.get("/api/v1/notifications",
                                headers={"Authorization": f"Bearer {raw}"})
        self.assertEqual(probe.status_code, 401)


if __name__ == "__main__":
    unittest.main()
