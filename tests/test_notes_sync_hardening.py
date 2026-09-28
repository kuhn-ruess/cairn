"""services/notes_sync — editing on BOTH sides must never lose text.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

What is pinned down here is what happens when the two sides move at the same
time, or when the share hands over something that must not be taken at face
value:

* a note saved in Cairn while a round runs is not overwritten by the round's
  stale copy — the next round sees two changed sides and keeps both;
* a file changed over there after the round listed it is not overwritten
  (``If-Match``), and a new file never lands on one somebody put there;
* a file that is not UTF-8, or that is EMPTY, never replaces a note's text;
* a rename that only changes case does not delete the only copy;
* opening a note pulls its file first, and never trashes it;
* the scratchpad is ``Scratchpad.md`` at the root, both ways, and its editor
  refuses to overwrite a version it has not seen.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None

from test_notes_sync_round import FakeDav


class LiveDav(FakeDav):
    """The fake share, plus the Depth-0 PROPFIND a live sync uses and a hook
    that runs just before a write lands (another client, racing us)."""

    def __init__(self):
        super().__init__()
        self.before_write = None
        self.on_read = None

    def stat(self, path):
        from application.services.webdav import DavEntry
        if path in self.files:
            return DavEntry(path=path, is_dir=False, size=len(self.files[path]),
                            etag=self._tag(path))
        if path in self.dirs:
            return DavEntry(path=path, is_dir=True)
        return None

    def read(self, path):
        data = super().read(path)
        if self.on_read:
            hook, self.on_read = self.on_read, None
            hook(path)
        return data

    def write(self, path, data, content_type="application/octet-stream", **kw):
        if self.before_write:
            hook, self.before_write = self.before_write, None
            hook(path)
        return super().write(path, data, content_type, **kw)


def file_body(raw: bytes) -> str:
    from application.services.notes_sync import parse
    return parse(raw.decode("utf-8"))[1]


@unittest.skipIf(connect is None, "mongoengine not installed")
class HardeningTests(unittest.TestCase):

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
        from application.models.user import User
        from application.services import notes_sync
        self.Item, self.User, self.ns = Item, User, notes_sync
        # No Redis in a test: the locks are a courtesy, the preconditions are
        # what is being tested.
        self._redis = notes_sync._redis_or_none
        notes_sync._redis_or_none = lambda: None
        Item.all_objects.delete()
        User.objects(username="harden-test").delete()
        self.user = User(username="harden-test", email="h@example.com",
                         level="premium", notes_mirror_enabled=True,
                         notes_mirror_url="https://nas.example/notes")
        self.user.set_password("x")
        self.user.save()
        self.dav = LiveDav()

    def tearDown(self):
        self.ns._redis_or_none = self._redis
        self.Item.all_objects.delete()
        self.User.objects(username="harden-test").delete()

    def note(self, title, body=""):
        return self.Item(owner=self.user, kind="note", title=title,
                         note=body).save()

    def sync(self):
        return self.ns.sync_user(self.user, client=self.dav, trigger="cli")

    def reload(self, item):
        return self.Item.objects(pk=item.pk).first()

    def edit_file(self, path, body):
        """Rewrite a file's body over there, keeping its front matter."""
        raw = self.dav.files[path].decode("utf-8")
        head = raw[: raw.index("\n---\n") + 5]
        self.dav.files[path] = (head + "\n" + body).encode("utf-8")

    # -- both sides moving at once --

    def test_a_note_saved_during_the_round_is_not_overwritten(self):
        note = self.note("Race", "- Anfang\n")
        self.sync()
        self.edit_file("Race.md", "- Von drüben\n")

        # The user saves while the round is reading the file.
        def user_saves(_path):
            n = self.reload(note)
            n.note = "- In Cairn getippt\n"
            n.save()
        self.dav.on_read = user_saves
        stats = self.sync()
        self.assertTrue(any("edited in Cairn" in e for e in stats.errors), stats.errors)
        self.assertEqual(self.reload(note).note, "- In Cairn getippt\n")

        # The next round sees both sides changed: both texts survive.
        stats = self.sync()
        self.assertEqual(stats.conflicts, 1)
        self.assertIn("In Cairn getippt", file_body(self.dav.files["Race.md"]))
        parked = [p for p in self.dav.files if "(conflict" in p]
        self.assertEqual(len(parked), 1)
        self.assertIn("Von drüben", self.dav.files[parked[0]].decode())

    def test_a_file_changed_after_the_listing_is_not_overwritten(self):
        note = self.note("Push", "- eins\n")
        self.sync()
        n = self.reload(note)
        n.note = "- zwei\n"
        n.save()

        def other_client_writes(path):
            self.edit_file(path, "- drei (von drüben)\n")
        self.dav.before_write = other_client_writes
        stats = self.sync()
        self.assertTrue(any("changed over there" in e for e in stats.errors), stats.errors)
        self.assertIn("drei (von drüben)", file_body(self.dav.files["Push.md"]))

        stats = self.sync()                     # now a conflict, both kept
        self.assertEqual(stats.conflicts, 1)
        self.assertIn("zwei", file_body(self.dav.files["Push.md"]))
        parked = [p for p in self.dav.files if "(conflict" in p]
        self.assertIn("drei (von drüben)", self.dav.files[parked[0]].decode())

    def test_a_new_note_does_not_land_on_a_file_that_appeared(self):
        self.note("Neu", "- aus Cairn\n")

        def someone_puts_a_file(path):
            self.dav.files[path] = b"fremder Text\n"
        self.dav.before_write = someone_puts_a_file
        self.sync()
        self.assertEqual(self.dav.files["Neu.md"], b"fremder Text\n")

    # -- what the share hands over --

    def test_a_file_that_is_not_utf8_is_left_alone(self):
        note = self.note("Umlaut", "- Grüße\n")
        self.sync()
        latin = "---\ncairn-id: x\n---\n- Grüße aus Latin-1\n".encode("latin-1")
        self.dav.files["Umlaut.md"] = latin
        stats = self.sync()
        self.assertTrue(any("not UTF-8" in e for e in stats.errors), stats.errors)
        self.assertEqual(self.reload(note).note, "- Grüße\n")
        self.assertEqual(self.dav.files["Umlaut.md"], latin)

    def test_a_byte_order_mark_is_not_a_change(self):
        note = self.note("BOM", "- a\n")
        self.sync()
        self.dav.files["BOM.md"] = b"\xef\xbb\xbf" + self.dav.files["BOM.md"]
        stats = self.sync()
        self.assertEqual(stats.conflicts, 0)
        self.assertEqual(stats.notes_created, 0)
        self.assertEqual(self.reload(note).note, "- a\n")

    def test_an_empty_file_never_empties_a_note(self):
        note = self.note("Leer", "- wichtiger Text\n")
        self.sync()
        self.dav.files["Leer.md"] = b""          # a half-finished upload
        self.sync()
        self.assertEqual(self.reload(note).note, "- wichtiger Text\n")
        self.assertIn("wichtiger Text", file_body(self.dav.files["Leer.md"]))

    def test_a_rename_that_only_changes_case_keeps_the_file(self):
        note = self.note("plan", "- x\n")
        self.sync()
        n = self.reload(note)
        n.title = "Plan"
        n.save()
        self.sync()
        self.assertEqual(self.dav.deletes, [])
        self.assertIn("title: Plan", self.dav.files["plan.md"].decode())
        self.sync()
        self.assertIsNone(self.reload(note).trashed_at)

    # -- live: on open --

    def test_opening_a_note_pulls_its_file_first(self):
        note = self.note("Offen", "- alt\n")
        self.sync()
        self.edit_file("Offen.md", "- neu von drüben\n")
        item = self.reload(note)
        self.assertEqual(self.ns.sync_note(item, client=self.dav), "changed")
        self.assertIn("neu von drüben", item.note)           # in place
        self.assertIn("neu von drüben", self.reload(note).note)

    def test_opening_a_note_whose_file_is_gone_does_not_trash_it(self):
        note = self.note("Weg", "- x\n")
        self.sync()
        del self.dav.files["Weg.md"]
        self.assertEqual(self.ns.sync_note(self.reload(note), client=self.dav), "ok")
        self.assertIsNone(self.reload(note).trashed_at)

    def test_a_queued_push_writes_just_that_note(self):
        note = self.note("Gespeichert", "- v1\n")
        self.sync()
        n = self.reload(note)
        n.note = "- v2\n"
        n.save()
        self.dav.writes = []
        self.assertEqual(self.ns.sync_note(self.reload(note), client=self.dav), "ok")
        self.assertEqual(self.dav.writes, ["Gespeichert.md"])
        self.assertIn("v2", file_body(self.dav.files["Gespeichert.md"]))

    # -- the scratchpad --

    def pad(self):
        return self.User.objects(pk=self.user.pk).first().scratchpad

    def set_pad(self, text):
        from datetime import datetime, timezone
        self.User.objects(pk=self.user.pk).update(
            set__scratchpad=text,
            set__scratchpad_updated_at=datetime.now(timezone.utc))

    def test_the_pad_is_mirrored_both_ways(self):
        self.set_pad("- Idee\n- noch eine\n")
        self.sync()
        self.assertEqual(self.dav.files["Scratchpad.md"], b"- Idee\n- noch eine\n")

        self.dav.files["Scratchpad.md"] = b"- Idee\n- auf dem NAS ergaenzt\n"
        stats = self.sync()
        self.assertEqual(stats.scratchpad, "pulled")
        self.assertEqual(self.pad(), "- Idee\n- auf dem NAS ergaenzt\n")
        self.assertEqual(stats.notes_created, 0)      # never adopted as a note

        stats = self.sync()
        self.assertEqual(stats.scratchpad, "")        # settled

    def test_the_pad_keeps_both_texts_on_a_conflict(self):
        self.set_pad("- a\n")
        self.sync()
        self.set_pad("- a\n- hier\n")
        self.dav.files["Scratchpad.md"] = b"- a\n- dort\n"
        stats = self.sync()
        self.assertEqual(stats.scratchpad, "conflict")
        self.assertEqual(self.dav.files["Scratchpad.md"], b"- a\n- hier\n")
        parked = [p for p in self.dav.files if p.startswith("Scratchpad (conflict")]
        self.assertEqual(len(parked), 1)
        self.sync()                                   # comes back as a note
        self.assertTrue(self.Item.objects(title__startswith="Scratchpad (conflict").first())

    def test_deleting_the_pad_file_does_not_empty_the_pad(self):
        self.set_pad("- bleibt\n")
        self.sync()
        del self.dav.files["Scratchpad.md"]
        self.sync()
        self.assertEqual(self.pad(), "- bleibt\n")
        self.assertEqual(self.dav.files["Scratchpad.md"], b"- bleibt\n")

    def test_a_note_called_scratchpad_makes_room(self):
        note = self.note("Scratchpad", "- eine Notiz\n")
        self.Item.objects(pk=note.pk).update(set__mirror_path="Scratchpad.md")
        # As if an older round had filed it there.
        self.dav.files["Scratchpad.md"] = self.ns.render(
            self.reload(note), "Scratchpad.md").encode()
        self.Item.objects(pk=note.pk).update(
            set__mirror_hash=self.ns._hash(self.dav.files["Scratchpad.md"].decode()),
            set__mirror_tag=self.dav._tag("Scratchpad.md"))
        self.set_pad("- Pad\n")
        self.sync()
        self.assertEqual(self.reload(note).mirror_path, "Scratchpad (2).md")
        self.assertIn("eine Notiz", self.dav.files["Scratchpad (2).md"].decode())
        self.sync()
        self.assertEqual(self.dav.files["Scratchpad.md"], b"- Pad\n")
        self.assertIsNone(self.reload(note).trashed_at)

    def test_the_pad_editor_refuses_a_stale_version(self):
        from application.views.scratchpad import pad_stamp
        self.set_pad("- a\n")
        stale = pad_stamp(self.User.objects(pk=self.user.pk).first())
        self.set_pad("- vom NAS\n")                   # a pull landed meanwhile
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True
        resp = client.post("/scratchpad/save",
                           data={"note": "- überschreibt\n", "base_stamp": stale},
                           headers={"X-Partial": "1"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["note"], "- vom NAS\n")
        self.assertEqual(self.pad(), "- vom NAS\n")


class ClientPathTests(unittest.TestCase):
    """webdav.DavClient never addresses anything outside its folder."""

    def test_a_path_that_climbs_out_is_refused(self):
        from application.services.webdav import DavClient, DavError
        c = DavClient("https://nas.example/home/My%20Notes")
        with self.assertRaises(DavError):
            c._url("a/../../etc/passwd")

    def test_hrefs_outside_the_root_are_dropped(self):
        from application.services.webdav import DavClient
        c = DavClient("https://nas.example/home/My%20Notes")
        self.assertEqual(c._relative("/home/My%20Notes/a/b.md"), "a/b.md")
        self.assertEqual(c._relative("/home/My%20Notes/"), "")
        self.assertIsNone(c._relative("/home/My%20Notes2/x.md"))
        self.assertIsNone(c._relative("/home/My%20Notes/a/../../x.md"))


if __name__ == "__main__":
    unittest.main()
