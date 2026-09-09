"""views/caldav._held_back — what the Reminders sync is allowed to deliver.

A task deferred into the future, or parked in a waiting/someday status, isn't
visible in Cairn yet, so it must not sit in a Reminders list either. The walk is
the expensive part to get wrong: an ancestor holds the whole subtree back, and
the chain is read from the pre-loaded node map so a full listing stays one
query.

Run with ``python -m unittest discover tests``.
"""
import unittest
from datetime import datetime, timedelta, timezone

from application.views.caldav import _held_back

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


class _Item:
    """Enough of an Item for the walk: a pk, a defer date, a status, a parent.

    The parent lives in ``_data`` and is only handed out through ``.parent``,
    the way mongoengine holds a reference — reading that attribute is the
    query the walk must not make once it has the node map."""

    _next = 0

    def __init__(self, defer_date=None, parent=None, status="open", kind="task"):
        _Item._next += 1
        self.pk = _Item._next
        self.defer_date = defer_date
        self.status = status
        self.kind = kind
        self._data = {"parent": parent}
        self.dereferenced = 0

    @property
    def parent(self):
        self.dereferenced += 1
        return self._data["parent"]


def _nodes(*items):
    return {str(i.pk): i for i in items}


class HeldBackTest(unittest.TestCase):

    def test_no_defer_date_is_delivered(self):
        t = _Item()
        self.assertFalse(_held_back(t, _nodes(t), NOW))

    def test_future_defer_is_held_back(self):
        t = _Item(defer_date=NOW + timedelta(hours=1))
        self.assertTrue(_held_back(t, _nodes(t), NOW))

    def test_past_defer_is_delivered_again(self):
        t = _Item(defer_date=NOW - timedelta(minutes=1))
        self.assertFalse(_held_back(t, _nodes(t), NOW))

    def test_naive_defer_date_reads_as_utc(self):
        t = _Item(defer_date=(NOW + timedelta(hours=1)).replace(tzinfo=None))
        self.assertTrue(_held_back(t, _nodes(t), NOW))

    def test_deferred_ancestor_holds_the_subtree(self):
        proj = _Item(defer_date=NOW + timedelta(days=2))
        section = _Item(parent=proj)
        t = _Item(parent=section)
        self.assertTrue(_held_back(t, _nodes(proj, section, t), NOW))

    def test_available_ancestors_deliver_the_task(self):
        proj = _Item(defer_date=NOW - timedelta(days=2))
        t = _Item(parent=proj)
        self.assertFalse(_held_back(t, _nodes(proj, t), NOW))

    def test_the_walk_never_dereferences_when_it_has_the_map(self):
        # One query per task on every poll is what this map exists to avoid.
        proj = _Item(defer_date=NOW + timedelta(days=2))
        t = _Item(parent=proj)
        self.assertTrue(_held_back(t, _nodes(proj, t), NOW))
        self.assertEqual((t.dereferenced, proj.dereferenced), (0, 0))

    def test_without_a_map_the_chain_is_dereferenced(self):
        # The single-item path (DELETE) has no map and may pay per level.
        proj = _Item(defer_date=NOW + timedelta(days=1))
        t = _Item(parent=proj)
        self.assertTrue(_held_back(t, None, NOW))
        self.assertEqual(t.dereferenced, 1)

    def test_a_parent_absent_from_the_map_ends_the_walk(self):
        # _resolve_ancestors closes the chain before the walk runs, so a miss
        # here means the ancestor really is gone — not a reason to go asking.
        proj = _Item(defer_date=NOW + timedelta(days=1))
        t = _Item(parent=proj)
        self.assertFalse(_held_back(t, _nodes(t), NOW))
        self.assertEqual(proj.dereferenced, 0)

    def test_waiting_is_held_back(self):
        for status in ("waiting", "waiting_reply", "someday"):
            with self.subTest(status=status):
                t = _Item(status=status)
                self.assertTrue(_held_back(t, _nodes(t), NOW))

    def test_waiting_ancestor_holds_the_subtree(self):
        parent = _Item(status="waiting_reply")
        t = _Item(parent=parent)
        self.assertTrue(_held_back(t, _nodes(parent, t), NOW))

    def test_open_task_under_an_open_project_is_delivered(self):
        proj = _Item()
        t = _Item(parent=proj)
        self.assertFalse(_held_back(t, _nodes(proj, t), NOW))

    def test_a_folder_holds_nothing_back(self):
        # A folder has no execution semantics; a defer date can only have come
        # from the API or an import, and honouring it hid the whole folder.
        folder = _Item(kind="folder", defer_date=NOW + timedelta(days=5))
        t = _Item(parent=folder)
        self.assertFalse(_held_back(t, _nodes(folder, t), NOW))

    def test_parent_cycle_terminates(self):
        a = _Item()
        b = _Item(parent=a)
        a._data["parent"] = b
        self.assertFalse(_held_back(b, _nodes(a, b), NOW))


if __name__ == "__main__":
    unittest.main()
