"""services/zammad_team — whose task is a ticket that changed hands.

A ticket is filed under the Cairn account its Zammad owner maps to, once, at
import time. Everything after that is an *assignment*: the owner keeps the task
where it is, the person now doing the work is assigned it, and that is what puts
it in their Inbox and — the reason this matters — in their search.

The case that made these tests exist: the roster could not name the new owner on
the day of the hand-over, the mirror gave up silently, and nothing ever asked
about that ticket again. It sat on a colleague's account for two days while its
owner searched for it and found nothing. So the rules are tested here, including
"asked again later".

Run with ``python -m unittest discover tests``.
"""
import unittest

from application.services.zammad_team import KEEP, _assignment_for, _owner_ids


class _User:
    """A Cairn account, as far as the assignment rules are concerned."""

    def __init__(self, pk):
        self.pk = pk


SVEN = _User("sven")
BASTIAN = _User("bastian")
JASMIN = _User("jasmin")
ROSTER = {"sven", "bastian", "jasmin"}


class AssignmentRules(unittest.TestCase):

    def test_handed_over_assigns_the_new_owner(self):
        # Task owned by Sven (that is where the import put it), ticket now
        # Bastian's: he gets it assigned, the filing stays Sven's.
        self.assertIs(
            _assignment_for("sven", None, BASTIAN, ROSTER), BASTIAN)

    def test_already_says_so_is_no_write(self):
        self.assertIs(
            _assignment_for("sven", "bastian", BASTIAN, ROSTER), KEEP)

    def test_handed_on_moves_the_assignment(self):
        self.assertIs(
            _assignment_for("sven", "bastian", JASMIN, ROSTER), JASMIN)

    def test_handed_back_clears_a_delegation_this_sync_could_have_made(self):
        self.assertIsNone(
            _assignment_for("sven", "bastian", SVEN, ROSTER))

    def test_handed_back_leaves_someone_else_s_decision_alone(self):
        # The assignee isn't in the roster, so no sync put them there — a
        # person did, and it is not ours to undo.
        self.assertIs(
            _assignment_for("sven", "outsider", SVEN, ROSTER), KEEP)

    def test_owner_nobody_can_name_changes_nothing(self):
        # No Cairn account for this Zammad agent: leave the assignment be.
        # Clearing it here would lose a real one.
        self.assertIs(_assignment_for("sven", "bastian", None, ROSTER), KEEP)
        self.assertIs(_assignment_for("sven", None, None, ROSTER), KEEP)

    def test_owner_owns_it_and_nobody_is_assigned(self):
        self.assertIs(_assignment_for("sven", None, SVEN, ROSTER), KEEP)


class OwnersSeen(unittest.TestCase):
    """What the fetch reports back about tickets it did not hand on.

    The list path reads every ticket anyway and only then drops the untouched
    ones; knowing who owns those costs nothing, and it is what lets a later run
    repair a ticket nobody has edited since."""

    def test_keyed_like_the_task_s_external_id(self):
        rows = [{"id": 322, "owner_id": 4}, {"id": 566, "owner_id": 1}]
        self.assertEqual(_owner_ids(rows),
                         {"zammad:322": "4", "zammad:566": "1"})

    def test_a_row_without_an_id_is_not_a_ticket(self):
        self.assertEqual(_owner_ids([{"owner_id": 4}]), {})

    def test_no_owner_reads_as_no_owner_not_as_none(self):
        self.assertEqual(_owner_ids([{"id": 7}]), {"zammad:7": ""})


class ReconcileAgainstTheDatabase(unittest.TestCase):
    """The pass itself, over real documents.

    The rules above are pure; this is the part that reads rows and writes
    assignments — a projected query, the stored owner id, and the override for
    the tickets this run did fetch. Needs a MongoDB (``TestConfig`` points at
    ``bktodo_test``); skipped when there isn't one."""

    @classmethod
    def setUpClass(cls):
        try:
            from mongoengine import disconnect
            from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
        except ImportError:  # pragma: no cover - no mongoengine installed
            raise unittest.SkipTest("mongoengine not installed")
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()          # forces a real round trip
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")
        cls.ctx = cls.app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "ctx"):
            cls.ctx.pop()

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        self.Item, self.User = Item, User
        for u in User.objects(username__in=("t-sven", "t-bastian")):
            Item.all_objects(owner=u).delete()
            u.delete()
        self.sven = User(username="t-sven", email="t-sven@example.com",
                         display_name="Sven", password_hash="x").save()
        self.bastian = User(username="t-bastian", email="t-bastian@example.com",
                            display_name="Bastian", password_hash="x").save()
        self.members = [self.sven, self.bastian]
        self.owners = {"4": self.bastian, "3": self.sven}
        self.roster = {str(self.sven.pk), str(self.bastian.pk)}

    def tearDown(self):
        for u in (self.sven, self.bastian):
            self.Item.all_objects(owner=u).delete()
            u.delete()

    def _ticket(self, zid, *, owner=None, external_owner="", assigned_to=None):
        return self.Item(owner=owner or self.sven, kind="task",
                         title=f"Ticket {zid}", status="open", source="zammad",
                         external_id=f"zammad:{zid}", external_ref=str(zid),
                         external_owner=external_owner,
                         assigned_to=assigned_to).save()

    def _run(self, owner_by_ext=None, *, dry_run=False):
        from application.services.zammad_team import _reconcile_assignments
        return _reconcile_assignments(self.members, self.owners, self.roster,
                                      owner_by_ext or {}, dry_run=dry_run)

    def test_a_ticket_this_run_never_fetched_is_still_put_right(self):
        # The whole point: nobody has touched this ticket since the day the
        # roster could not name its owner. Its stored owner id is enough.
        task = self._ticket(566, external_owner="4")
        self.assertEqual(self._run(), 1)
        self.assertEqual(str(task.reload().assigned_to.pk), str(self.bastian.pk))

    def test_the_fetch_overrides_what_the_task_remembers(self):
        task = self._ticket(322, external_owner="3")      # owner as of last run
        self.assertEqual(self._run({"zammad:322": "4"}), 1)
        self.assertEqual(str(task.reload().assigned_to.pk), str(self.bastian.pk))

    def test_a_settled_assignment_costs_no_write(self):
        task = self._ticket(322, external_owner="4", assigned_to=self.bastian)
        before = task.reload().assigned_at
        self.assertEqual(self._run(), 0)
        self.assertEqual(task.reload().assigned_at, before)

    def test_an_owner_the_roster_cannot_name_is_left_alone(self):
        task = self._ticket(700, external_owner="99", assigned_to=self.bastian)
        self.assertEqual(self._run(), 0)
        self.assertEqual(str(task.reload().assigned_to.pk), str(self.bastian.pk))

    def test_handed_back_in_zammad_clears_the_assignment(self):
        task = self._ticket(322, external_owner="3", assigned_to=self.bastian)
        self.assertEqual(self._run(), 1)
        self.assertIsNone(task.reload().assigned_to)

    def test_a_dry_run_reports_and_writes_nothing(self):
        task = self._ticket(566, external_owner="4")
        self.assertEqual(self._run(dry_run=True), 1)
        self.assertIsNone(task.reload().assigned_to)

    def test_a_trashed_task_is_not_woken_up(self):
        from datetime import datetime, timezone
        task = self._ticket(566, external_owner="4")
        task.trashed_at = datetime.now(timezone.utc)
        task.save()
        self.assertEqual(self._run(), 0)


if __name__ == "__main__":
    unittest.main()
