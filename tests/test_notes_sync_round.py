"""services/notes_sync — a whole round, against a share that lives in memory.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

The parts worth driving end to end are the ones no unit can answer on its own,
because they are about what the round does the SECOND time:

* a document moved into another folder over there moves in Cairn too — and
  into the folder that directory means, created if Cairn never had it;
* an image added over there arrives on the note, and the round that follows
  writes nothing, adopts nothing and parks no conflict. A mirror that imports
  the same picture once per round is worse than one that never imported it.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40


class FakeDav:
    """A WebDAV share as a dict. Enough of the protocol for one round."""

    def __init__(self):
        self.files = {}          # path -> bytes
        self.dirs = {""}
        self.writes = []
        self.deletes = []

    # -- what the round calls --
    def list(self, path=""):
        from application.services.webdav import DavEntry
        out = []
        for p in sorted(self.dirs | set(self.files)):
            if not p:
                continue
            parent = p.rsplit("/", 1)[0] if "/" in p else ""
            if parent != path:
                continue
            is_dir = p in self.dirs
            out.append(DavEntry(path=p, is_dir=is_dir,
                                size=0 if is_dir else len(self.files[p]),
                                etag=self._tag(p)))
        return out

    def walk(self, path="", *, skip=()):
        found, queue = [], [path]
        while queue:
            for entry in self.list(queue.pop()):
                if entry.path.rsplit("/", 1)[-1] in skip:
                    continue
                found.append(entry)
                if entry.is_dir:
                    queue.append(entry.path)
        return found

    def read(self, path):
        from application.services.webdav import DavError
        if path not in self.files:
            raise DavError(f"GET {path}: HTTP 404", 404)
        return self.files[path]

    def write(self, path, data, content_type="application/octet-stream"):
        self.makedirs(path.rsplit("/", 1)[0] if "/" in path else "")
        self.files[path] = data
        self.writes.append(path)
        return self._tag(path)

    def delete(self, path):
        self.deletes.append(path)
        for p in [p for p in self.files if p == path or p.startswith(path + "/")]:
            del self.files[p]
        self.dirs -= {p for p in self.dirs if p == path or p.startswith(path + "/")}

    def makedirs(self, path):
        parts = [p for p in (path or "").split("/") if p]
        for i in range(len(parts)):
            self.dirs.add("/".join(parts[: i + 1]))

    # -- test-side helpers --
    def _tag(self, path):
        import hashlib
        if path in self.dirs:
            return ""
        return hashlib.sha1(self.files[path]).hexdigest()

    def move_file(self, src, dst):
        """What a user does in a file manager."""
        self.makedirs(dst.rsplit("/", 1)[0] if "/" in dst else "")
        self.files[dst] = self.files.pop(src)


@unittest.skipIf(connect is None, "mongoengine not installed")
class RoundTests(unittest.TestCase):

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
        self.Item, self.User = Item, User
        Item.all_objects.delete()
        User.objects(username="mirror-test").delete()
        self.user = User(username="mirror-test", email="m@example.com",
                         level="premium")
        self.user.set_password("x")
        self.user.save()
        self.dav = FakeDav()

    def tearDown(self):
        self.Item.all_objects.delete()
        self.User.objects(username="mirror-test").delete()

    # -- helpers --
    def folder(self, title, parent=None):
        return self.Item(owner=self.user, kind="folder", folder_domain="notes",
                         title=title, parent=parent, status="active").save()

    def note(self, title, body="", parent=None):
        return self.Item(owner=self.user, kind="note", title=title, note=body,
                         parent=parent).save()

    def sync(self):
        from application.services import notes_sync
        return notes_sync.sync_user(self.user, client=self.dav, trigger="cli")

    def reload(self, item):
        return self.Item.objects(pk=item.pk).first()

    # -- the cases --
    def test_a_note_moved_over_there_is_refiled_here(self):
        kunden = self.folder("Kunden")
        note = self.note("SNOW Integration", "- Ein Block\n", parent=kunden)
        self.sync()
        self.assertIn("Kunden/SNOW Integration.md", self.dav.files)

        team = self.folder("Team")
        self.dav.makedirs("Team")
        self.dav.move_file("Kunden/SNOW Integration.md",
                           "Team/SNOW Integration.md")
        self.sync()

        note = self.reload(note)
        self.assertEqual(str(note.parent.pk), str(team.pk))
        self.assertEqual(note.mirror_path, "Team/SNOW Integration.md")
        self.assertEqual(self.Item.objects(owner=self.user, kind="note").count(), 1)

    def test_even_into_a_folder_cairn_never_had(self):
        note = self.note("Bora", "- Ein Block\n")
        self.sync()
        self.dav.move_file("Bora.md", "Kunden/FMG/Bora.md")
        self.sync()

        note = self.reload(note)
        self.assertEqual(note.parent.title, "FMG")
        self.assertEqual(note.parent.parent.title, "Kunden")
        self.assertEqual(note.parent.owner.username, "mirror-test")

    def test_an_image_added_over_there_arrives_and_stays_once(self):
        fmg = self.folder("FMG")
        note = self.note("SNOW Integration", "- Vergleich:\n", parent=fmg)
        self.sync()

        # What DEVONthink does: the file beside the note, the link in the text.
        self.dav.write("FMG/_assets/Bild 2026.png", PNG)
        path = "FMG/SNOW Integration.md"
        self.dav.files[path] = self.dav.files[path].replace(
            b"- Vergleich:\n",
            b"- Vergleich:\n\t- ![Bild](_assets/Bild%202026.png)\n")

        stats = self.sync()
        self.assertEqual(stats.assets_read, 1)
        note = self.reload(note)
        self.assertEqual(len(note.note_images), 1)
        token = note.note_images[0].token
        self.assertIn(f"/notes/{note.pk}/note-image/{token}", note.note)
        self.assertIn(f"FMG/_assets/{note.pk}/{token}.png", self.dav.files)
        # The file the user put there is theirs: not touched, not deleted.
        self.assertIn("FMG/_assets/Bild 2026.png", self.dav.files)

        # And now the second round, which is where a duplicate would show up.
        before = len(self.dav.writes)
        stats = self.sync()
        self.assertEqual(stats.assets_read, 0)
        self.assertEqual(stats.notes_created, 0)
        self.assertEqual(stats.conflicts, 0)
        self.assertEqual(stats.unchanged, 1)
        self.assertEqual(self.dav.writes[before:], [])
        note = self.reload(note)
        self.assertEqual(len(note.note_images), 1)
        self.assertEqual(self.Item.objects(owner=self.user, kind="note").count(), 1)


if __name__ == "__main__":
    unittest.main()
