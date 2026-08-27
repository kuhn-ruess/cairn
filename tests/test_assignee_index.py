"""services/permissions.build_assignee_index — who an imported row names.

Run with ``python -m unittest discover tests``. The precedence rule is the
reason this has a test at all: an ``import_alias`` must never shadow another
account's real e-mail or username, or listing a colleague's address would
quietly redirect their imported tasks.
"""
import unittest

from application.services.permissions import build_assignee_index


class _User:
    """Enough of a User for the matcher: the four identity fields + aliases."""

    def __init__(self, username, email, display_name="", import_aliases=None):
        self.username = username
        self.email = email
        self.display_name = display_name
        self.import_aliases = list(import_aliases or [])

    def __repr__(self):
        return f"<{self.username}>"


class BuildAssigneeIndexTests(unittest.TestCase):

    def setUp(self):
        self.me = _User("bastian", "mail@bastian-kuhn.de", "Bastian Kuhn")
        self.mate = _User("jasmin", "jasmin@acme.de", "Jasmin")

    def index(self):
        return build_assignee_index([self.me, self.mate])

    def test_the_four_identity_forms_all_resolve(self):
        idx = self.index()
        for key in ("mail@bastian-kuhn.de", "bastian", "bastian kuhn", "mail"):
            self.assertIs(idx[key], self.me, key)

    def test_matching_is_case_and_space_insensitive(self):
        self.me.import_aliases = ["  Bastian.Kuhn.Extern@DRV-Bund.de  "]
        self.assertIs(self.index()["bastian.kuhn.extern@drv-bund.de"], self.me)

    def test_an_alias_resolves_to_its_owner(self):
        self.mate.import_aliases = ["j.kollegin@drv-bund.de"]
        self.assertIs(self.index()["j.kollegin@drv-bund.de"], self.mate)

    def test_an_alias_never_shadows_a_real_identity(self):
        # Every identity form of the colleague, claimed as an alias.
        self.me.import_aliases = ["jasmin@acme.de", "jasmin", "Jasmin"]
        idx = self.index()
        for key in ("jasmin@acme.de", "jasmin"):
            self.assertIs(idx[key], self.mate, key)

    def test_an_alias_loses_even_when_its_owner_comes_first(self):
        # Order within a pass decides between two *aliases*, never between an
        # alias and an identity — the actor is first in the candidate list.
        self.me.import_aliases = ["shared-name"]
        self.mate.import_aliases = ["shared-name"]
        self.assertIs(self.index()["shared-name"], self.me)

    def test_empty_and_missing_values_are_not_keys(self):
        self.me.display_name = ""
        self.me.import_aliases = ["", "   "]
        idx = self.index()
        self.assertNotIn("", idx)
        self.assertIsNone(idx.get("   "))

    def test_a_user_without_the_field_at_all_still_indexes(self):
        # ``strict = False`` on User: a document stored before the field
        # existed simply has no aliases.
        del self.me.import_aliases
        self.assertIs(self.index()["bastian"], self.me)


if __name__ == "__main__":
    unittest.main()
