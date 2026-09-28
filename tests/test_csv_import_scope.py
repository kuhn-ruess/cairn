""""Complete tasks missing from the CSV" closes only what the file answers for.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

The regression: the close-missing step compared the file with every imported
task of the user, so a Helix export closed all Jira tasks, a Jira export all
Helix ones, and an export into one project the tickets of another. Pinned
here: Jira then Helix and Helix then Jira — in the same project and in two —
leave the other system's tasks open; two Jira exports into two projects leave
each other alone; missing tasks of the same project and system are still
closed, and marked as closed by the import. Also the repair
(``services.csv_reopen``) for what the old code closed. All data made up.
"""
import io
import unittest
from datetime import datetime, timedelta, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


def _jira(*keys):
    lines = ["Zusammenfassung,Vorgangsschlüssel:,Status"]
    lines += [f"Ticket {k},{k},offen" for k in keys]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _helix(*ids):
    lines = ['"Priorität","ID anzeigen","Kurzbeschreibung","Status"']
    lines += [f'"Mittel","{i}","Störung {i}","Zugewiesen"' for i in ids]
    return ("﻿" + "\n".join(lines) + "\n").encode("utf-8")


@unittest.skipIf(connect is None, "mongoengine not installed")
class CsvImportScopeTests(unittest.TestCase):

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

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls._wipe()
        disconnect(alias="default")

    @staticmethod
    def _wipe():
        from application.models.item import Item
        from application.models.user import User
        User.objects.delete()
        Item.all_objects.delete()

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        self.Item = Item
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._wipe()
        self.user = User(username="imp", email="imp@test.invalid",
                         display_name="Ida Import", password_hash="x",
                         level="premium").save()
        self.p1 = Item(owner=self.user, kind="project", title="Tickets",
                       status="active").save()
        self.p2 = Item(owner=self.user, kind="project", title="Other",
                       status="active").save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def tearDown(self):
        self.ctx.pop()

    def _import(self, project, data):
        return self.client.post(
            f"/projects/{project.pk}/import-csv",
            data={"file": (io.BytesIO(data), "x.csv"), "mode": "sync",
                  "archive_missing": "1"},
            content_type="multipart/form-data")

    def _status(self):
        return {t.external_ref: t.status for t in
                self.Item.objects(owner=self.user, kind="task")}

    def test_helix_after_jira_same_project_keeps_jira_open(self):
        self._import(self.p1, _jira("AB-1", "AB-2"))
        self._import(self.p1, _helix("INC0001"))
        self.assertEqual(self._status(),
                         {"AB-1": "open", "AB-2": "open", "INC0001": "open"})

    def test_jira_after_helix_same_project_keeps_helix_open(self):
        self._import(self.p1, _helix("INC0001", "WO0002"))
        self._import(self.p1, _jira("AB-1"))
        self.assertEqual(self._status(),
                         {"INC0001": "open", "WO0002": "open", "AB-1": "open"})

    def test_systems_in_two_projects_leave_each_other_alone(self):
        self._import(self.p1, _jira("AB-1"))
        self._import(self.p2, _helix("INC0001"))
        self._import(self.p1, _jira("AB-1"))
        self.assertEqual(self._status(), {"AB-1": "open", "INC0001": "open"})

    def test_same_system_in_another_project_is_left_alone(self):
        self._import(self.p1, _jira("AB-1"))
        self._import(self.p2, _jira("CD-1"))
        self.assertEqual(self._status(), {"AB-1": "open", "CD-1": "open"})

    def test_missing_ticket_of_this_file_is_still_closed(self):
        self._import(self.p1, _jira("AB-1", "AB-2"))
        self._import(self.p1, _helix("INC0001"))
        self._import(self.p1, _jira("AB-1"))
        self.assertEqual(self._status(),
                         {"AB-1": "open", "AB-2": "done", "INC0001": "open"})
        gone = self.Item.objects(external_ref="AB-2").first()
        self.assertEqual(gone.completed_via, "csv-import")

    def test_already_closed_task_is_not_counted_again(self):
        self._import(self.p1, _jira("AB-1", "AB-2"))
        t = self.Item.objects(external_ref="AB-2").first()
        t.mark_done(by=self.user)
        closed_at = self.Item.objects(pk=t.pk).first().completed_at
        resp = self._import(self.p1, _jira("AB-1"))
        self.assertEqual(resp.status_code, 302)
        t = self.Item.objects(pk=t.pk).first()
        self.assertEqual((t.completed_at, t.completed_via),
                         (closed_at, "user"))
        with self.client.session_transaction() as sess:
            flashes = " ".join(m for _, m in sess.get("_flashes", []))
        self.assertNotIn("no longer in the file", flashes)

    def test_reopen_finds_what_the_old_import_closed(self):
        from application.services.csv_reopen import closed_by_import, reopen
        self._import(self.p1, _jira("AB-1"))
        self._import(self.p1, _helix("INC0001"))
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        # What the old close-missing step did: a plain status write.
        for t in self.Item.objects(owner=self.user, kind="task"):
            t.status = "done"
            t.save()
        # Closed by hand long ago — outside the window, stays closed.
        old = self.Item(owner=self.user, kind="task", parent=self.p1,
                        title="old", external_id="csv:ZZ-9",
                        external_ref="ZZ-9", source="jira", status="done",
                        completed_at=before - timedelta(days=3),
                        completed_via="user").save()
        after = datetime.now(timezone.utc) + timedelta(seconds=1)

        found = closed_by_import(self.user, before, after)
        self.assertEqual(sorted(i.external_ref for i in found),
                         ["AB-1", "INC0001"])
        self.assertEqual([i.external_ref for i in closed_by_import(
            self.user, before, after, source="helix")], ["INC0001"])
        self.assertEqual(reopen(found), 2)
        self.assertEqual(self._status(),
                         {"AB-1": "open", "INC0001": "open", "ZZ-9": "done"})
        fresh = self.Item.objects(external_ref="AB-1").first()
        self.assertIsNone(fresh.completed_at)
        self.assertEqual(self.Item.objects(pk=old.pk).first().status, "done")


if __name__ == "__main__":
    unittest.main()
