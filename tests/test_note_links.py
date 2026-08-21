import unittest
from application.services import note_links

class TargetsTest(unittest.TestCase):
    IDX = {"cairn": ("t1", "Cairn")}
    def test_wiki_and_app_links_both_count(self):
        out = note_links.targets(
            "see [[Cairn]] and [Cairn](/notes/0123456789abcdef01234567)", self.IDX)
        self.assertEqual(out, ["t1", "0123456789abcdef01234567"])
    def test_an_app_link_alone_counts(self):
        self.assertEqual(
            note_links.targets("[x](/notes/0123456789abcdef01234567)", {}),
            ["0123456789abcdef01234567"])
    def test_other_links_do_not(self):
        self.assertEqual(note_links.targets("[x](/projects/abc) [y](https://a)", {}), [])
