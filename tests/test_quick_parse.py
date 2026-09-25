"""services/quick_parse — what ``@``, ``+`` and ``#`` mean in a task line.

Run with ``python -m unittest discover tests``.

The rule this file pins down is a decision, not an implementation detail:
**``@`` is always a person, ``+`` is always a tag** — in a comment, in the
quick-add box, in a sub-task line, and on a line captured out of a note. The
``@`` used to mean "tag" in the parser while it meant "person" in a comment,
which is how ``@bastian anrufen`` in a note turned into a task tagged
"bastian" that nobody was assigned and nobody was told about.
"""
import unittest
from unittest import mock

from application.services.quick_parse import parse_task_text


class _Tag:
    def __init__(self, name):
        self.name = name


class _User:
    """Enough of a User for the parser: its embedded tags."""

    def __init__(self, tags=()):
        self.tags = [_Tag(n) for n in tags]

    def tag_by_name(self, name):
        lname = (name or "").lower()
        for t in self.tags:
            if t.name.lower() == lname:
                return t
        return None


def _parse(text, user, assignees=None):
    """``parse_task_text`` with the assignable-user lookup stubbed out, so the
    token rules are testable without a team in a database."""
    people = {k.lower(): v for k, v in (assignees or {}).items()}

    def resolve(actor, name):
        return people.get((name or "").strip().lower())

    with mock.patch(
        "application.services.permissions.resolve_assignable_user_by_name",
        side_effect=resolve,
    ):
        return parse_task_text(text, user)


class SigilMeaningTests(unittest.TestCase):

    def setUp(self):
        self.user = _User(tags=["phone", "Review AI Work"])
        self.sven = object()

    def test_at_is_a_person(self):
        p = _parse("call the bank @sven", self.user, {"sven": self.sven})
        self.assertIs(p.assignee, self.sven)
        self.assertEqual(p.tags, [])
        self.assertEqual(p.title, "call the bank")

    def test_plus_is_a_tag(self):
        p = _parse("call the bank +phone", self.user)
        self.assertEqual(p.tags, ["phone"])
        self.assertIsNone(p.assignee)
        self.assertEqual(p.title, "call the bank")

    def test_at_never_becomes_a_tag(self):
        """The bug: ``@sven`` used to land in ``tags``/``unresolved_tags``."""
        p = _parse("call the bank @sven", self.user)
        self.assertEqual(p.tags, [])
        self.assertEqual(p.unresolved_tags, [])
        self.assertEqual(p.unresolved_assignee, "sven")

    def test_plus_never_becomes_a_person(self):
        p = _parse("call the bank +sven", self.user, {"sven": self.sven})
        self.assertIsNone(p.assignee)
        self.assertEqual(p.unresolved_tags, ["sven"])

    def test_both_sigils_in_one_line(self):
        p = _parse("call the bank @sven +phone p1 !", self.user,
                   {"sven": self.sven})
        self.assertIs(p.assignee, self.sven)
        self.assertEqual(p.tags, ["phone"])
        self.assertEqual(p.priority, 1)
        self.assertTrue(p.flagged)
        self.assertEqual(p.title, "call the bank")

    def test_several_tags(self):
        self.user.tags.append(_Tag("errand"))
        p = _parse("buy milk +phone +errand", self.user)
        self.assertEqual(p.tags, ["phone", "errand"])

    def test_a_tag_name_with_spaces_is_quoted(self):
        p = _parse('finish this +"Review AI Work"', self.user)
        self.assertEqual(p.tags, ["Review AI Work"])
        self.assertEqual(p.title, "finish this")

    def test_a_person_written_by_display_name_is_quoted(self):
        p = _parse('ask them @"Sven Meier"', self.user,
                   {"Sven Meier": self.sven})
        self.assertIs(p.assignee, self.sven)
        self.assertEqual(p.title, "ask them")

    def test_a_dotted_username_still_resolves(self):
        p = _parse("ask them @sven.meier", self.user,
                   {"sven.meier": self.sven})
        self.assertIs(p.assignee, self.sven)

    def test_an_email_address_is_not_a_person_token(self):
        """``@`` only starts a token at a word boundary — otherwise every
        address pasted into a line would try to assign the task."""
        p = _parse("mail sven@example.com about it", self.user)
        self.assertIsNone(p.assignee)
        self.assertIsNone(p.unresolved_assignee)
        self.assertEqual(p.title, "mail sven@example.com about it")

    def test_the_tag_match_is_case_insensitive_and_keeps_the_real_name(self):
        p = _parse("call +PHONE", self.user)
        self.assertEqual(p.tags, ["phone"])

    def test_a_tag_name_may_hold_a_colon(self):
        """The bug: ``+AI:local`` was cut at the colon — the task came out
        tagged "AI" with ``:local`` still in the title."""
        self.user.tags.append(_Tag("AI:local"))
        p = _parse("run the model +AI:local", self.user)
        self.assertEqual(p.tags, ["AI:local"])
        self.assertEqual(p.unresolved_tags, [])
        self.assertEqual(p.title, "run the model")

    def test_a_colon_that_ends_the_token_is_not_part_of_the_tag(self):
        """A colon only joins two halves of a name; one followed by a space
        punctuates the sentence and stays out of the tag."""
        p = _parse("+phone: ring the bank", self.user)
        self.assertEqual(p.tags, ["phone"])
        self.assertEqual(p.title, ": ring the bank")


class ProjectTokenTests(unittest.TestCase):
    """``#`` is untouched by the swap — it still means the project."""

    def test_hash_is_still_the_project(self):
        user = _User()
        item = mock.MagicMock()
        item.objects.return_value.first.return_value = None
        with mock.patch("application.models.item.Item", item):
            p = _parse("write the report #Website +phone", user)
        self.assertEqual(p.unresolved_project, "Website")
        self.assertEqual(p.title, "write the report")


if __name__ == "__main__":
    unittest.main()
