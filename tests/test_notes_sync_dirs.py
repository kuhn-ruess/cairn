"""services/notes_sync — removing the directory a trashed folder left behind.

Run with ``python -m unittest discover tests``. This is the one place in the
mirror that deletes a COLLECTION rather than a file, so the rule that decides
it gets tested on its own: a single file of the user's, anywhere underneath,
and the directory stays.
"""
import unittest

from application.services import notes_sync
from application.services.webdav import DavEntry, DavError


class FakeClient:
    """Answers walks from a flat list of paths, and records what was deleted."""

    def __init__(self, tree=None, fail=()):
        self.tree = list(tree or [])
        self.fail = set(fail)
        self.deleted = []

    def walk(self, path="", **kw):
        if path in self.fail:
            raise DavError("nope")
        return [DavEntry(path=p, is_dir=d) for p, d in self.tree
                if p.startswith(path + "/")]

    def delete(self, path):
        self.deleted.append(path)


class Stub:
    """A _Round with only the parts this decision touches."""

    _only_our_leftovers = notes_sync._Round._only_our_leftovers
    drop_dirs_of_trashed_folders = notes_sync._Round.drop_dirs_of_trashed_folders

    def __init__(self, client, paths=(), may_delete=True, dry_run=False):
        self.client = client
        self.user = object()
        self.stats = notes_sync.MirrorStats()
        self.may_delete = may_delete
        self.dry_run = dry_run
        self._paths = {str(i): p for i, p in enumerate(paths)}


class EmptyEnoughTest(unittest.TestCase):
    def only(self, tree):
        return Stub(FakeClient(tree))._only_our_leftovers("Work")

    def test_nothing_in_it(self):
        self.assertTrue(self.only([]))

    def test_our_own_assets_leftovers_do_not_count(self):
        self.assertTrue(self.only([("Work/_assets", True),
                                   ("Work/_assets/abc123", True)]))

    def test_one_file_and_it_stays(self):
        self.assertFalse(self.only([("Work/Something.md", False)]))

    def test_a_file_deep_inside_our_assets_still_counts(self):
        """An asset we failed to delete is still somebody's data."""
        self.assertFalse(self.only([("Work/_assets", True),
                                    ("Work/_assets/abc/pic.png", False)]))

    def test_a_folder_of_their_own_and_it_stays(self):
        self.assertFalse(self.only([("Work/Photos", True)]))


class DropDirsTest(unittest.TestCase):
    def run_with(self, tree, paths, **kw):
        client = FakeClient(tree)
        round_ = Stub(client, paths, **kw)
        notes_sync.folder_paths = lambda user, trashed=False: round_._paths
        round_.drop_dirs_of_trashed_folders()
        return client, round_.stats

    def setUp(self):
        self._real = notes_sync.folder_paths

    def tearDown(self):
        notes_sync.folder_paths = self._real

    def test_an_empty_one_goes(self):
        client, stats = self.run_with([], ["Work"])
        self.assertEqual(client.deleted, ["Work"])
        self.assertEqual(stats.folders_deleted, 1)

    def test_one_with_something_in_it_stays(self):
        client, stats = self.run_with([("Work/Keep.md", False)], ["Work"])
        self.assertEqual(client.deleted, [])
        self.assertEqual(stats.folders_deleted, 0)

    def test_deepest_first_so_a_branch_collapses(self):
        client, _ = self.run_with([], ["Work", "Work/Deep/Deeper", "Work/Deep"])
        self.assertEqual(client.deleted, ["Work/Deep/Deeper", "Work/Deep", "Work"])

    def test_nothing_happens_when_the_listing_could_not_be_trusted(self):
        """Same guard that stops an empty or failed listing deleting files."""
        client, stats = self.run_with([], ["Work"], may_delete=False)
        self.assertEqual(client.deleted, [])
        self.assertEqual(stats.folders_deleted, 0)

    def test_a_dry_run_counts_but_does_not_delete(self):
        client, stats = self.run_with([], ["Work"], dry_run=True)
        self.assertEqual(client.deleted, [])
        self.assertEqual(stats.folders_deleted, 1)

    def test_the_root_is_never_touched(self):
        client, _ = self.run_with([], ["", "Work"])
        self.assertEqual(client.deleted, ["Work"])

    def test_a_share_that_will_not_answer_is_reported_not_ignored(self):
        client = FakeClient([], fail={"Work"})
        round_ = Stub(client, ["Work"])
        notes_sync.folder_paths = lambda user, trashed=False: round_._paths
        round_.drop_dirs_of_trashed_folders()
        self.assertEqual(client.deleted, [])
        self.assertTrue(round_.stats.errors)


if __name__ == "__main__":
    unittest.main()
