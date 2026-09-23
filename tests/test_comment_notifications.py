"""Commenting on a task tells the people the task names.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Until this existed a comment notified nobody unless it carried an ``@name``:
an answer on a delegated task reached its assignee only if the writer happened
to spell their handle, so a reply could sit unread for a day with the bell
showing nothing. The set is deliberately narrow — the task's own cast, not
everyone who ever wrote in the thread — and the rules that keep it quiet are
what's pinned here: never the author, never twice for one comment, and never
for a thread mirrored in from somewhere else.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class CommentNotificationTests(unittest.TestCase):

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
        self.User = User
        self.Item = Item
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            # boss owns and delegated; agent is the assignee; bystander is in
            # the same team but has nothing to do with this task.
            from application.models.team import Team
            self.boss = User(username="cn-boss", email="cnb@test.invalid",
                             password_hash="x", level="team_admin").save()
            self.agent = User(username="cn-agent", email="cna@test.invalid",
                              password_hash="x", level="premium").save()
            self.bystander = User(username="cn-by", email="cny@test.invalid",
                                  password_hash="x", level="premium").save()
            # One team, so ``@cn-agent`` resolves at all and delegation is real.
            team = Team(name="cn-team", admin=self.boss).save()
            for u in (self.boss, self.agent, self.bystander):
                u.team = team
                u.save()
            self.task = Item(owner=self.boss, kind="task", title="Logwatch Wrapper",
                             assigned_to=self.agent, assigned_by=self.boss).save()
            self.solo = Item(owner=self.boss, kind="task", title="Nobody's job").save()

    def _kinds(self, user):
        with self.app.app_context():
            fresh = self.User.objects(pk=user.pk).first()
            return [n.kind for n in (fresh.notifications or [])]

    def _texts(self, user):
        with self.app.app_context():
            fresh = self.User.objects(pk=user.pk).first()
            return [n.text for n in (fresh.notifications or [])]

    def _comment(self, as_user, task, body):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(as_user.pk)
            sess["_fresh"] = True
        return client.post(f"/tasks/{task.pk}/comment", data={"body": body})

    # -- who hears about it ------------------------------------------------

    def test_the_assignee_is_told(self):
        """The leg that was missing: a reply on a delegated task."""
        self._comment(self.boss, self.task, "wie sieht es aus?")
        self.assertIn("comment", self._kinds(self.agent))

    def test_the_delegator_is_told_when_the_assignee_answers(self):
        self._comment(self.agent, self.task, "erledigt, siehe oben")
        self.assertIn("comment", self._kinds(self.boss))

    def test_the_owner_is_told_on_a_task_nobody_was_delegated(self):
        """Otherwise a comment on an undelegated task reaches nobody at all."""
        from application.services.notifications import notify_new_comment
        with self.app.app_context():
            task = self.Item.objects(pk=self.solo.pk).first()
            agent = self.User.objects(pk=self.agent.pk).first()
            notify_new_comment(task, "kleine Anmerkung", by_user=agent)
        self.assertIn("comment", self._kinds(self.boss))

    def test_the_entry_names_the_writer_and_the_task(self):
        self._comment(self.boss, self.task, "wie sieht es aus?")
        self.assertEqual(
            self._texts(self.agent),
            [f"{self.boss} commented on “Logwatch Wrapper”"],
        )

    def test_somebody_with_nothing_to_do_with_it_is_not_told(self):
        self._comment(self.boss, self.task, "wie sieht es aus?")
        self.assertEqual(self._kinds(self.bystander), [])

    # -- what keeps it quiet ----------------------------------------------

    def test_the_author_is_never_told_about_their_own_comment(self):
        self._comment(self.boss, self.task, "Notiz an mich")
        self.assertEqual(self._kinds(self.boss), [])

    def test_a_mentioned_assignee_is_told_once_not_twice(self):
        """Mentioned AND the assignee is one notification — the specific one."""
        self._comment(self.boss, self.task, "@cn-agent schaust du?")
        self.assertEqual(self._kinds(self.agent), ["mention"])

    def test_an_imported_comment_notifies_nobody(self):
        """A Zammad/Todoist thread is appended straight to ``comments`` and
        never goes through the notifying path."""
        from application.models.item import Comment
        with self.app.app_context():
            task = self.Item.objects(pk=self.task.pk).first()
            task.comments.append(Comment(external_author="Kunde", body="Antwort",
                                         external=True))
            task.save()
        self.assertEqual(self._kinds(self.agent), [])

    def test_a_comment_without_an_author_is_a_no_op_not_a_crash(self):
        from application.services.notifications import notify_new_comment
        with self.app.app_context():
            task = self.Item.objects(pk=self.task.pk).first()
            notify_new_comment(task, "irgendwas", by_user=None)
        self.assertEqual(self._kinds(self.agent), [])

    # -- the API writes comments too --------------------------------------

    def test_the_api_notifies_exactly_as_the_form_does(self):
        with self.app.app_context():
            boss = self.User.objects(pk=self.boss.pk).first()
            _tok, raw = boss.issue_api_token("test")
        client = self.app.test_client()
        resp = client.post(
            f"/api/v1/tasks/{self.task.pk}/comments",
            json={"body": "per API geantwortet"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        self.assertEqual(resp.status_code, 201)
        self.assertIn("comment", self._kinds(self.agent))

    # -- the feed can still be filtered ------------------------------------

    def test_a_kind_filter_is_an_allow_list_so_comment_must_be_asked_for(self):
        """A poller on ``assigned,mention`` keeps the feed it had."""
        self._comment(self.boss, self.task, "wie sieht es aus?")
        from application.models.notification import Notification
        with self.app.app_context():
            fresh = self.User.objects(pk=self.agent.pk).first()
            rows = list(fresh.notifications or [])
        wanted = {"assigned", "mention"}
        self.assertEqual([n for n in rows if n.kind in wanted], [])
        self.assertTrue([n for n in rows if n.kind in wanted | {"comment"}])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
