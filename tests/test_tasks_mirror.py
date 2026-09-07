"""services/tasks_mirror — the tasks half of the WebDAV mirror.

Run with ``python -m unittest discover tests``. No database: the module reads
reference fields through ``_data`` (so a list never dereferences), which is
exactly what lets a plain stub stand in for an ``Item`` here.

What is worth pinning down: a file under ``Tasks/`` must never be deleted —
a task that disappears MOVES to the archive, and a task that is done lives
there too until it is reopened — the notes round must keep its hands off this
subtree, recurring tasks must stay out of it, and a file must only be rewritten
when it would say something new.
"""
import unittest
from datetime import datetime, timezone

from application.services import tasks_mirror as tm
from application.services.webdav import DavEntry, DavError


def dt(day, hour=9):
    return datetime(2026, 9, day, hour, 0, tzinfo=timezone.utc)


class FakeTask:
    """An Item with only the fields the mirror reads."""

    def __init__(self, pk, title, parent=None, **kw):
        self.pk = pk
        self.title = title
        self._data = {"parent": parent, "assigned_to": kw.pop("assigned_to", None)}
        self.kind = kw.pop("kind", "task")
        self.status = kw.pop("status", "open")
        self.tags = kw.pop("tags", [])
        self.note = kw.pop("note", "")
        self.comments = kw.pop("comments", [])
        self.due_date = kw.pop("due_date", None)
        self.flagged = kw.pop("flagged", False)
        self.created_at = kw.pop("created_at", dt(1))
        self.updated_at = kw.pop("updated_at", dt(2))
        self.mirror_path = kw.pop("mirror_path", "")
        self.mirror_hash = kw.pop("mirror_hash", "")
        self.repeat_every_days = kw.pop("repeat_every_days", None)
        self.repeat_weekdays = kw.pop("repeat_weekdays", [])
        self.repeat_monthday = kw.pop("repeat_monthday", None)


class FakeComment:
    def __init__(self, body, created_at, author=None, external_author=""):
        self.body = body
        self.created_at = created_at
        self.external_author = external_author
        self._data = {"author": author}


class FakeClient:
    def __init__(self, tree=(), fail=()):
        self.tree = list(tree)
        self.fail = set(fail)
        self.written = {}
        self.deleted = []
        self.moved = []

    def walk(self, path="", **kw):
        if path in self.fail:
            raise DavError("nope", 404)
        return [DavEntry(path=p, is_dir=d) for p, d in self.tree
                if p.startswith(path + "/")]

    def list(self, path=""):
        return [DavEntry(path=p, is_dir=d) for p, d in self.tree
                if p.rsplit("/", 1)[0] == path]

    def write(self, path, data, content_type=""):
        self.written[path] = data.decode("utf-8")
        return "etag"

    def delete(self, path):
        self.deleted.append(path)

    def move(self, src, dst, overwrite=False):
        self.moved.append((src, dst))


class StubRound:
    """A notes ``_Round`` with only the parts the tasks half touches."""

    def __init__(self, client, dry_run=False):
        from application.services.notes_sync import MirrorStats
        self.client = client
        self.user = object()
        self.stats = MirrorStats()
        self.dry_run = dry_run


class OwnsTest(unittest.TestCase):
    def test_the_subtree_and_nothing_else(self):
        self.assertTrue(tm.owns("Tasks"))
        self.assertTrue(tm.owns("Tasks/Work/Fix it.md"))
        self.assertFalse(tm.owns("Tasksomething/x.md"))
        self.assertFalse(tm.owns("Notes/Tasks/x.md"))


class DirForTest(unittest.TestCase):
    PATHS = {"p1": "Work/Acme", "f1": "Work"}

    def test_a_task_in_a_project(self):
        task = FakeTask("t1", "Fix", parent="p1")
        self.assertEqual(tm.dir_for(task, self.PATHS, {}), "Tasks/Work/Acme")

    def test_a_task_with_no_project_is_inbox(self):
        self.assertEqual(tm.dir_for(FakeTask("t1", "Fix"), self.PATHS, {}),
                         "Tasks/Inbox")

    def test_a_subtask_files_with_its_project_not_under_its_parent(self):
        task = FakeTask("t2", "Sub", parent="t1")
        self.assertEqual(tm.dir_for(task, self.PATHS, {"t1": "p1"}),
                         "Tasks/Work/Acme")

    def test_a_parent_chain_that_loops_still_answers(self):
        task = FakeTask("t2", "Sub", parent="t1")
        self.assertEqual(tm.dir_for(task, self.PATHS, {"t1": "t2", "t2": "t1"}),
                         "Tasks/Inbox")


class RenderTest(unittest.TestCase):
    def test_front_matter_body_and_comments_in_order(self):
        task = FakeTask(
            "abc", "Call the NAS vendor", tags=["Cairn", "Waiting"],
            note="Ask about the WebDAV timeout.", due_date=dt(11),
            assigned_to="u1",
            comments=[
                FakeComment("Second", dt(5), author="u1"),
                FakeComment("First", dt(3), external_author="Zammad"),
            ])
        out = tm.render(task, {"u1": "Bastian Kuhn"})
        self.assertIn("cairn-id: abc", out)
        self.assertIn("title: Call the NAS vendor", out)
        self.assertIn("tags: [Cairn, Waiting]", out)
        self.assertIn("due: 2026-09-11", out)
        self.assertIn("assignee: Bastian Kuhn", out)
        self.assertIn("Ask about the WebDAV timeout.", out)
        self.assertLess(out.index("First"), out.index("Second"))
        self.assertIn("### Zammad — 2026-09-03 09:00", out)
        self.assertIn("### Bastian Kuhn — 2026-09-05 09:00", out)

    def test_no_comments_no_heading(self):
        self.assertNotIn("## Comments", tm.render(FakeTask("a", "x"), {}))

    def test_a_naive_timestamp_does_not_blow_up_the_sort(self):
        task = FakeTask("a", "x", comments=[
            FakeComment("naive", datetime(2026, 9, 4, 9)),
            FakeComment("aware", dt(3)),
        ])
        out = tm.render(task, {})
        self.assertLess(out.index("aware"), out.index("naive"))


class WriteOneTest(unittest.TestCase):
    def setUp(self):
        self.updates = []
        self._real_item = tm.Item
        tm.Item = self._fake_item()

    def tearDown(self):
        tm.Item = self._real_item

    def _fake_item(self):
        updates = self.updates

        class Q:
            def update(self, **kw):
                updates.append(kw)

        class FakeItem:
            @staticmethod
            def all_objects(**kw):
                return Q()

        return FakeItem

    def test_a_new_task_is_written_and_remembered(self):
        round_ = StubRound(FakeClient())
        task = FakeTask("a", "x")
        tm._one(round_, task, "Tasks/Inbox/x.md", "body", set())
        self.assertIn("Tasks/Inbox/x.md", round_.client.written)
        self.assertEqual(round_.stats.tasks_written, 1)
        self.assertEqual(task.mirror_path, "Tasks/Inbox/x.md")

    def test_unchanged_is_not_rewritten(self):
        from application.services.notes_sync import _hash
        round_ = StubRound(FakeClient())
        task = FakeTask("a", "x", mirror_path="Tasks/Inbox/x.md",
                        mirror_hash=_hash("body"))
        tm._one(round_, task, "Tasks/Inbox/x.md", "body", {"Tasks/Inbox/x.md"})
        self.assertEqual(round_.client.written, {})
        self.assertEqual(round_.stats.unchanged, 1)

    def test_a_file_someone_removed_over_there_comes_back(self):
        from application.services.notes_sync import _hash
        round_ = StubRound(FakeClient())
        task = FakeTask("a", "x", mirror_path="Tasks/Inbox/x.md",
                        mirror_hash=_hash("body"))
        tm._one(round_, task, "Tasks/Inbox/x.md", "body", set())
        self.assertIn("Tasks/Inbox/x.md", round_.client.written)

    def test_a_renamed_task_travels_and_leaves_no_second_copy(self):
        round_ = StubRound(FakeClient())
        task = FakeTask("a", "y", mirror_path="Tasks/Inbox/x.md")
        tm._one(round_, task, "Tasks/Inbox/y.md", "body", {"Tasks/Inbox/x.md"})
        self.assertEqual(round_.client.moved,
                         [("Tasks/Inbox/x.md", "Tasks/Inbox/y.md")])
        self.assertEqual(round_.client.deleted, [])
        self.assertIn("Tasks/Inbox/y.md", round_.client.written)

    def test_a_refiled_task_is_moved_and_not_rewritten(self):
        from application.services.notes_sync import _hash
        round_ = StubRound(FakeClient())
        task = FakeTask("a", "x", mirror_path="Tasks/Inbox/x.md",
                        mirror_hash=_hash("body"))
        tm._one(round_, task, "Tasks/Work/x.md", "body", {"Tasks/Inbox/x.md"})
        self.assertEqual(round_.client.moved,
                         [("Tasks/Inbox/x.md", "Tasks/Work/x.md")])
        self.assertEqual(round_.client.written, {})
        self.assertEqual(task.mirror_path, "Tasks/Work/x.md")

    def test_completing_moves_the_file_into_the_archive(self):
        round_ = StubRound(FakeClient())
        task = FakeTask("a", "x", status="done", mirror_path="Tasks/Inbox/x.md")
        tm._one(round_, task, "Tasks/Archive/Inbox/x.md", "done body",
                {"Tasks/Inbox/x.md"})
        self.assertEqual(round_.client.moved,
                         [("Tasks/Inbox/x.md", "Tasks/Archive/Inbox/x.md")])
        self.assertEqual(round_.stats.tasks_archived, 1)
        self.assertEqual(round_.stats.tasks_written, 0)

    def test_reopening_brings_the_file_back_out(self):
        round_ = StubRound(FakeClient())
        task = FakeTask("a", "x", mirror_path="Tasks/Archive/Inbox/x.md")
        tm._one(round_, task, "Tasks/Inbox/x.md", "open again",
                {"Tasks/Archive/Inbox/x.md"})
        self.assertEqual(round_.client.moved,
                         [("Tasks/Archive/Inbox/x.md", "Tasks/Inbox/x.md")])
        self.assertEqual(round_.client.deleted, [])
        self.assertEqual(round_.stats.tasks_written, 1)

    def test_a_move_that_fails_still_writes_the_file(self):
        class Stubborn(FakeClient):
            def move(self, src, dst, overwrite=False):
                raise DavError("locked", 423)

        round_ = StubRound(Stubborn())
        task = FakeTask("a", "y", mirror_path="Tasks/Inbox/x.md")
        tm._one(round_, task, "Tasks/Inbox/y.md", "body", {"Tasks/Inbox/x.md"})
        self.assertIn("Tasks/Inbox/y.md", round_.client.written)
        self.assertEqual(len(round_.stats.errors), 1)

    def test_a_dry_run_writes_nothing(self):
        round_ = StubRound(FakeClient(), dry_run=True)
        tm._one(round_, FakeTask("a", "x"), "Tasks/Inbox/x.md", "body", set())
        self.assertEqual(round_.client.written, {})
        self.assertEqual(round_.stats.tasks_written, 1)
        self.assertEqual(self.updates, [])


class ArchiveTest(unittest.TestCase):
    """The deletion rule: nothing under Tasks/ is ever deleted."""

    def setUp(self):
        self.updates = []
        self.orphan = FakeTask("gone", "Old", mirror_path="Tasks/Inbox/Old.md")
        self._real_item = tm.Item
        tm.Item = self._fake_item()

    def tearDown(self):
        tm.Item = self._real_item

    def _fake_item(self):
        updates, orphan = self.updates, self.orphan

        class Q:
            def __init__(self, kw):
                self.kw = kw

            def only(self, *fields):
                return [orphan] if "mirror_path__in" in self.kw else []

            def update(self, **kw):
                updates.append(kw)

        class FakeItem:
            @staticmethod
            def all_objects(**kw):
                return Q(kw)

        return FakeItem

    def test_an_unclaimed_file_moves_to_the_archive_and_is_forgotten(self):
        round_ = StubRound(FakeClient())
        tm._archive(round_, {"Tasks/Inbox/Old.md"}, ["Tasks/Inbox/Old.md"])
        self.assertEqual(round_.client.moved,
                         [("Tasks/Inbox/Old.md", "Tasks/Archive/Inbox/Old.md")])
        self.assertEqual(round_.client.deleted, [])
        self.assertEqual(round_.stats.tasks_archived, 1)
        self.assertEqual(self.updates, [{"set__mirror_path": "", "set__mirror_hash": ""}])

    def test_the_archive_never_overwrites_what_it_already_holds(self):
        round_ = StubRound(FakeClient())
        tm._archive(round_, {"Tasks/Inbox/Old.md", "Tasks/Archive/Inbox/Old.md"},
                    ["Tasks/Inbox/Old.md"])
        client = round_.client
        self.assertEqual(client.moved,
                         [("Tasks/Inbox/Old.md", "Tasks/Archive/Inbox/Old (2).md")])

    def test_a_dry_run_moves_nothing(self):
        round_ = StubRound(FakeClient(), dry_run=True)
        tm._archive(round_, {"Tasks/Inbox/Old.md"}, ["Tasks/Inbox/Old.md"])
        self.assertEqual(round_.client.moved, [])
        self.assertEqual(self.updates, [])


class ListingTest(unittest.TestCase):
    def test_the_archive_is_part_of_the_listing(self):
        client = FakeClient([("Tasks/Inbox/A.md", False),
                             ("Tasks/Archive/Inbox/B.md", False),
                             ("Tasks/Inbox", True)])
        self.assertEqual(tm._listing(StubRound(client)),
                         {"Tasks/Inbox/A.md", "Tasks/Archive/Inbox/B.md"})

    def test_a_share_without_the_folder_yet_is_simply_empty(self):
        round_ = StubRound(FakeClient(fail=("Tasks",)))
        self.assertEqual(tm._listing(round_), set())
        self.assertEqual(round_.stats.errors, [])


class WantPathTest(unittest.TestCase):
    """Where a file goes: the project tree, or the same place in the archive."""

    PATHS = {"p1": "Work/Acme"}

    def want(self, task, taken=(), present=()):
        return tm.want_path(task, self.PATHS, {}, set(taken), set(present))

    def test_an_open_task_files_under_its_project(self):
        self.assertEqual(self.want(FakeTask("t1", "Fix", parent="p1")),
                         "Tasks/Work/Acme/Fix.md")

    def test_a_done_task_files_in_the_archive_at_the_same_place(self):
        task = FakeTask("t1", "Fix", parent="p1", status="done")
        self.assertEqual(self.want(task), "Tasks/Archive/Work/Acme/Fix.md")

    def test_a_dropped_task_goes_there_too(self):
        task = FakeTask("t1", "Fix", status="dropped")
        self.assertEqual(self.want(task), "Tasks/Archive/Inbox/Fix.md")

    def test_reopening_puts_it_back_in_the_live_tree(self):
        task = FakeTask("t1", "Fix", parent="p1", status="open",
                        mirror_path="Tasks/Archive/Work/Acme/Fix.md")
        self.assertEqual(self.want(task), "Tasks/Work/Acme/Fix.md")

    def test_two_tasks_of_the_same_name_do_not_share_a_file(self):
        task = FakeTask("aabbccddeeff", "Fix", parent="p1")
        self.assertEqual(self.want(task, taken={"tasks/work/acme/fix.md"}),
                         "Tasks/Work/Acme/Fix (ddeeff).md")

    def test_a_file_already_there_is_never_overwritten(self):
        """A forgotten history in the archive is claimed by nobody — a new
        task of that name must not land on top of it."""
        task = FakeTask("aabbccddeeff", "Fix", status="done")
        self.assertEqual(self.want(task, present={"Tasks/Archive/Inbox/Fix.md"}),
                         "Tasks/Archive/Inbox/Fix (ddeeff).md")

    def test_the_task_s_own_file_is_not_a_clash(self):
        task = FakeTask("t1", "Fix", mirror_path="Tasks/Inbox/Fix.md")
        self.assertEqual(self.want(task, present={"Tasks/Inbox/Fix.md"}),
                         "Tasks/Inbox/Fix.md")


class RepeatsTest(unittest.TestCase):
    """A recurring task is not mirrored: the next occurrence is the point."""

    def test_a_plain_task_is_written(self):
        self.assertFalse(tm.repeats(FakeTask("t1", "Fix")))

    def test_every_n_days(self):
        self.assertTrue(tm.repeats(FakeTask("t1", "Spanish", repeat_every_days=1)))

    def test_on_weekdays(self):
        self.assertTrue(tm.repeats(FakeTask("t1", "Review", repeat_weekdays=[4])))

    def test_on_a_monthday(self):
        self.assertTrue(tm.repeats(FakeTask("t1", "Rent", repeat_monthday=1)))


class ArchivedPathTest(unittest.TestCase):
    def test_the_tree_repeats_itself_under_archive(self):
        self.assertEqual(tm.archived("Tasks/Work/Acme"), "Tasks/Archive/Work/Acme")
        self.assertEqual(tm.archived("Tasks/Inbox/x.md"), "Tasks/Archive/Inbox/x.md")

    def test_the_root_itself(self):
        self.assertEqual(tm.archived("Tasks"), "Tasks/Archive")


if __name__ == "__main__":
    unittest.main()
