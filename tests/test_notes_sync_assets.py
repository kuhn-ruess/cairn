"""services/notes_sync — attachments coming back FROM the share.

An image dropped into a mirrored document is two things over there: a file
beside it and a relative link in the text. Neither means anything in the app
until the bytes sit on the note — and if that file sits in the note's own
folder, the next write-back deletes it as one no note claims, so the picture
is lost in both places.

The folder is not part of the rule: DEVONthink writes ``_resources/``,
Obsidian an attachments folder of the vault's choosing, Logseq ``assets/``,
this app ``_assets/<note id>/``. What they agree on is the relative link, so
that is what is read. These are the cases it has to get right.

Run with ``python -m unittest discover tests``.
"""
import unittest

from application.services import notes_sync
from application.services.webdav import DavEntry, DavError

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40


def _stub_image(token, data):
    from application.models.item import NoteImage
    return NoteImage(token=token, content_type="image/png", data=data)


class _Blobs:
    """An Item as far as absorb_assets is concerned: two attachment lists,
    an id and a size."""

    def __init__(self, pk="aaa111"):
        self.pk = pk
        self.title = "Eine Notiz"
        self.note_images = []
        self.note_files = []

    def storage_bytes(self) -> int:
        return sum(len(b.data) for b in self.note_images + self.note_files)


class _Share:
    """A share that answers with what it was given, and remembers what it was
    asked for. Listings come out of the same dict — absorbing looks a target
    up before it downloads it."""

    def __init__(self, files):
        self.files = files
        self.read_paths = []
        self.listed = []

    def list(self, path=""):
        self.listed.append(path)
        out = []
        for name, data in self.files.items():
            parent = name.rsplit("/", 1)[0] if "/" in name else ""
            if parent == path:
                out.append(DavEntry(path=name, is_dir=False, size=len(data)))
        return out

    def read(self, path):
        self.read_paths.append(path)
        if path not in self.files:
            raise DavError(f"GET {path}: HTTP 404 Not Found", 404)
        return self.files[path]


def _round(share):
    """A round without its __init__ — no database, no user, just the two
    things absorbing needs."""
    rnd = notes_sync._Round.__new__(notes_sync._Round)
    rnd.client, rnd.stats, rnd.dry_run = share, notes_sync.MirrorStats(), False
    return rnd


class AbsorbAssetsTest(unittest.TestCase):
    def test_an_image_added_over_there_is_taken_into_the_note(self):
        share = _Share({"Work/_assets/aaa111/foto.png": PNG})
        note, rnd = _Blobs(), _round(share)
        body = rnd.absorb_assets(note, "- ![](_assets/aaa111/foto.png)\n",
                                 "Work/Eine Notiz.md")
        self.assertEqual(len(note.note_images), 1)
        self.assertEqual(note.note_images[0].content_type, "image/png")
        token = note.note_images[0].token
        self.assertIn(f"](/notes/aaa111/note-image/{token})", body)
        self.assertEqual(rnd.stats.assets_read, 1)

    def test_the_link_is_resolved_against_the_file_it_sits_in(self):
        share = _Share({"Work/_assets/aaa111/foto.png": PNG})
        rnd = _round(share)
        rnd.absorb_assets(_Blobs(), "![](../_assets/aaa111/foto.png)",
                          "Work/Tief/Eine Notiz.md")
        self.assertEqual(share.read_paths, ["Work/_assets/aaa111/foto.png"])

    def test_a_percent_encoded_name(self):
        share = _Share({"_assets/aaa111/mein foto.png": PNG})
        note, rnd = _Blobs(), _round(share)
        body = rnd.absorb_assets(note, "![](_assets/aaa111/mein%20foto.png)",
                                 "Eine Notiz.md")
        self.assertEqual(len(note.note_images), 1)
        self.assertNotIn("_assets", body)

    def test_something_that_is_not_an_image_becomes_a_file_attachment(self):
        share = _Share({"_assets/aaa111/Angebot.pdf": b"%PDF-1.4 ..."})
        note, rnd = _Blobs(), _round(share)
        body = rnd.absorb_assets(note, "[Angebot](_assets/aaa111/Angebot.pdf)",
                                 "Eine Notiz.md")
        self.assertEqual(note.note_images, [])
        self.assertEqual(note.note_files[0].filename, "Angebot.pdf")
        self.assertEqual(note.note_files[0].content_type, "application/pdf")
        self.assertIn("/note-file/", body)

    def test_what_the_note_already_owns_is_left_for_body_from_file(self):
        note = _Blobs()
        note.note_images.append(_stub_image("tok123", PNG))
        share = _Share({})
        rnd = _round(share)
        body = rnd.absorb_assets(note, "![](_assets/aaa111/tok123.png)",
                                 "Eine Notiz.md")
        self.assertEqual(body, "![](_assets/aaa111/tok123.png)")
        self.assertEqual(share.read_paths, [])
        self.assertEqual(len(note.note_images), 1)

    def test_the_same_file_twice_is_fetched_once(self):
        share = _Share({"_assets/aaa111/foto.png": PNG})
        note, rnd = _Blobs(), _round(share)
        rnd.absorb_assets(note, "![](_assets/aaa111/foto.png)\n"
                                "![](_assets/aaa111/foto.png)\n",
                          "Eine Notiz.md")
        self.assertEqual(len(share.read_paths), 1)
        self.assertEqual(len(note.note_images), 1)

    def test_a_file_that_is_not_there_leaves_the_link_alone(self):
        """And says nothing about it: a link into another app, or one that was
        already broken, is not this round's to repair."""
        rnd = _round(_Share({}))
        body = rnd.absorb_assets(_Blobs(), "![](_assets/aaa111/weg.png)", "N.md")
        self.assertEqual(body, "![](_assets/aaa111/weg.png)")
        self.assertEqual(rnd.stats.errors, [])
        self.assertEqual(rnd.client.read_paths, [])

    def test_whatever_folder_the_other_app_keeps_its_files_in(self):
        """DEVONthink's _resources, Obsidian's attachments, Logseq's assets —
        the folder is not the rule, the relative link is."""
        for href, remote in (("_resources/bild.png", "Work/_resources/bild.png"),
                             ("attachments/bild.png", "Work/attachments/bild.png"),
                             ("assets/bild.png", "Work/assets/bild.png"),
                             ("bild.png", "Work/bild.png")):
            with self.subTest(href=href):
                share = _Share({remote: PNG})
                note, rnd = _Blobs(), _round(share)
                body = rnd.absorb_assets(note, f"![]({href})",
                                         "Work/Eine Notiz.md")
                self.assertEqual(len(note.note_images), 1, href)
                self.assertIn("/note-image/", body)

    def test_an_address_is_not_a_file(self):
        share = _Share({})
        rnd = _round(share)
        for href in ("https://example.com/bild.png", "/notes/bbb/note-image/x",
                     "mailto:mail@example.com", "#irgendwo",
                     "Andere Notiz.md", "../Andere Notiz.md#block-7"):
            with self.subTest(href=href):
                body = f"[x]({href})"
                self.assertEqual(rnd.absorb_assets(_Blobs(), body, "Work/N.md"),
                                 body)
        self.assertEqual(share.read_paths, [])

    def test_nothing_above_the_share_is_ever_read(self):
        share = _Share({})
        rnd = _round(share)
        body = "![](../../../etc/passwd)"
        self.assertEqual(rnd.absorb_assets(_Blobs(), body, "Work/N.md"), body)
        self.assertEqual(share.read_paths, [])

    def test_a_file_too_large_for_a_note_is_left_on_the_share(self):
        share = _Share({"_assets/aaa111/film.mov": b"x" * 32})
        share.files["_assets/aaa111/film.mov"] = b"x" * 32
        rnd = _round(share)
        rnd._listings = {"_assets/aaa111": {
            "_assets/aaa111/film.mov": DavEntry(
                path="_assets/aaa111/film.mov", is_dir=False,
                size=64 * 1024 * 1024)}}
        note = _Blobs()
        body = rnd.absorb_assets(note, "[Film](_assets/aaa111/film.mov)", "N.md")
        self.assertEqual(body, "[Film](_assets/aaa111/film.mov)")
        self.assertEqual(note.note_files, [])
        self.assertEqual(share.read_paths, [])       # never even downloaded
        self.assertTrue(rnd.stats.errors)

    def test_one_listing_per_folder_however_many_pictures(self):
        share = _Share({"_assets/aaa111/a.png": PNG, "_assets/aaa111/b.png": PNG})
        note, rnd = _Blobs(), _round(share)
        rnd.absorb_assets(note, "![](_assets/aaa111/a.png)\n"
                                "![](_assets/aaa111/b.png)\n", "N.md")
        self.assertEqual(len(note.note_images), 2)
        self.assertEqual(share.listed, ["_assets/aaa111"])

    def test_and_then_it_goes_back_out_as_one_of_the_notes_own(self):
        """The round trip: what came in as a foreign file is written back
        under its token, which is how the next round recognises it as ours."""
        share = _Share({"_assets/aaa111/foto.png": PNG})
        note, rnd = _Blobs(), _round(share)
        body = rnd.absorb_assets(note, "- ![](_assets/aaa111/foto.png)\n", "N.md")
        out = notes_sync.body_to_file(note, body, "N.md")
        token = note.note_images[0].token
        self.assertIn(f"_assets/aaa111/{token}.png", out)
        self.assertEqual(rnd.absorb_assets(note, out, "N.md"), out)


if __name__ == "__main__":
    unittest.main()
