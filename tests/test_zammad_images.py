"""services/zammad_import — the pictures in a ticket.

A screenshot in a ticket is the ticket: "it looks like this" is what the
customer wrote. But every way Zammad points at one is a reference Cairn cannot
follow — a ``cid:`` left over from the mail, a path into Zammad's API that only
opens with the sync's token, a base64 blob that would live in the note text
forever. Each has to become an image stored on the task itself.

The cases that matter are the second sync (it must not download again), the
things that are NOT pictures (a footer's spacer gif), and the images hanging
off a reply rather than the first message.

Run with ``python -m unittest discover tests``.
"""
import base64
import unittest

from application.services import zammad_import as zi

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 4000      # big enough to count as a picture
PIXEL = b"GIF89a" + b"\0" * 40                # a footer's spacer


class _Comment:
    def __init__(self, external_id, body):
        self.external_id, self.body = external_id, body


class _Task:
    """An Item as far as the image pass is concerned."""

    def __init__(self, comments=(), note=""):
        self.pk = "task1"
        self.title = "Ticket"
        self.note = note
        self.comments = list(comments)
        self.note_images = []

    def storage_bytes(self) -> int:
        return sum(len(i.data) for i in self.note_images)


def _article(aid, attachments=()):
    return {"id": aid, "attachments": list(attachments)}


def _att(att_id, filename="shot.png", size=4008, cid=None):
    prefs = {"Content-Type": "image/png"}
    if cid:
        prefs["Content-ID"] = f"<{cid}>"
    return {"id": att_id, "filename": filename, "size": size, "preferences": prefs}


class _Zammad:
    """Stands in for the instance: counts what was actually downloaded."""

    def __init__(self, data=PNG):
        self.data, self.urls = data, []

    def __call__(self, url, token, stats):
        self.urls.append(url)
        return self.data


class ZammadImagesTest(unittest.TestCase):
    def setUp(self):
        self.real_download = zi._download
        self.zammad = _Zammad()
        zi._download = self.zammad
        self.stats = zi.ZammadStats()

    def tearDown(self):
        zi._download = self.real_download

    def _absorb(self, task, articles):
        return zi._absorb_images(task, articles, "https://help.example/api/v1",
                                 "tok", "571", self.stats)

    # ---- the three ways a body points at an image ---------------------------

    def test_an_api_path_in_the_note_becomes_an_image_on_the_task(self):
        task = _Task(note="Sieht so aus:\n\n![](/api/v1/ticket_attachment/571/9/3)")
        self.assertTrue(self._absorb(task, [_article(9, [_att(3)])]))
        self.assertEqual(len(task.note_images), 1)
        self.assertIn("](/tasks/task1/note-image/zm-9-3)", task.note)
        self.assertEqual(self.stats.images_imported, 1)
        self.assertEqual(
            self.zammad.urls,
            ["https://help.example/api/v1/ticket_attachment/571/9/3"])

    def test_a_cid_is_resolved_through_the_attachment_content_id(self):
        task = _Task(note="![Screenshot](cid:part12.abc@mail)")
        self._absorb(task, [_article(9, [_att(3, cid="part12.abc@mail")])])
        self.assertIn("![Screenshot](/tasks/task1/note-image/zm-9-3)", task.note)

    def test_a_pasted_base64_image_is_stored_instead_of_kept_in_the_text(self):
        src = "data:image/png;base64," + base64.b64encode(PNG).decode()
        task = _Task(note=f"![]({src})")
        self._absorb(task, [_article(9)])
        self.assertEqual(len(task.note_images), 1)
        self.assertNotIn("base64", task.note)
        self.assertEqual(self.zammad.urls, [])       # nothing to fetch

    # ---- what a second sync must not do -------------------------------------

    def test_a_re_sync_finds_the_image_it_already_has(self):
        articles = [_article(9, [_att(3)])]
        first = _Task(note="![](/api/v1/ticket_attachment/571/9/3)")
        self._absorb(first, articles)
        # The next run writes the raw body again and the pass runs over it.
        first.note = "![](/api/v1/ticket_attachment/571/9/3)"
        self._absorb(first, articles)
        self.assertEqual(len(first.note_images), 1)
        self.assertEqual(len(self.zammad.urls), 1)
        self.assertIn("](/tasks/task1/note-image/zm-9-3)", first.note)

    # ---- what is not a picture ----------------------------------------------

    def test_a_spacer_gif_is_left_where_it_is(self):
        zi._download = _Zammad(PIXEL)
        task = _Task(note="![](/api/v1/ticket_attachment/571/9/3)")
        self.assertFalse(self._absorb(task, [_article(9, [_att(3, size=46)])]))
        self.assertEqual(task.note_images, [])
        self.assertIn("/api/v1/ticket_attachment/571/9/3", task.note)

    def test_an_image_hosted_somewhere_else_is_not_fetched(self):
        task = _Task(note="![](https://cdn.example/logo.png)")
        self.assertFalse(self._absorb(task, [_article(9)]))
        self.assertEqual(self.zammad.urls, [])

    def test_an_attachment_too_large_for_a_task_stays_in_zammad(self):
        task = _Task(note="![](/api/v1/ticket_attachment/571/9/3)")
        self._absorb(task, [_article(9, [_att(3, size=40 * 1024 * 1024)])])
        self.assertEqual(task.note_images, [])
        self.assertEqual(self.zammad.urls, [])
        self.assertTrue(self.stats.errors)

    # ---- attached, not pasted ------------------------------------------------

    def test_a_screenshot_attached_to_the_mail_is_appended_to_the_body(self):
        task = _Task(note="Guten Tag, siehe Anhang.")
        self.assertTrue(self._absorb(task, [_article(9, [_att(3, "fehler.png")])]))
        self.assertTrue(task.note.endswith(
            "![fehler.png](/tasks/task1/note-image/zm-9-3)"))
        self.assertIn("Guten Tag", task.note)

    def test_the_same_attachment_is_not_appended_twice(self):
        articles = [_article(9, [_att(3, "fehler.png")])]
        task = _Task(note="Guten Tag, siehe Anhang.")
        self._absorb(task, articles)
        after_first = task.note
        self._absorb(task, articles)
        self.assertEqual(task.note, after_first)
        self.assertEqual(len(self.zammad.urls), 1)

    def test_an_attached_image_goes_above_the_ticket_back_link(self):
        task = _Task(note="Siehe Anhang.\n\n[Zammad #571](https://help.example/#ticket/zoom/571)")
        self._absorb(task, [_article(9, [_att(3, "fehler.png")])])
        self.assertEqual(task.note.splitlines()[-1],
                         "[Zammad #571](https://help.example/#ticket/zoom/571)")
        self.assertIn("![fehler.png](/tasks/task1/note-image/zm-9-3)", task.note)

    def test_a_pdf_attachment_is_left_in_zammad(self):
        pdf = {"id": 4, "filename": "rechnung.pdf", "size": 90000,
               "preferences": {"Content-Type": "application/pdf"}}
        task = _Task(note="Anbei.")
        self.assertFalse(self._absorb(task, [_article(9, [pdf])]))
        self.assertEqual(self.zammad.urls, [])
        self.assertEqual(task.note, "Anbei.")

    # ---- the rest of the thread ---------------------------------------------

    def test_an_image_in_a_reply_lands_on_its_comment(self):
        task = _Task(
            comments=[_Comment("10", "Und jetzt so:\n\n![](cid:zwei@mail)")],
            note="Erste Nachricht")
        self.assertTrue(self._absorb(task, [
            _article(9),
            _article(10, [_att(7, cid="zwei@mail")]),
        ]))
        self.assertIn("](/tasks/task1/note-image/zm-10-7)", task.comments[0].body)
        self.assertEqual(len(task.note_images), 1)

    def test_a_comment_cairn_wrote_is_not_touched(self):
        task = _Task(comments=[_Comment(None, "![](cid:egal)")], note="")
        self.assertFalse(self._absorb(task, [_article(9)]))


if __name__ == "__main__":
    unittest.main()
