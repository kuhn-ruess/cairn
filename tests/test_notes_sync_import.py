"""services/notes_sync — what the mirrored file looks like, and when it says
it changed.

Run with ``python -m unittest discover tests``. Two things the round trip has
to get right and neither is visible from inside the app:

* a block property (``id::``) is a line of its own to the outline and a lazy
  paragraph continuation to Markdown — every renderer prints it inside the
  bullet's text unless it is hidden;
* a file that comes home carries its own timestamps. Stamping "now" on the
  way in is how an imported archive loses its dates.
"""
import unittest
from datetime import datetime, timezone

from application.services import notes_sync
from application.services.webdav import DavEntry

BODY = ("- Erste Zeile\n"
        "\t- Kind mit Referenz\n"
        "\t  id:: 0193f2a1-6c2e-7a10-9f1b-2c4d5e6f7a8b\n"
        "\t  collapsed:: true\n"
        "\t\t- Enkel\n")


class _Note:
    """Stands in for an Item — render() reads only these."""
    pk = "aaa111"
    title = "Eine Notiz"
    tags = []
    note_format = ""
    note_images = []
    note_files = []
    created_at = datetime(2026, 1, 2, 8, 0, tzinfo=timezone.utc)
    updated_at = datetime(2026, 3, 4, 9, 0, tzinfo=timezone.utc)

    def __init__(self, note):
        self.note = note


class PropsTest(unittest.TestCase):
    def test_a_property_is_hidden_as_a_comment(self):
        out = notes_sync.hide_props(BODY)
        self.assertIn("\t  <!-- id:: 0193f2a1-6c2e-7a10-9f1b-2c4d5e6f7a8b -->", out)
        self.assertIn("\t  <!-- collapsed:: true -->", out)
        self.assertNotIn("\t  id::", out)

    def test_and_comes_back_exactly(self):
        self.assertEqual(notes_sync.show_props(notes_sync.hide_props(BODY)), BODY)

    def test_the_bullet_text_itself_is_untouched(self):
        self.assertIn("\t- Kind mit Referenz\n", notes_sync.hide_props(BODY))

    def test_prose_that_merely_mentions_a_property_is_left_alone(self):
        body = "- Ein Block\n  schreibe key:: value irgendwo\n"
        self.assertEqual(notes_sync.hide_props(body), body)

    def test_an_outline_note_is_written_without_the_noise(self):
        out = notes_sync.render(_Note(BODY), "Eine Notiz.md")
        self.assertIn("<!-- id:: ", out)
        self.assertNotIn("\t  id:: ", out)

    def test_a_markdown_document_is_stored_exactly_as_typed(self):
        note = _Note("Ein Absatz.\n\n  key:: value\n")
        note.note_format = "markdown"
        self.assertIn("  key:: value", notes_sync.render(note, "Doc.md"))

    def test_reading_a_file_restores_the_property(self):
        hidden = notes_sync.hide_props(BODY)
        self.assertEqual(notes_sync.body_from_file(_Note(BODY), hidden), BODY)


class RemoteTimeTest(unittest.TestCase):
    def entry(self, mtime):
        return DavEntry(path="x.md", is_dir=False, mtime=mtime)

    def test_an_http_date_from_the_share(self):
        got = notes_sync._Round.remote_time(
            self.entry("Mon, 07 Sep 2026 15:04:05 GMT"), {})
        self.assertEqual(got, datetime(2026, 9, 7, 15, 4, 5, tzinfo=timezone.utc))

    def test_the_share_wins_over_our_own_stale_front_matter(self):
        got = notes_sync._Round.remote_time(
            self.entry("Mon, 07 Sep 2026 15:04:05 GMT"),
            {"updated": "2026-01-01T00:00:00+00:00"})
        self.assertEqual(got.year, 2026)
        self.assertEqual(got.month, 9)

    def test_front_matter_when_the_share_answers_no_time(self):
        got = notes_sync._Round.remote_time(
            self.entry(""), {"updated": "2026-01-01T10:00:00+00:00"})
        self.assertEqual(got, datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))

    def test_a_naive_stamp_is_read_as_utc(self):
        self.assertEqual(notes_sync._parse_dt("2026-01-01T10:00:00"),
                         datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))

    def test_nothing_readable_is_no_answer_at_all(self):
        self.assertIsNone(notes_sync._Round.remote_time(self.entry("gestern"),
                                                        {"updated": "bald"}))
        self.assertIsNone(notes_sync._Round.remote_time(None, {}))


if __name__ == "__main__":
    unittest.main()
