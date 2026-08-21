"""services/outline.py — the note body as a block tree.

Run with ``python -m unittest discover tests`` (stdlib only, no test
dependency). These are the invariants the whole outline feature stands on: if
parse/serialize don't agree, every block id, fold state and reference built on
top of them is quietly wrong.
"""
import unittest

from application.services import outline


def shape(blocks):
    """The tree as nested tuples, for readable assertions."""
    return [(b.content, b.props, shape(b.children)) for b in blocks]


class SerializeTest(unittest.TestCase):
    def test_nesting_uses_one_tab_per_level(self):
        tree = [outline.Block(content="a", children=[
            outline.Block(content="b", children=[outline.Block(content="c")])])]
        self.assertEqual(outline.serialize(tree), "- a\n\t- b\n\t\t- c\n")

    def test_further_lines_sit_two_spaces_past_the_bullet(self):
        tree = [outline.Block(content="one\ntwo", children=[
            outline.Block(content="deep\nlines")])]
        self.assertEqual(outline.serialize(tree),
                         "- one\n  two\n\t- deep\n\t  lines\n")

    def test_properties_are_written_after_the_content(self):
        tree = [outline.Block(content="a", props={"id": "u1", "collapsed": "true"})]
        self.assertEqual(outline.serialize(tree),
                         "- a\n  id:: u1\n  collapsed:: true\n")

    def test_empty_tree_is_an_empty_string(self):
        self.assertEqual(outline.serialize([]), "")


class RoundTripTest(unittest.TestCase):
    """serialize -> parse -> serialize must not drift."""

    CASES = {
        "flat": "- one\n- two\n- three\n",
        "nested": "- a\n\t- b\n\t\t- c\n\t- d\n- e\n",
        "multiline": "- first\n  second\n\t- child\n\t  more\n",
        "props": "- a\n  id:: 0193f2a1\n  collapsed:: true\n\t- b\n",
        "blank line inside a block": "- a\n  \n  b\n",
        "empty block": "-\n\t- child of nothing\n",
        "markdown survives": "- # Heading\n- **bold** and `code`\n- | a | b |\n",
        "wiki links survive": "- see [[Weekly review]] and ((0193f2a1))\n",
        "fenced code": "- look:\n  ```python\n  x = 1\n  - not a bullet\n  ```\n\t- after\n",
        "tilde fence": "- t\n  ~~~\n  - still code\n  ~~~\n",
    }

    def test_round_trips(self):
        for name, text in self.CASES.items():
            with self.subTest(name):
                self.assertEqual(outline.serialize(outline.parse(text)), text)

    def test_parsing_is_idempotent(self):
        for name, text in self.CASES.items():
            with self.subTest(name):
                once = outline.serialize(outline.parse(text))
                self.assertEqual(outline.serialize(outline.parse(once)), once)


class StrictParseTest(unittest.TestCase):
    def test_properties_leave_the_content(self):
        [block] = outline.parse("- a\n  id:: u1\n  collapsed:: true\n")
        self.assertEqual(block.content, "a")
        self.assertEqual(block.props, {"id": "u1", "collapsed": "true"})
        self.assertEqual(block.uuid, "u1")
        self.assertTrue(block.collapsed)

    def test_a_bullet_inside_fenced_code_is_not_a_block(self):
        blocks = outline.parse("- look:\n  ```\n  - not a bullet\n  ```\n")
        self.assertEqual(len(blocks), 1)
        self.assertIn("- not a bullet", blocks[0].content)

    def test_a_property_inside_fenced_code_stays_text(self):
        [block] = outline.parse("- x\n  ```\n  id:: nope\n  ```\n")
        self.assertEqual(block.props, {})
        self.assertIn("id:: nope", block.content)

    def test_an_over_indented_bullet_is_clamped(self):
        blocks = outline.parse("- a\n\t\t\t- b\n")
        self.assertEqual(shape(blocks), [("a", {}, [("b", {}, [])])])

    def test_text_before_the_first_bullet_is_kept(self):
        blocks = outline.parse("- a\n\nstray\n")
        self.assertEqual([b.content for b in blocks], ["a\n\nstray"])

    def test_idx_is_document_order(self):
        blocks = outline.parse("- a\n\t- b\n\t\t- c\n- d\n")
        self.assertEqual([(b.content, b.idx) for b in outline.walk(blocks)],
                         [("a", 0), ("b", 1), ("c", 2), ("d", 3)])


class KeywordTest(unittest.TestCase):
    def test_keyword_splits_off_the_body(self):
        [block] = outline.parse("- TODO write the parser\n")
        self.assertEqual(block.keyword, "TODO")
        self.assertEqual(block.body, "write the parser")

    def test_a_word_that_merely_starts_with_todo_is_not_a_keyword(self):
        [block] = outline.parse("- TODOS are not keywords\n")
        self.assertEqual(block.keyword, "")
        self.assertEqual(block.body, "TODOS are not keywords")


class CheckboxTest(unittest.TestCase):
    """"- [ ] x" — the dash became the bullet, so the box has to be read off
    the block. The FILE stays an ordinary Markdown checklist either way."""

    def test_open_and_done(self):
        self.assertIs(outline.parse("- [ ] milk\n")[0].checked, False)
        self.assertIs(outline.parse("- [x] milk\n")[0].checked, True)
        self.assertIs(outline.parse("- [X] milk\n")[0].checked, True)

    def test_a_plain_block_has_no_box(self):
        self.assertIsNone(outline.parse("- milk\n")[0].checked)
        self.assertIsNone(outline.parse("- [later] milk\n")[0].checked)

    def test_the_marker_leaves_the_body(self):
        self.assertEqual(outline.parse("- [ ] milk\n")[0].body, "milk")

    def test_a_keyword_and_a_box_can_share_a_block(self):
        [b] = outline.parse("- TODO [ ] milk\n")
        self.assertEqual(b.keyword, "TODO")
        self.assertIs(b.checked, False)
        self.assertEqual(b.body, "milk")

    def test_ticking_is_a_content_edit(self):
        """What the editor writes back must parse to the same shape."""
        self.assertEqual(outline.serialize(outline.parse("- [x] milk\n")),
                         "- [x] milk\n")


class DetectionTest(unittest.TestCase):
    """Which parser a body gets. Reading a hand-written list as strict swallows
    its sub-items into the parent block, so this is the load-bearing choice."""

    def test_our_own_outline_is_strict(self):
        self.assertTrue(outline.is_outline("- a\n\t- b\n"))
        self.assertTrue(outline.is_outline("- a\n  id:: u1\n"))
        self.assertTrue(outline.is_outline("- a\n- b\n"))

    def test_a_space_indented_list_is_a_human_writing_markdown(self):
        self.assertFalse(outline.is_outline("- a\n  - b\n"))
        self.assertFalse(outline.is_outline("prose\n"))
        self.assertFalse(outline.is_outline(""))

    def test_a_legacy_checklist_nests_instead_of_merging(self):
        blocks = outline.parse("- [ ] milk\n  - [ ] oat milk\n- [ ] bread\n")
        self.assertEqual(shape(blocks), [
            ("[ ] milk", {}, [("[ ] oat milk", {}, [])]),
            ("[ ] bread", {}, []),
        ])

    def test_a_tab_indented_legacy_list_already_reads_as_nesting(self):
        blocks = outline.parse("Intro\n- a\n\t- b\n")
        self.assertEqual(shape(blocks), [
            ("Intro", {}, []),
            ("a", {}, [("b", {}, [])]),
        ])


class LooseParseTest(unittest.TestCase):
    """Notes written before the outline existed. Nothing is migrated — they
    simply have to READ as an outline."""

    def test_paragraphs_become_top_level_blocks(self):
        blocks = outline.parse("First para.\n\nSecond para.\n")
        self.assertEqual([b.content for b in blocks], ["First para.", "Second para."])

    def test_a_paragraphs_own_lines_stay_together(self):
        [block] = outline.parse("one\ntwo\n")
        self.assertEqual(block.content, "one\ntwo")

    def test_headings_are_blocks_of_their_own(self):
        blocks = outline.parse("# Title\nprose\n")
        self.assertEqual([b.content for b in blocks], ["# Title", "prose"])

    def test_markdown_lists_become_nested_blocks(self):
        # A body starting with "- " already reads as an outline, so the loose
        # path is exercised through a note that opens with prose.
        blocks = outline._loose("intro\n\n* a\n  * b\n    * c\n* d\n")
        self.assertEqual(shape(blocks), [
            ("intro", {}, []),
            ("a", {}, [("b", {}, [("c", {}, [])])]),
            ("d", {}, []),
        ])

    def test_numbered_lists_nest_too(self):
        blocks = outline._loose("x\n\n1. one\n   1. inner\n2. two\n")
        self.assertEqual(shape(blocks), [
            ("x", {}, []),
            ("one", {}, [("inner", {}, [])]),
            ("two", {}, []),
        ])

    def test_a_list_items_own_lines_stay_with_it(self):
        blocks = outline._loose("x\n\n* item\n  continued here\n")
        self.assertEqual(blocks[1].content, "item\ncontinued here")

    def test_fenced_code_stays_in_one_piece(self):
        blocks = outline._loose("intro\n\n```python\nx = 1\n\ny = 2\n```\n")
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[1].content, "```python\nx = 1\n\ny = 2\n```")

    def test_a_legacy_note_survives_the_first_save(self):
        """The real migration path: read loose, write strict, read again."""
        legacy = "# Notes\n\nSome prose.\n\n- one\n- two\n"
        once = outline.serialize(outline.parse(legacy))
        self.assertEqual(once, "- # Notes\n- Some prose.\n- one\n- two\n")
        self.assertEqual(outline.serialize(outline.parse(once)), once)

    def test_an_empty_body_is_an_empty_outline(self):
        self.assertEqual(outline.parse(""), [])
        self.assertEqual(outline.parse("   \n\n"), [])


class LookupTest(unittest.TestCase):
    TEXT = "- a\n  id:: u1\n\t- b\n\t  id:: u2\n\t\t- c\n- d\n"

    def test_find_and_path(self):
        blocks = outline.parse(self.TEXT)
        self.assertEqual(outline.find(blocks, "u2").content, "b")
        self.assertIsNone(outline.find(blocks, "nope"))
        self.assertEqual([b.content for b in outline.path_to(blocks, "u2")], ["a", "b"])
        self.assertEqual(outline.path_to(blocks, "nope"), [])

    def test_collect_ids_and_by_idx(self):
        blocks = outline.parse(self.TEXT)
        self.assertEqual(outline.collect_ids(blocks), ["u1", "u2"])
        self.assertEqual(outline.by_idx(blocks, 2).content, "c")

    def test_ensure_id_is_lazy_and_stable(self):
        blocks = outline.parse(self.TEXT)
        fresh = outline.by_idx(blocks, 2)
        self.assertEqual(fresh.uuid, "")
        minted = outline.ensure_id(fresh)
        self.assertTrue(minted)
        self.assertEqual(outline.ensure_id(fresh), minted)
        self.assertIn(f"id:: {minted}", outline.serialize(blocks))

    def test_plain_drops_bullets_and_properties(self):
        self.assertEqual(outline.plain(self.TEXT), "a\nb\nc\nd")


if __name__ == "__main__":
    unittest.main()
