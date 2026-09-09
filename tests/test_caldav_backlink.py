"""views/caldav._strip_backlink — the link back to Cairn must not accumulate.

The export writes a link to the task on the first line of DESCRIPTION. A CalDAV
client sends the whole description back on every edit, so the link has to be
taken off again on the way in — otherwise it is saved into the note and the
next export puts a second one in front of it.

Run with ``python -m unittest discover tests``.
"""
import unittest

from application.views.caldav import _strip_backlink

PK = "6a184b656f9fb3e7524f1c75"
URL = f"https://app.cairn-app.de/tasks/{PK}"


class _Item:
    def __init__(self, pk=PK):
        self.pk = pk


class StripBacklinkTest(unittest.TestCase):

    def test_link_and_blank_line_go(self):
        self.assertEqual(_strip_backlink(f"{URL}\n\nDer Text", _Item()), "Der Text")

    def test_link_only_leaves_nothing(self):
        self.assertEqual(_strip_backlink(URL, _Item()), "")

    def test_trailing_slash_and_http(self):
        text = f"http://app.cairn-app.de/tasks/{PK}/\n\nText"
        self.assertEqual(_strip_backlink(text, _Item()), "Text")

    def test_another_host_still_counts(self):
        # The same task read through a different hostname (LAN, port-forward).
        self.assertEqual(
            _strip_backlink(f"https://cairn.local:5004/tasks/{PK}\n\nText", _Item()),
            "Text")

    def test_a_link_to_a_different_task_survives(self):
        other = "https://app.cairn-app.de/tasks/6a5b75eb37f437de456c40f6"
        text = f"{other}\n\nSiehe da"
        self.assertEqual(_strip_backlink(text, _Item()), text)

    def test_a_note_that_merely_mentions_the_link_survives(self):
        text = f"Siehe {URL} für den Kontext"
        self.assertEqual(_strip_backlink(text, _Item()), text)

    def test_repeated_application_is_idempotent(self):
        once = _strip_backlink(f"{URL}\n\nText", _Item())
        self.assertEqual(_strip_backlink(once, _Item()), once)

    def test_empty_and_unsaved_items_pass_through(self):
        self.assertEqual(_strip_backlink("", _Item()), "")
        self.assertEqual(_strip_backlink(f"{URL}\n\nText", _Item(pk=None)),
                         f"{URL}\n\nText")


if __name__ == "__main__":
    unittest.main()
