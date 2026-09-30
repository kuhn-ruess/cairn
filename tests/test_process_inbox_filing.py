"""Processing the inbox files a task — and changes nothing else about it.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Why this exists: ``tasks/process_inbox.html`` is a deliberately TRIMMED version
of the inspector — title, note, project, status, priority, assignee, flag, the
three dates and the tags, and nothing else. ``_apply_form`` used to decide "this field never
reached the POST" from the user's inspector layout alone, which says nothing
about a second template that renders less. So every field the triage form leaves
out arrived as its empty default and was written over the task: filing a task
through the inbox run UNASSIGNED it (an absent ``assigned_to`` reads as an
explicit unassign), and dropped its estimate, location, repeat rule, follow-up
and target dates on the way.

That is not a cosmetic loss: task visibility is owner-OR-assignee, so the
assignee didn't just lose the task from their list — the API started answering
404 for it.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class ProcessInboxFilingTests(unittest.TestCase):

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

    @staticmethod
    def _wipe():
        from application.models.item import Item
        from application.models.user import User
        Item.all_objects.delete()
        User.objects.delete()

    def setUp(self):
        from datetime import datetime, timezone
        from application.models.item import Item
        from application.models.user import User
        with self.app.app_context():
            self._wipe()
            self.owner = User(username="pi-owner", email="pi-owner@test.invalid",
                              password_hash="x", level="premium").save()
            self.helper = User(username="pi-helper", email="pi-helper@test.invalid",
                              password_hash="x", level="premium").save()
            self.project = Item(owner=self.owner, kind="project", title="Cairn",
                                project_type="parallel", status="active").save()
            # A loose inbox task (no parent) that somebody is already working on,
            # carrying every field the triage form does NOT render.
            task = Item(owner=self.owner, kind="task", title="On the Table Feature",
                        estimated_minutes=90, location="Büro", waiting_on="Bastian",
                        repeat_every_days=7, repeat_unit="week",
                        review_interval_days=14, is_internal=True,
                        start_date=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
                        end_date=datetime(2026, 9, 30, 17, 0, tzinfo=timezone.utc))
            task.assign_to(self.helper, by_user=self.owner)
            self.task = task.save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.owner.pk)
            sess["_fresh"] = True

    def _reload(self):
        from application.models.item import Item
        with self.app.app_context():
            return Item.objects(pk=self.task.pk).first()

    def _file_it(self, **overrides):
        """POST exactly what ``tasks/process_inbox.html`` submits: the trimmed
        field set, filing the task under the project. Nothing else is sent —
        that absence is the whole point of this test."""
        data = {
            "title": "On the Table Feature",
            "note": "",
            "project": str(self.project.pk),
            "status": "open",
            "priority": "",
            "assigned_to": str(self.helper.pk),
            "defer_date": "",
            "due_date": "",
            "plan_date": "",
        }
        data.update(overrides)
        return self.client.post(
            f"/tasks/inbox/process/{self.task.pk}/save", data=data,
            follow_redirects=False,
        )

    # -- the bug -----------------------------------------------------------

    def test_filing_keeps_the_assignee(self):
        """The reported bug: the inbox run handed the task to nobody."""
        self._file_it()
        task = self._reload()
        self.assertIsNotNone(task.assigned_to, "filing unassigned the task")
        self.assertEqual(task.assigned_to.pk, self.helper.pk)

    def test_the_assignee_can_still_see_it_afterwards(self):
        """Visibility is owner-OR-assignee, which is why losing the assignee
        turned into a 404 on the assignee's API calls."""
        from application.models.user import User
        self._file_it()
        task = self._reload()
        with self.app.app_context():
            helper = User.objects(pk=self.helper.pk).first()
            self.assertTrue(task.can_be_viewed_by(helper))

    def test_filing_actually_files(self):
        """Guards the test above against a false green: a POST the form refused
        would also leave the assignee alone."""
        self.assertEqual(self._reload().parent, None)
        self._file_it()
        self.assertEqual(self._reload().parent.pk, self.project.pk)

    def test_filing_keeps_what_the_triage_form_never_showed(self):
        """Same mechanism, same fix: a field absent from THIS template is not
        an instruction to clear it."""
        self._file_it()
        task = self._reload()
        self.assertEqual(task.estimated_minutes, 90)
        self.assertEqual(task.location, "Büro")
        self.assertEqual(task.waiting_on, "Bastian")
        self.assertEqual(task.repeat_every_days, 7)
        self.assertEqual(task.review_interval_days, 14)
        self.assertTrue(task.is_internal)
        self.assertIsNotNone(task.start_date)
        self.assertIsNotNone(task.end_date)

    # -- and it still does its job ----------------------------------------

    def test_the_fields_it_does_show_still_apply(self):
        self._file_it(status="next", priority="2", flagged="y",
                      note="Triaged", due_date="2026-10-01T12:00",
                      tags=["home"])
        task = self._reload()
        self.assertEqual(task.status, "next")
        self.assertEqual(task.priority, 2)
        self.assertTrue(task.flagged)
        self.assertEqual(task.note, "Triaged")
        self.assertIsNotNone(task.due_date)
        self.assertEqual(task.tags, ["home"])

    # -- assigning from the triage form ------------------------------------

    def test_the_triage_form_can_assign(self):
        """Hand the task to a teammate while filing it."""
        from application.models.team import Team
        from application.models.user import User
        with self.app.app_context():
            team = Team(name="Inbox team", admin=self.owner).save()
            mate = User(username="pi-mate", email="pi-mate@test.invalid",
                        password_hash="x", level="premium", team=team).save()
        try:
            resp = self._file_it(assigned_to=str(mate.pk))
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(self._reload().assigned_to.pk, mate.pk)
        finally:
            with self.app.app_context():
                Team.objects(pk=team.pk).delete()

    def test_the_triage_form_can_unassign(self):
        """The picker is rendered now, so its empty value means what it means
        in the inspector: nobody."""
        self._file_it(assigned_to="")
        self.assertIsNone(self._reload().assigned_to)

    def test_a_hidden_inspector_section_does_not_block_triage(self):
        """The triage form renders its fields whatever the inspector layout
        says; hiding the Assignee or Status/Project section over there must not
        make the same fields here silently do nothing."""
        from application.models.user import User
        with self.app.app_context():
            User.objects(pk=self.owner.pk).update(
                inspector_hidden=["assigned_to", "status_project"])
            # Guard against a false green: the inspector really hides them.
            from application.services.inspector_layout import hidden_sections
            self.assertLessEqual({"assigned_to", "status_project"},
                                 hidden_sections(User.objects(pk=self.owner.pk).first()))
        self._file_it(assigned_to="")
        task = self._reload()
        self.assertIsNone(task.assigned_to)
        self.assertEqual(task.parent.pk, self.project.pk)

    def test_the_inspector_can_still_unassign(self):
        """The empty picker must keep meaning "unassign" where it is actually
        rendered — the fix narrows what counts as absent, it doesn't make the
        assignee unwritable."""
        resp = self.client.post(
            f"/tasks/{self.task.pk}/edit",
            data={
                "title": "On the Table Feature", "note": "",
                "project": str(self.project.pk), "status": "open",
                "priority": "", "defer_date": "", "due_date": "",
                "plan_date": "", "start_date": "", "end_date": "",
                "estimated_minutes": "", "location": "", "waiting_on": "",
                "followup_at": "", "followup_every_days": "",
                "review_interval_days": "", "repeat_every_days": "",
                "repeat_unit": "day", "repeat_monthday": "",
                "assigned_to": "", "owner": str(self.owner.pk),
            },
        )
        self.assertIn(resp.status_code, (200, 204, 302))
        self.assertIsNone(self._reload().assigned_to)


class ProcessInboxFieldListTests(unittest.TestCase):
    """``PROCESS_INBOX_FIELDS`` is a hand-written list of what one template
    renders, and a name that drifts out of it fails silently — the field stops
    being saved, or starts being cleared again. So both directions are pinned
    here, and neither needs a database."""

    def test_every_name_is_a_real_form_field(self):
        from application.forms import TaskForm
        from application.views.tasks import PROCESS_INBOX_FIELDS
        self.assertEqual(PROCESS_INBOX_FIELDS - set(TaskForm.__dict__), set())

    def test_it_lists_every_field_the_template_renders(self):
        """Add a field to the triage form and this fails until the list says
        so — otherwise the new field would simply not be saved."""
        import re
        from pathlib import Path
        from application.views.tasks import PROCESS_INBOX_FIELDS
        tpl = (Path(__file__).resolve().parent.parent
               / "application/templates/tasks/process_inbox.html").read_text()
        rendered = set(re.findall(r"\bform\.([a-z_]+)\(", tpl))
        self.assertIn("title", rendered)      # the regex found anything at all
        self.assertEqual(rendered, set(PROCESS_INBOX_FIELDS))

    def test_tags_use_the_inspector_token_field(self):
        """Not the old bare multi-select with a Ctrl-click hint."""
        from pathlib import Path
        tpl = (Path(__file__).resolve().parent.parent
               / "application/templates/tasks/process_inbox.html").read_text()
        self.assertIn('"data-tag-toggle": "1"', tpl)
        self.assertNotIn("Ctrl-click", tpl)


if __name__ == "__main__":
    unittest.main()
