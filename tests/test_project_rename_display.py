"""A renamed billing project has to read renamed everywhere it is shown.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

``TimeEntry.project_name`` is a snapshot taken when the time was booked. That
snapshot is what keeps a sheet readable after the project is deleted, so it
stays — but while the project is still there, its own name is the true one.
Stripping "Synced: " off every imported project renamed them in the management
list and nowhere else: the report, the CSV export, the day sheet and the trash
all kept printing the name the work was booked under, which reads as "the rename
did not save".

Every display path resolves through ``current_project_names`` now. Both halves
matter and both are tested here: the live name wins while the project exists,
and the snapshot is still there to fall back on when it does not.
"""
import unittest
from datetime import date, datetime, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None

WHEN = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)
DAY = date(2026, 8, 5)


@unittest.skipIf(connect is None, "mongoengine not installed")
class ProjectRenameDisplayTests(unittest.TestCase):

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
        cls.ctx = cls.app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()
        disconnect(alias="default")

    def setUp(self):
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeEntry, TimeTrackingProject)
        from application.models.user import User
        from application.services.timetracking import _append_entries
        self.Item, self.Bucket = Item, TimeBucket
        self.Team, self.User = Team, User
        self.Customer, self.Project = Customer, TimeTrackingProject
        self._wipe()

        self.user = User(username="rename-test", email="rn@test.invalid",
                         password_hash="x", level="team_admin").save()
        self.team = Team(name="Test team", admin=self.user,
                         admins=[self.user]).save()
        self.customer = Customer(team=self.team, name="Quartz Healthcare").save()
        # The name the import gave it, and the name somebody has since fixed.
        self.project = TimeTrackingProject(
            team=self.team, customer=self.customer, name="Synced: Beratung").save()
        self.task = Item(kind="task", title="A ticket", owner=self.user).save()

        def entry(minutes=60, **kw):
            return TimeEntry(project_id=str(self.project.id),
                             project_name="Synced: Beratung",
                             user_id=str(self.user.id), user_name="Tester",
                             minutes=minutes, spent_on=WHEN, **kw)

        _append_entries(self.team, self.customer, "2026-08-05", [
            entry(source_item_id=str(self.task.pk), source_kind="task"),
            entry(status="cancelled", minutes=30),
        ])
        # The rename that has to reach every reader below.
        self.project.name = "Beratung"
        self.project.save()

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        for doc in (TimeBucket, TimeTrackingProject, Customer, Item, Team, User):
            doc.objects.delete()

    # -- helpers ---------------------------------------------------------

    def _report(self):
        from application.services.timetracking import (
            build_report_rows, reachable_project_ids)
        return build_report_rows(
            team=self.team, scope_ids=reachable_project_ids(self.user, self.team),
            date_from=DAY, date_to=DAY, viewer=self.user)

    # -- the live name wins ----------------------------------------------

    def test_the_report_shows_the_new_name(self):
        """The one the ticket was raised about."""
        rows = self._report()
        self.assertEqual([r["project"] for r in rows], ["Beratung"])

    def test_the_day_sheet_shows_the_new_name(self):
        from application.services.timetracking import day_sheet
        sheet = day_sheet(self.user, DAY)
        self.assertEqual([e["project"] for e in sheet["entries"]], ["Beratung"])
        # …including the cancelled line the day offers to take back.
        self.assertEqual([e["project"] for e in sheet["removed"]], ["Beratung"])

    def test_the_trash_shows_the_new_name(self):
        from application.services.timetracking import cancelled_entries
        rows = cancelled_entries(team=self.team, viewer=self.user)
        self.assertEqual([r["project"] for r in rows], ["Beratung"])

    def test_the_task_inspector_shows_the_new_name(self):
        from application.services.timetracking import item_entries
        rows = item_entries(self.user, self.task)
        self.assertEqual([r["project_name"] for r in rows], ["Beratung"])

    # -- the snapshot is still the fallback -------------------------------

    def test_a_deleted_project_still_says_what_it_was(self):
        """Why the snapshot exists at all: it has to survive the project.

        Checked on the day sheet rather than the report — the report bounds
        itself by ``reachable_project_ids``, so a deleted project's time drops
        out of it entirely and there would be no name left to assert on."""
        from application.services.timetracking import day_sheet
        self.project.delete()
        sheet = day_sheet(self.user, DAY)
        self.assertEqual([e["project"] for e in sheet["entries"]],
                         ["Synced: Beratung"])

    def test_a_project_of_another_team_is_not_borrowed(self):
        """The lookup is per team; an id colliding across teams must not make
        one team's report print another team's name."""
        from application.services.timetracking import current_project_names
        other_admin = self.User(username="other", email="o@test.invalid",
                                password_hash="x", level="team_admin").save()
        other = self.Team(name="Other", admin=other_admin,
                          admins=[other_admin]).save()
        cust = self.Customer(team=other, name="Theirs").save()
        theirs = self.Project(team=other, customer=cust, name="Theirs").save()
        names = current_project_names(self.team)
        self.assertIn(str(self.project.id), names)
        self.assertNotIn(str(theirs.id), names)

    def test_no_teams_is_an_empty_map_not_a_query(self):
        from application.services.timetracking import current_project_names
        self.assertEqual(current_project_names([]), {})
        self.assertEqual(current_project_names(None), {})


if __name__ == "__main__":
    unittest.main()
