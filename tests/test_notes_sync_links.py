"""services/notes_sync — Cairn's link syntaxes in the mirrored file.

Run with ``python -m unittest discover tests``. Nothing outside Cairn and
Logseq understands "[[Title]]", "((id))" or "{{embed ((id))}}", so the mirror
writes real Markdown links — and has to read them back without losing what the
writer actually said.
"""
import unittest

from application.services import notes_sync

UID = "9f1e046b-f62b-4ed0-a2d8-64d19169b13b"
OTHER = "0193f2a1-6c2e-7a10-9f1b-2c4d5e6f7a8b"


class _Note:
    """Stands in for an Item: the translators only read these three."""
    pk = "aaa111"
    note_images = []
    note_files = []


class _Links(notes_sync.LinkMap):
    """A LinkMap built by hand, so these tests need no database."""

    def __init__(self, notes, blocks=None, labels=None):
        self.by_title, self.by_pk, self.by_path = {}, {}, {}
        self.block_note, self._labels, self._bodies = {}, {}, {}
        for pk, (title, path) in notes.items():
            self.by_title[title.lower()] = (pk, path)
            self.by_pk[pk] = (title, path)
            self.by_path[path.lower()] = pk
        self.block_note = dict(blocks or {})
        self._labels = dict(labels or {})

    def label(self, block_id):
        return self._labels.get(block_id, "")


NOTES = {
    "t1": ("Cairn Handbuch", "Arbeit/Cairn Handbuch.md"),
    "t2": ("Wurzelnotiz", "Wurzelnotiz.md"),
}
LINKS = _Links(NOTES, blocks={UID: "t1"}, labels={UID: "Der Originalblock"})


def to_file(body, path="Allgemein.md"):
    return notes_sync.body_to_file(_Note(), body, path, LINKS)


def from_file(body, path="Allgemein.md"):
    return notes_sync.body_from_file(_Note(), body, path, LINKS)


class ToFileTest(unittest.TestCase):
    def test_a_wiki_link_becomes_a_real_one(self):
        self.assertEqual(to_file("see [[Cairn Handbuch]] here"),
                         "see [Cairn Handbuch](Arbeit/Cairn%20Handbuch.md) here")

    def test_a_labelled_wiki_link_keeps_its_label(self):
        self.assertEqual(to_file("[[Cairn Handbuch|the manual]]"),
                         "[the manual](Arbeit/Cairn%20Handbuch.md)")

    def test_a_block_reference_carries_its_id_in_the_fragment(self):
        """Without it the id is gone and the reference comes home as a plain
        note link — a quiet loss of what the writer said."""
        self.assertEqual(
            to_file(f"see (({UID}))"),
            "see [Der Originalblock](Arbeit/Cairn%20Handbuch.md#block-" + UID + ")")

    def test_an_embed_is_marked_as_one(self):
        self.assertEqual(
            to_file(f"{{{{embed (({UID}))}}}}"),
            "[Der Originalblock](Arbeit/Cairn%20Handbuch.md#embed-" + UID + ")")

    def test_the_path_is_relative_to_the_file_holding_the_link(self):
        self.assertEqual(to_file("[[Wurzelnotiz]]", "Arbeit/Tief/Tiefe.md"),
                         "[Wurzelnotiz](../../Wurzelnotiz.md)")

    def test_what_cannot_be_resolved_is_left_alone(self):
        """Better a wiki link that only Cairn understands than a link to a
        file that is not there."""
        self.assertEqual(to_file("[[No such note]]"), "[[No such note]]")
        self.assertEqual(to_file(f"(({OTHER}))"), f"(({OTHER}))")

    def test_ordinary_markdown_is_untouched(self):
        for text in ("[a link](https://example.com)",
                     "plain text", "![img](_assets/x/y.png)"):
            with self.subTest(text):
                self.assertEqual(to_file(text), text)


class RoundTripTest(unittest.TestCase):
    """What goes out has to come back as what it was."""

    CASES = (
        "- see [[Cairn Handbuch]] here\n",
        "- [[Cairn Handbuch|the manual]]\n",
        f"- reference (({UID}))\n",
        f"- {{{{embed (({UID}))}}}}\n",
        "- [an outside link](https://example.com)\n",
        "- [[No such note]]\n",
        "- plain words, no links at all\n",
    )

    def test_every_shape(self):
        for text in self.CASES:
            with self.subTest(text):
                self.assertEqual(from_file(to_file(text)), text)

    def test_across_folders(self):
        path = "Arbeit/Tief/Tiefe.md"
        text = "- up to [[Wurzelnotiz]]\n"
        self.assertEqual(from_file(to_file(text, path), path), text)


class FromFileTest(unittest.TestCase):
    def test_a_link_to_something_else_stays_a_link(self):
        for text in ("[docs](https://example.com/a.md)",
                     "[a file we do not know](Somewhere/Else.md)"):
            with self.subTest(text):
                self.assertEqual(from_file(text), text)

    def test_a_hand_written_link_to_a_note_becomes_a_wiki_link(self):
        """Written in DEVONthink by hand: in Cairn it is a real note link."""
        self.assertEqual(from_file("[Wurzelnotiz](Wurzelnotiz.md)"),
                         "[[Wurzelnotiz]]")

    def test_renamed_link_text_survives_as_a_label(self):
        self.assertEqual(from_file("[my own words](Wurzelnotiz.md)"),
                         "[[Wurzelnotiz|my own words]]")

    def test_an_unknown_fragment_does_not_invent_a_reference(self):
        self.assertEqual(from_file("[x](Wurzelnotiz.md#something-else)"),
                         "[[Wurzelnotiz|x]]")


if __name__ == "__main__":
    unittest.main()
