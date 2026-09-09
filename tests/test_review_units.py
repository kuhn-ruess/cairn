"""What the weekly review considers a review unit, and on what cadence.

Two silent-loss bugs are pinned here. A task filed straight into a FOLDER has
no enclosing project, so it appeared in no review at all (and not in the Inbox
either, which is ``parent=None``). And a folder's ``review_interval_days`` is
written by the code, not by the user — inheriting that 0 silenced every project
underneath it.

Run with ``python -m unittest discover tests``.
"""
import unittest
from datetime import datetime, timedelta, timezone

from application.models.item import DEFAULT_REVIEW_INTERVAL_DAYS, Item
from application.views.review import _review_unit

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


class _Node:
    """Enough of an Item for the two walks: kind, parent, review fields."""

    _next = 0

    def __init__(self, kind="project", parent=None, review_interval_days=None,
                 last_reviewed_at=None, status="active"):
        _Node._next += 1
        self.pk = _Node._next
        self.kind = kind
        self.parent = parent
        self.review_interval_days = review_interval_days
        self.last_reviewed_at = last_reviewed_at
        self.status = status

    # The two methods under test are plain walks over duck-typed nodes.
    def effective_review_interval(self):
        return Item.effective_review_interval(self)

    def is_review_due(self, now=None):
        return Item.is_review_due(self, now)


class ReviewUnitTest(unittest.TestCase):

    def test_task_in_a_project(self):
        proj = _Node()
        t = _Node(kind="task", parent=proj)
        self.assertIs(_review_unit(t), proj)

    def test_section_and_parent_task_are_walked_past(self):
        proj = _Node()
        sec = _Node(kind="section", parent=proj)
        parent_task = _Node(kind="task", parent=sec)
        t = _Node(kind="task", parent=parent_task)
        self.assertIs(_review_unit(t), proj)

    def test_task_in_a_folder_has_no_unit_without_the_folder_set(self):
        folder = _Node(kind="folder")
        t = _Node(kind="task", parent=folder)
        self.assertIsNone(_review_unit(t))

    def test_task_holding_folder_is_the_unit(self):
        folder = _Node(kind="folder")
        t = _Node(kind="task", parent=folder)
        self.assertIs(_review_unit(t, {folder.pk}), folder)

    def test_project_wins_over_a_folder_further_up(self):
        folder = _Node(kind="folder")
        proj = _Node(parent=folder)
        t = _Node(kind="task", parent=proj)
        self.assertIs(_review_unit(t, {folder.pk}), proj)

    def test_cycle_terminates(self):
        a = _Node(kind="section")
        b = _Node(kind="section", parent=a)
        a.parent = b
        t = _Node(kind="task", parent=b)
        self.assertIsNone(_review_unit(t))


class EffectiveIntervalTest(unittest.TestCase):

    def test_own_value_wins(self):
        self.assertEqual(_Node(review_interval_days=14)
                         .effective_review_interval(), 14)

    def test_explicit_never_is_kept(self):
        self.assertEqual(_Node(review_interval_days=0)
                         .effective_review_interval(), 0)

    def test_inherits_from_a_project_above(self):
        proj = _Node(review_interval_days=14)
        t = _Node(kind="task", parent=proj)
        self.assertEqual(t.effective_review_interval(), 14)

    def test_unset_falls_back_to_the_default(self):
        self.assertEqual(_Node().effective_review_interval(),
                         DEFAULT_REVIEW_INTERVAL_DAYS)

    def test_a_folders_zero_does_not_silence_the_project_below(self):
        folder = _Node(kind="folder", review_interval_days=0)
        proj = _Node(parent=folder)
        self.assertEqual(proj.effective_review_interval(),
                         DEFAULT_REVIEW_INTERVAL_DAYS)
        self.assertTrue(proj.is_review_due(NOW))

    def test_a_projects_zero_still_silences_the_task_below(self):
        proj = _Node(review_interval_days=0)
        t = _Node(kind="task", parent=proj)
        self.assertEqual(t.effective_review_interval(), 0)
        self.assertFalse(t.is_review_due(NOW))

    def test_a_folder_reads_the_default_cadence(self):
        folder = _Node(kind="folder", review_interval_days=0)
        self.assertEqual(folder.effective_review_interval(),
                         DEFAULT_REVIEW_INTERVAL_DAYS)
        self.assertTrue(folder.is_review_due(NOW))

    def test_a_reviewed_folder_is_quiet_until_the_cadence_is_up(self):
        folder = _Node(kind="folder", review_interval_days=0,
                       last_reviewed_at=NOW - timedelta(days=3))
        self.assertFalse(folder.is_review_due(NOW))
        folder.last_reviewed_at = NOW - timedelta(days=8)
        self.assertTrue(folder.is_review_due(NOW))


class FolderBlocksNothingTest(unittest.TestCase):
    """``Item._own_block`` answers for a folder before it reads anything else —
    a folder carries no defer date, no status and no sequential order that a
    user could see or clear."""

    class _Folder:
        kind = "folder"
        status = "waiting"          # nonsense on a folder; must be ignored
        defer_date = NOW + timedelta(days=5)
        followup_at = NOW + timedelta(days=5)

    def test_folder_blocks_nothing(self):
        self.assertFalse(Item._own_block(self._Folder(), NOW))


if __name__ == "__main__":
    unittest.main()
