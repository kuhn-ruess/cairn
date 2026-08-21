"""services/containers.resolve_notes_folder — what the Notes button opens.

Run with ``python -m unittest discover tests``. Dereferencing a reference
bypasses mongoengine's ``objects`` manager, which is what hides trashed items
everywhere else — so this is one of the few places that has to check by hand.
"""
import unittest

from application.services.containers import resolve_notes_folder


class _Item:
    """Enough of an Item for this walk: a kind, a parent, a notes_folder."""

    _next = 0

    def __init__(self, kind="project", parent=None, notes_folder=None,
                 trashed_at=None):
        _Item._next += 1
        self.pk = _Item._next
        self.kind = kind
        self.parent = parent
        self.notes_folder = notes_folder
        self.trashed_at = trashed_at


class ResolveNotesFolderTest(unittest.TestCase):
    def test_its_own_link(self):
        folder = _Item(kind="folder")
        self.assertIs(resolve_notes_folder(_Item(notes_folder=folder)), folder)

    def test_inherited_from_an_ancestor(self):
        folder = _Item(kind="folder")
        project = _Item(notes_folder=folder)
        task = _Item(kind="task", parent=_Item(kind="section", parent=project))
        self.assertIs(resolve_notes_folder(task), folder)

    def test_a_trashed_folder_resolves_to_nothing(self):
        """Otherwise a project keeps its Notes button and walks straight into
        a folder the user had just thrown away."""
        folder = _Item(kind="folder", trashed_at="2026-08-21")
        self.assertIsNone(resolve_notes_folder(_Item(notes_folder=folder)))

    def test_a_link_to_something_that_is_not_a_folder(self):
        self.assertIsNone(resolve_notes_folder(_Item(notes_folder=_Item(kind="note"))))

    def test_nothing_linked_anywhere(self):
        self.assertIsNone(resolve_notes_folder(_Item(parent=_Item())))

    def test_a_cycle_does_not_hang(self):
        a = _Item()
        b = _Item(parent=a)
        a.parent = b
        self.assertIsNone(resolve_notes_folder(a))


if __name__ == "__main__":
    unittest.main()
