"""A re-import leaves the user's status alone and shows the ticket's own.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

The regression: every CSV import re-ran the tag rules over every tag, so a
"Jira: Status" tag with a status mapping snapped a ticket already in Cairn
back to the mapped status on each import — whatever the user had set by hand
since. A generic CSV's status column overwrote it outright. Pinned here: a
ticket already here keeps its status while the ticket system's status is
unchanged; only a status the ticket system just moved to may fire its rule;
new tickets still get their rules; and the ticket system's status is kept on
the task (``external_status``) and shown. All data made up.
"""
import io
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


def _jira(*rows):
    lines = ["Zusammenfassung,Vorgangsschlüssel:,Status"]
    lines += [f"Ticket {k},{k},{s}" for k, s in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _helix(*rows):
    lines = ['"Priorität","ID anzeigen","Kurzbeschreibung","Status"']
    lines += [f'"Mittel","{i}","Störung {i}","{s}"' for i, s in rows]
    return ("﻿" + "\n".join(lines) + "\n").encode("utf-8")


@unittest.skipIf(connect is None, "mongoengine not installed")
class CsvImportStatusTests(unittest.TestCase):

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
        from application.models.tag import Tag
        from application.models.user import User
        self.Item = Item
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._wipe()
        self.user = User(username="imp", email="imp@test.invalid",
                         display_name="Ida Import", password_hash="x",
                         level="premium",
                         tags=[Tag(name="In Arbeit", maps_to_status="next"),
                               Tag(name="Geschlossen", maps_to_status="done")],
                         ).save()
        self.p1 = Item(owner=self.user, kind="project", title="Tickets",
                       status="active").save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def tearDown(self):
        self.ctx.pop()

    def _import(self, data):
        resp = self.client.post(
            f"/projects/{self.p1.pk}/import-csv",
            data={"file": (io.BytesIO(data), "x.csv"), "mode": "sync"},
            content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)

    def _task(self, ref):
        return self.Item.objects(owner=self.user, external_ref=ref).first()

    def _set_status(self, ref, status):
        t = self._task(ref)
        t.status = status
        t.save()

    def test_new_ticket_gets_its_rule(self):
        self._import(_jira(("AB-1", "In Arbeit")))
        t = self._task("AB-1")
        self.assertEqual((t.status, t.external_status), ("next", "In Arbeit"))

    def test_reimport_keeps_status_set_by_hand(self):
        self._import(_jira(("AB-1", "In Arbeit"), ("AB-2", "Offen")))
        self._set_status("AB-1", "waiting")
        self._set_status("AB-2", "someday")
        self._import(_jira(("AB-1", "In Arbeit"), ("AB-2", "Offen")))
        self.assertEqual(self._task("AB-1").status, "waiting")
        self.assertEqual(self._task("AB-2").status, "someday")

    def test_reimport_refreshes_the_ticket_systems_status(self):
        self._import(_helix(("INC0001", "Zugewiesen")))
        self._set_status("INC0001", "waiting")
        self._import(_helix(("INC0001", "Ausstehend")))
        t = self._task("INC0001")
        self.assertEqual((t.status, t.external_status),
                         ("waiting", "Ausstehend"))
        self.assertIn("Ausstehend", t.tags)
        self.assertNotIn("Zugewiesen", t.tags)

    def test_a_status_the_ticket_just_moved_to_fires_its_rule(self):
        self._import(_jira(("AB-1", "Offen")))
        self._set_status("AB-1", "waiting")
        self._import(_jira(("AB-1", "Geschlossen")))
        self.assertEqual(self._task("AB-1").status, "done")

    def test_unmapped_status_change_leaves_status_alone(self):
        self._import(_jira(("AB-1", "In Arbeit")))
        self._set_status("AB-1", "waiting")
        self._import(_jira(("AB-1", "Review")))
        t = self._task("AB-1")
        self.assertEqual((t.status, t.external_status), ("waiting", "Review"))

    def test_generic_csv_status_column_only_on_creation(self):
        data = b"title,csv_id,status\nPlain,P-1,next\n"
        self._import(data)
        self.assertEqual(self._task("P-1").status, "next")
        self._set_status("P-1", "waiting")
        self._import(data)
        self.assertEqual(self._task("P-1").status, "waiting")

    def test_list_shows_the_ticket_systems_status(self):
        self._import(_helix(("INC0001", "Zugewiesen")))
        html = self.client.get(f"/projects/{self.p1.pk}").get_data(as_text=True)
        self.assertIn('<span class="source-chip-status">Zugewiesen</span>', html)


if __name__ == "__main__":
    unittest.main()
