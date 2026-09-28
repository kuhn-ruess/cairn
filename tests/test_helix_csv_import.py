"""A BMC Helix "Grid Data" export goes through the CSV import like a Jira one.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

What is pinned: the export is recognised by its columns (German header row,
BOM, "dd.mm.yyyy, HH:MM:SS" dates), the display id keys the row so a second
import updates instead of duplicating, the Helix status becomes a tag under
"Helix: Status" rather than Cairn's status, "Name des Zuständigen" assigns,
and the requester lands in the note once — never over the user's own note.
The rows below are made up; a real export never belongs in this tree.
"""
import io
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


HEADER = ('"Eingriff erforderlich","Priorität","ID anzeigen","Zieldatum",'
          '"SLM-Status","Vollständiger Name des Kunden","Name des Zuständigen",'
          '"Kurzbeschreibung","Status","Datum letzter Änderung",'
          '"Anwendungsebene","betroffene Infrastruktur","Aktuelles Enddatum",'
          '"Umgebung","Aktuelles Startdatun","Platform"')


def _row(prio, rid, due, requester, assignee, title, status, env="", start=""):
    cells = ["", prio, rid, due, "", requester, assignee, title, status,
             "28.09.2026, 13:58:41", "", "", "", env, start, ""]
    return ",".join('"' + c.replace('"', '""') + '"' for c in cells)


def _csv(*rows):
    return ("\ufeff" + "\n".join((HEADER,) + rows) + "\n").encode("utf-8")


@unittest.skipIf(connect is None, "mongoengine not installed")
class HelixCsvImportTests(unittest.TestCase):

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
        from application.models.team import Team
        from application.models.user import User
        Team.objects.delete()
        User.objects.delete()
        Item.all_objects.delete()

    def tearDown(self):
        self.ctx.pop()

    def setUp(self):
        from application.models.item import Item
        from application.models.team import Team
        from application.models.user import User
        self.Item = Item
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._wipe()
        self.user = User(username="hx", email="hx@test.invalid",
                         display_name="Hanna Import", password_hash="x",
                         level="team_admin").save()
        self.mate = User(username="mate", email="mate@test.invalid",
                         display_name="Max Mate", password_hash="x",
                         level="premium").save()
        team = Team(name="T", admin=self.user, admins=[self.user]).save()
        self.user.team = team
        self.user.save()
        self.mate.team = team
        self.mate.save()
        self.project = Item(owner=self.user, kind="project",
                            title="Helix", status="active").save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _import(self, data):
        return self.client.post(
            f"/projects/{self.project.pk}/import-csv",
            data={"file": (io.BytesIO(data), "grid.csv"), "mode": "sync"},
            content_type="multipart/form-data")

    def _tasks(self):
        return {t.external_ref: t for t in
                self.Item.objects(owner=self.user, kind="task")}

    def test_rows_become_helix_tasks(self):
        resp = self._import(_csv(
            _row("Mittel", "INC000000000001", "30.09.2026, 17:00:00",
                 "Erika Muster", "Max Mate", 'Karte "EMA" offline',
                 "Zugewiesen", env="PROD", start="29.09.2026, 08:00:00"),
            _row("Niedrig", "WO0000000000002", "", "Otto Normal",
                 "Hanna Import", "Plugin bauen", "In Bearbeitung"),
        ))
        self.assertEqual(resp.status_code, 302)
        tasks = self._tasks()
        self.assertEqual(set(tasks), {"INC000000000001", "WO0000000000002"})

        inc = tasks["INC000000000001"]
        self.assertEqual(inc.title, 'Karte "EMA" offline')
        self.assertEqual(inc.source, "helix")
        self.assertEqual(inc.external_id, "csv:INC000000000001")
        self.assertEqual(inc.priority, 3)
        self.assertEqual(inc.status, "open")
        self.assertIn("Zugewiesen", inc.tags)
        self.assertEqual(inc.assigned_to, self.mate)
        self.assertIsNotNone(inc.due_date)
        self.assertEqual((inc.due_date.day, inc.due_date.month), (30, 9))
        self.assertIsNotNone(inc.start_date)
        self.assertIn("- Customer: Erika Muster", inc.note)
        self.assertIn("- Environment: PROD", inc.note)

        wo = tasks["WO0000000000002"]
        self.assertEqual(wo.priority, 4)
        self.assertIsNone(wo.assigned_to)  # the importer's own ticket
        self.assertIn("In Bearbeitung", wo.tags)

        self.user.reload()
        groups = {t.name: t.parent for t in self.user.tags}
        self.assertEqual(groups.get("Zugewiesen"), "Helix: Status")

    def test_reimport_updates_and_keeps_the_users_note(self):
        self._import(_csv(_row("Mittel", "INC000000000001", "", "Erika Muster",
                               "Hanna Import", "Alt", "Zugewiesen")))
        task = self._tasks()["INC000000000001"]
        task.note = "my own notes"
        task.save()

        self._import(_csv(_row("Hoch", "INC000000000001", "", "Erika Muster",
                               "Hanna Import", "Neu", "In Bearbeitung")))
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        task = tasks["INC000000000001"]
        self.assertEqual(task.title, "Neu")
        self.assertEqual(task.priority, 2)
        self.assertEqual(task.note, "my own notes")
        self.assertIn("In Bearbeitung", task.tags)
        self.assertNotIn("Zugewiesen", task.tags)

    def test_helix_date_format(self):
        from datetime import datetime
        from application.views.projects import _parse_csv_date
        self.assertEqual(_parse_csv_date("28.09.2026, 13:58:41"),
                         datetime(2026, 9, 28, 13, 58, 41))


if __name__ == "__main__":
    unittest.main()
