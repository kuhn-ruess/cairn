"""Booked time survives whatever happens to the task or project it was booked on.

Run with ``python -m unittest discover tests``. The database tests need a
MongoDB (``TestConfig`` points at ``bktodo_test``) and are skipped without one.

The rule: time entries are never deleted, trashed, unlinked or hidden because
the item they point at went into the trash, came back out of it, or was
deleted for good. They live in ``TimeBucket`` documents of the billing module
and keep a soft ``source_item_id``; nothing on the trash side may write to
them. While the item is away, the day sheet and the report still list the time
and say where its task went; a restore makes it show on the task again.

Pinned here:

* a static scan: the trash/delete code paths never touch the billing
  collections,
* trashing a task (single, bulk, shortcut ``d``, as part of a project)
  leaves every entry byte for byte as it was,
* restoring it (one, a selection, child before parent) shows the entries on
  the task again, still linked,
* deleting it for good (one, empty trash, retention purge, the REST API's
  project delete) keeps the entries, which then say the task is gone,
* the day sheet and the report keep listing the time with a marker,
* a stopwatch running on a task that went into the trash still stops and logs.
"""
import pathlib
import re
import unittest
from datetime import datetime, timedelta, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover: no mongoengine installed
    connect = None

ROOT = pathlib.Path(__file__).resolve().parent.parent / "application"

# The code that trashes, restores or deletes items. None of it has any business
# with booked time.
ITEM_DELETE_PATHS = (
    "services/trash.py",
    "views/trash.py",
    "services/cleanup.py",
)


class TrashCodeLeavesTimeAloneTests(unittest.TestCase):

    def test_trash_and_delete_code_never_names_the_time_collections(self):
        pat = re.compile(r"TimeBucket|TimeEntry|time_buckets|entries__")
        bad = []
        for rel in ITEM_DELETE_PATHS:
            for n, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
                if pat.search(line):
                    bad.append(f"{rel}:{n}: {line.strip()}")
        self.assertEqual(bad, [], "a trash/delete path reaches into booked time")


@unittest.skipIf(connect is None, "mongoengine not installed")
class TrashKeepsTimeTests(unittest.TestCase):

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
        cls._wipe()
        cls.ctx.pop()
        disconnect(alias="default")

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
        from application.services.timetracking import add_entry
        self.Item, self.Bucket = Item, TimeBucket
        self._wipe()
        self.user = User(username="keeper", email="keeper@test.invalid",
                         password_hash="x", level="team_admin").save()
        team = Team(name="T", admin=self.user, admins=[self.user]).save()
        self.user.team = team
        self.user.save()
        self.team = team
        acme = Customer(team=team, name="Acme").save()
        self.tp = TimeTrackingProject(team=team, customer=acme,
                                      name="Support").save()
        self.folder = Item(owner=self.user, kind="project", title="Acme work",
                           time_project=self.tp).save()
        self.task = Item(owner=self.user, kind="task", title="fix it",
                         parent=self.folder).save()
        self.sub = Item(owner=self.user, kind="task", title="sub step",
                        parent=self.task).save()
        self.day = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
        for minutes, item in ((60, self.task), (30, self.task), (15, self.sub)):
            add_entry(project=self.tp, user=self.user, minutes=minutes,
                      spent_on=self.day, note=f"{minutes}m", source_item=item)
        self.before = self._snapshot()
        self.client = self.app.test_client()
        from flask import g
        g.pop("_login_user", None)
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    # ---- helpers ----------------------------------------------------------

    def _snapshot(self):
        """Every entry as stored, keyed by id."""
        return {e["entry_id"]: e
                for b in self.Bucket.objects.as_pymongo()
                for e in b["entries"]}

    def assertTimeUntouched(self):
        self.assertEqual(self._snapshot(), self.before)

    def _task_minutes(self, item):
        from application.services.timetracking import item_entries
        return sorted(r["minutes"] for r in item_entries(self.user, item))

    def _reload(self, item):
        return self.Item.all_objects(pk=item.pk).first()

    def _day_rows(self):
        from application.services.timetracking import day_sheet
        return day_sheet(self.user, self.day.date())["entries"]

    def _report_rows(self):
        from application.services.timetracking import (
            build_report_rows, reachable_project_ids)
        return build_report_rows(
            team=self.team, scope_ids=reachable_project_ids(self.user, self.team),
            date_from=self.day.date(), date_to=self.day.date(), viewer=self.user)

    # ---- trashing ---------------------------------------------------------

    def test_trashing_a_task_keeps_its_time_and_restore_shows_it_again(self):
        r = self.client.post(f"/tasks/{self.task.pk}/delete",
                             data={"trash_via": "shortcut"},
                             headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(self._reload(self.task).trashed_at)
        self.assertTimeUntouched()
        # Still on the day sheet and in the report, saying where the task went.
        rows = self._day_rows()
        self.assertEqual(sorted(r["minutes"] for r in rows), [15, 30, 60])
        self.assertTrue(all(r["source_state"] == "trashed" for r in rows))
        self.assertEqual(len(self._report_rows()), 3)
        html = self.client.get("/time/day?d=2026-10-05").get_data(as_text=True)
        self.assertIn("task in trash", html)
        # The trashed task's read only page lists its time too.
        html = self.client.get(f"/trash/{self.task.pk}").get_data(as_text=True)
        self.assertIn("1h", html)

        r = self.client.post(f"/trash/{self.task.pk}/restore")
        self.assertEqual(r.status_code, 302)
        task = self._reload(self.task)
        self.assertIsNone(task.trashed_at)
        self.assertIsNone(self._reload(self.sub).trashed_at)
        self.assertTimeUntouched()
        self.assertEqual(self._task_minutes(task), [30, 60])
        self.assertEqual(self._task_minutes(self.sub), [15])
        self.assertTrue(all(r["source_state"] == "" for r in self._day_rows()))

    def test_bulk_restore_brings_the_time_back_child_first_too(self):
        from application.services.trash import trash_item
        # The task is thrown away on its own first, then its folder: two
        # deletions, two trash rows, and the task comes before the folder in
        # the database's natural order (it was created first).
        trash_item(self.task, source="web", via="Web: shortcut d")
        trash_item(self.folder, source="web", via="Web: bulk action")
        self.assertIsNotNone(self._reload(self.sub).trashed_at)
        self.assertTimeUntouched()
        self.assertEqual(len(self._day_rows()), 3)
        self.assertEqual(len(self._report_rows()), 3)
        # Child listed before its parent: the parent's restore must still
        # bring the child back in place, not re-file it at the top level.
        r = self.client.post("/trash/restore-selected", data={
            "item_ids": [str(self.task.pk), str(self.folder.pk)]})
        self.assertEqual(r.status_code, 302)
        for it in (self.folder, self.task, self.sub):
            self.assertIsNone(self._reload(it).trashed_at, it.title)
        self.assertEqual(self._reload(self.sub)._data["parent"].id, self.task.pk)
        self.assertEqual(self._reload(self.task)._data["parent"].id, self.folder.pk)
        self.assertTimeUntouched()
        self.assertEqual(self._task_minutes(self._reload(self.task)), [30, 60])

    def test_bulk_trash_route_keeps_time(self):
        r = self.client.post("/tasks/bulk", data={
            "action": "delete", "task_ids": [str(self.task.pk)]})
        self.assertIn(r.status_code, (200, 302))
        self.assertIsNotNone(self._reload(self.task).trashed_at)
        self.assertTimeUntouched()

    def test_a_member_still_sees_time_of_a_trashed_folder_in_the_report(self):
        """Report scope for a plain member comes from the containers they own;
        a trashed folder must not take its billing project's time out of it."""
        from application.services.timetracking import reachable_project_ids
        from application.services.trash import trash_item
        from application.models.user import User
        boss = User(username="boss", email="boss@test.invalid",
                    password_hash="x", level="team_admin").save()
        self.user.level = "premium"
        self.user.save()
        self.team.admin = boss
        self.team.admins = [boss]
        self.team.billers = []
        self.team.save()
        self.assertFalse(self.team.is_managed_by(self.user))
        self.assertIn(str(self.tp.pk), reachable_project_ids(self.user, self.team))
        trash_item(self.folder, source="web", via="Web: trash button")
        self.assertIn(str(self.tp.pk), reachable_project_ids(self.user, self.team))

    # ---- deleting for good ------------------------------------------------

    def test_permanent_delete_keeps_the_time_and_says_the_task_is_gone(self):
        from application.services.trash import trash_item
        trash_item(self.task, source="web", via="Web: trash button")
        r = self.client.post(f"/trash/{self.task.pk}/delete")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self._reload(self.task))
        self.assertTimeUntouched()
        rows = self._day_rows()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["source_state"] == "deleted" for r in rows))
        html = self.client.get("/time/day?d=2026-10-05").get_data(as_text=True)
        self.assertIn("task deleted", html)
        self.assertIn("fix it", html)   # the label snapshot still names it

    def test_empty_trash_and_retention_purge_keep_the_time(self):
        from application.services.trash import purge_trash, trash_item
        trash_item(self.task, source="web", via="Web: trash button")
        r = self.client.post("/trash/empty")
        self.assertEqual(r.status_code, 302)
        self.assertTimeUntouched()
        trash_item(self.folder, source="web", via="Web: trash button")
        self.Item.all_objects(pk=self.folder.pk).update(
            set__trashed_at=datetime.now(timezone.utc) - timedelta(days=90))
        self.assertEqual(purge_trash(self.user), 1)
        self.assertTimeUntouched()
        self.assertEqual(len(self._report_rows()), 3)

    def test_api_project_delete_keeps_the_time(self):
        from application.services.cleanup import delete_project_cascade
        delete_project_cascade(self.folder)
        self.assertIsNone(self._reload(self.task))
        self.assertTimeUntouched()
        self.assertEqual(len(self._day_rows()), 3)

    # ---- the stopwatch ----------------------------------------------------

    def test_a_timer_on_a_trashed_task_still_stops_and_logs(self):
        from application.services.timetracking import start_timer
        from application.services.trash import trash_item
        start_timer(self.user, project=self.tp, source_item=self.task)
        trash_item(self.task, source="web", via="Web: shortcut d")
        r = self.client.post("/time/timer/stop", headers={"X-Partial": "day"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self._snapshot()), 4)
        new = [e for k, e in self._snapshot().items() if k not in self.before]
        self.assertEqual(new[0]["source_item_id"], str(self.task.pk))

    # ---- the check / repair command ---------------------------------------

    def test_check_relinks_a_duplicate_tickets_time_only_with_apply(self):
        from application.services.timetracking import add_entry, item_entries
        from application.services.trash_time_check import check
        self.task.external_id = "zammad:77"
        self.task.save()
        # What the sync made while the first task sat in the trash.
        dupe = self.Item(owner=self.user, kind="task", title="fix it",
                         external_id="zammad:77", parent=self.folder).save()
        add_entry(project=self.tp, user=self.user, minutes=45,
                  spent_on=self.day, note="later", source_item=dupe)
        r = check(self.user)
        [d] = r["duplicates"]
        self.assertEqual((d["keep"], d["entries"], d["minutes"]),
                         (str(self.task.pk), 1, 45))
        self.assertEqual(r["relinked"], 0)
        self.assertEqual(len(item_entries(self.user, dupe)), 1)   # dry run
        n_before = len(self._snapshot())
        self.assertEqual(check(self.user, apply=True)["relinked"], 1)
        self.assertEqual(self._task_minutes(self.task), [30, 45, 60])
        self.assertEqual(item_entries(self.user, dupe), [])
        self.assertEqual(len(self._snapshot()), n_before)          # none lost
        self.assertEqual(check(self.user, apply=True)["relinked"], 0)
        self.assertIsNotNone(self._reload(dupe))                   # kept

    def test_check_lists_time_on_trashed_and_deleted_tasks(self):
        from application.services.trash import purge_item, trash_item
        from application.services.trash_time_check import check
        trash_item(self.sub, source="web", via="Web: trash button")
        self.assertEqual(check(self.user)["on_trashed"], [1, 15])
        purge_item(self._reload(self.sub))
        r = check(self.user)
        self.assertEqual((r["on_trashed"], r["on_deleted"]), ([0, 0], [1, 15]))
        self.assertTimeUntouched()


if __name__ == "__main__":
    unittest.main()
