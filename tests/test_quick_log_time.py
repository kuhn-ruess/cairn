"""The global "Log time" dialog (``t``): context preselects, the user overrides.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

What is pinned: the page the dialog opens on decides the preselected billing
project (a task through its folder's link, a billing project's page, a
customer's page through the user's most recent project there), no context
falls back to the last-used project, the project the form SENDS is where the
time lands, and a refused booking writes nothing.
"""
import re
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class QuickLogTimeTests(unittest.TestCase):

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
        self.Bucket = TimeBucket
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._wipe()
        self.user = User(username="qlog", email="qlog@test.invalid",
                         password_hash="x", level="team_admin").save()
        team = Team(name="T", admin=self.user, admins=[self.user]).save()
        self.user.team = team
        acme = Customer(team=team, name="Acme").save()
        self.globex = Customer(team=team, name="Globex").save()
        self.support = TimeTrackingProject(team=team, customer=acme,
                                           name="Support").save()
        self.web = TimeTrackingProject(team=team, customer=self.globex,
                                       name="Website").save()
        self.ops = TimeTrackingProject(team=team, customer=self.globex,
                                       name="Ops").save()
        self.user.tt_last_project = str(self.web.pk)
        self.user.tt_recent_projects = [str(self.ops.pk), str(self.web.pk)]
        self.user.save()
        folder = Item(owner=self.user, kind="project", title="Acme work",
                      time_project=self.support).save()
        self.task = Item(owner=self.user, kind="task", title="fix it",
                         parent=folder).save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _selected(self, **params):
        html = self.client.get("/time/quick", query_string=params).get_data(as_text=True)
        m = re.search(r'<option value="([0-9a-f]{24})" selected', html)
        return (m.group(1) if m else ""), html

    def _entries(self):
        return [e for b in self.Bucket.objects for e in b.entries]

    def test_a_task_preselects_its_folders_billing_project(self):
        pid, html = self._selected(item=str(self.task.pk))
        self.assertEqual(pid, str(self.support.pk))
        self.assertIn("Preselected from <strong>fix it</strong>", html)
        self.assertIn('name="link_item"', html)
        # The rest of the shortlist is still there to override with.
        self.assertIn(str(self.web.pk), html)

    def test_a_billing_projects_page_preselects_that_project(self):
        pid, _ = self._selected(tp=str(self.ops.pk))
        self.assertEqual(pid, str(self.ops.pk))

    def test_a_customers_page_preselects_the_most_recent_project_there(self):
        pid, html = self._selected(customer=str(self.globex.pk))
        self.assertEqual(pid, str(self.ops.pk))
        self.assertIn("Preselected from <strong>Globex</strong>", html)

    def test_no_context_falls_back_to_the_last_used_project(self):
        pid, html = self._selected()
        self.assertEqual(pid, str(self.web.pk))
        self.assertNotIn('name="link_item"', html)

    def test_an_unknown_item_is_just_no_context(self):
        pid, _ = self._selected(item="0" * 24)
        self.assertEqual(pid, str(self.web.pk))

    def test_booking_lands_on_the_project_sent_and_links_the_task(self):
        resp = self.client.post("/time/quick", data={
            "project_id": str(self.ops.pk), "duration": "1h30m",
            "note": "call", "link_item": "1", "item_id": str(self.task.pk)})
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["project"], "Globex › Ops")
        [entry] = self._entries()
        self.assertEqual(entry.minutes, 90)
        self.assertEqual(entry.project_id, str(self.ops.pk))
        self.assertEqual(entry.note, "call")
        self.assertEqual(entry.source_item_id, str(self.task.pk))

    def test_unticked_link_books_without_a_source(self):
        self.client.post("/time/quick", data={
            "project_id": str(self.web.pk), "duration": "30",
            "item_id": str(self.task.pk)})
        [entry] = self._entries()
        self.assertFalse(entry.source_item_id)

    def test_refusals_write_nothing(self):
        no_project = self.client.post("/time/quick", data={"duration": "1h"})
        self.assertEqual(no_project.status_code, 400)
        bad = self.client.post("/time/quick", data={
            "project_id": str(self.web.pk), "duration": "a while"})
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(bad.get_json()["ok"])
        self.assertEqual(self._entries(), [])

    def test_the_dialog_is_on_every_page_for_a_team_user(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="quickLogModal"', html)
        self.assertIn("quick-log.js", html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
