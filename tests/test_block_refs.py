"""services/block_refs.py — ((block references)) and {{embed}}.

Run with ``python -m unittest discover tests``. The parsing here is what
decides whether a directive renders or is left on screen as its own source
text, which is what "the embed does nothing" looks like from the outside.
"""
import unittest

from application.services import block_refs

UID = "9f1e046b-f62b-4ed0-a2d8-64d19169b13b"


class RefsTest(unittest.TestCase):
    def test_finds_ids_once_each_in_order(self):
        other = "0193f2a1-6c2e-7a10-9f1b-2c4d5e6f7a8b"
        self.assertEqual(
            block_refs.refs(f"see (({UID})) and (({other})) and (({UID})) again"),
            [UID, other])

    def test_prose_full_of_brackets_is_not_a_reference(self):
        self.assertEqual(block_refs.refs("a ((thing)) and ((another one))"), [])
        self.assertEqual(block_refs.refs("f((x)) = ((y))"), [])

    def test_nothing_to_find(self):
        self.assertEqual(block_refs.refs(""), [])
        self.assertEqual(block_refs.refs("plain text"), [])


class EmbedTest(unittest.TestCase):
    """Every reasonable spelling counts: this gets typed as often as pasted."""

    def test_the_spellings_that_must_work(self):
        for text in (f"{{{{embed (({UID}))}}}}",
                     f"{{{{ embed (({UID})) }}}}",
                     f"{{{{embed(({UID}))}}}}",
                     f"{{{{Embed (({UID}))}}}}",
                     f"{{{{EMBED  (({UID}))  }}}}",
                     f"  {{{{embed (({UID}))}}}}  "):
            with self.subTest(text):
                self.assertEqual(block_refs.embed_target(text), UID)

    def test_only_as_the_whole_of_a_block(self):
        """Mid-sentence there is no room for a subtree, so it is not an embed."""
        self.assertEqual(block_refs.embed_target(f"see {{{{embed (({UID}))}}}}"), "")
        self.assertEqual(block_refs.embed_target(f"{{{{embed (({UID}))}}}} and more"), "")

    def test_not_an_embed_at_all(self):
        self.assertEqual(block_refs.embed_target(f"(({UID}))"), "")
        self.assertEqual(block_refs.embed_target("{{embed [[A title]]}}"), "")
        self.assertEqual(block_refs.embed_target(""), "")


class ExpandTest(unittest.TestCase):
    class _Note:
        pk = "abc123"
        title = "Source"

    class _Block:
        def __init__(self, body):
            self.body = body

    def resolved(self, body="The referenced line"):
        return {UID: {"note": self._Note(), "block": self._Block(body)}}

    def test_a_reference_becomes_a_link_carrying_the_block(self):
        out = block_refs.expand(f"see (({UID}))", self.resolved())
        self.assertEqual(out, "see [The referenced line](/notes/abc123?zoom=" + UID + ")")

    def test_an_embed_in_a_sentence_reads_as_the_link(self):
        """It cannot render as a block there, but it must not leave its braces
        lying in the text either — that is what looks like nothing happened."""
        out = block_refs.expand(f"see {{{{embed (({UID}))}}}} here", self.resolved())
        self.assertNotIn("{{", out)
        self.assertNotIn("}}", out)
        self.assertIn("The referenced line", out)

    def test_an_id_that_resolves_to_nothing_is_still_visible(self):
        self.assertEqual(block_refs.expand(f"see (({UID}))", {}), "see block reference")

    def test_brackets_and_parens_leave_the_link_text(self):
        """The label is planted inside [..](..), where a stray one closes the
        link early and spills the rest of the block into the page."""
        out = block_refs.expand(f"(({UID}))", self.resolved("A [thing] (of sorts)"))
        self.assertEqual(out, "[A thing of sorts](/notes/abc123?zoom=" + UID + ")")

    def test_text_without_a_reference_is_untouched(self):
        self.assertEqual(block_refs.expand("plain", self.resolved()), "plain")


class LabelTest(unittest.TestCase):
    class _Block:
        def __init__(self, body):
            self.body = body

    def test_only_the_first_line(self):
        self.assertEqual(block_refs.label_for(self._Block("one\ntwo")), "one")

    def test_an_empty_block_still_says_something(self):
        self.assertEqual(block_refs.label_for(self._Block("   ")), "empty block")

    def test_long_ones_are_cut(self):
        long = "x" * 300
        out = block_refs.label_for(self._Block(long))
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), block_refs.LABEL_LIMIT + 1)


if __name__ == "__main__":
    unittest.main()
