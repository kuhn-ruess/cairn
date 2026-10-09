"""The old rolling habits move into the 12 Week Year without loss.

Needs a MongoDB (``TestConfig`` -> ``bktodo_test``); skipped without one.
Pinned: each target becomes one habit tactic with its check-ins as dates,
the raw habit documents are backed up first and never changed, a second run
copies nothing, a running cycle is used and otherwise one is created, and the
old /habits addresses lead to the new page.
"""
import unittest
from datetime import date, datetime, timezone

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover
    disconnect = None


@unittest.skipIf(disconnect is None, "mongoengine not installed")
class HabitMigrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls._wipe()
        disconnect(alias="default")

    @staticmethod
    def _wipe():
        from application.models.habit import Habit
        from application.models.twelve_week import TwelveWeekYear
        from application.models.user import User
        from application.services.habit_migration import BACKUP_COLLECTION
        Habit.objects.delete()
        Habit._get_collection().database[BACKUP_COLLECTION].delete_many({})
        TwelveWeekYear.objects.delete()
        User.objects.delete()

    def setUp(self):
        from application.models.habit import Habit, HabitCheckin, HabitTarget
        from application.models.user import User
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._wipe()
        self.user = User(username="hm", email="hm@test.invalid", password_hash="x",
                         level="premium", timezone="Europe/Berlin").save()
        self.habit = Habit(owner=self.user, name="Training", period_days=14, targets=[
            HabitTarget(tid="run", name="Run", count=4),
            HabitTarget(tid="str", name="Strength", count=6)], checkins=[
            # 23:30 UTC is the next day in Berlin: the date is the user's.
            HabitCheckin(tid="run", at=datetime(2026, 10, 5, 23, 30, tzinfo=timezone.utc)),
            HabitCheckin(tid="run", at=datetime(2026, 9, 1, 8, tzinfo=timezone.utc)),
            HabitCheckin(tid="str", at=datetime(2026, 10, 7, 8, tzinfo=timezone.utc)),
        ]).save()
        Habit(owner=self.user, name="Read", period_days=7,
              targets=[HabitTarget(tid="r", name="Read", count=7)]).save()
        self.now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.ctx.pop()

    def _raw(self):
        from application.models.habit import Habit
        return sorted(Habit._get_collection().find({}), key=lambda d: d["name"])

    def test_targets_become_habit_tactics_with_their_history(self):
        from application.models.twelve_week import TwelveWeekYear
        from application.services import habit_migration as hm
        before = self._raw()
        st = hm.migrate_user(self.user, now=self.now)
        self.assertEqual((st["tactics"], st["checkins"], st["backed_up"]), (3, 3, 2))
        self.assertTrue(st["created_cycle"])
        cycle = TwelveWeekYear.objects.get()
        self.assertEqual(cycle.title, "Habits")
        self.assertEqual(cycle.start.date(), date(2026, 10, 5))
        by_title = {t.title: t for t in cycle.tactics}
        run = by_title["Training: Run"]
        self.assertEqual(run.gid, "")
        self.assertTrue(run.recurring)
        self.assertEqual(run.per_week, 2)                    # 4 in 14 days
        self.assertEqual(run.log, ["2026-09-01", "2026-10-06"])
        self.assertEqual(by_title["Training: Strength"].per_week, 3)
        self.assertEqual(by_title["Read"].per_week, 7)
        self.assertEqual(run.source, f"habit:{self.habit.pk}:run")
        # Old documents untouched, and their copies are in the backup.
        self.assertEqual(self._raw(), before)
        backup = hm.Habit._get_collection().database[hm.BACKUP_COLLECTION]
        self.assertEqual(backup.count_documents({}), 2)
        self.user.reload()
        self.assertTrue(self.user.habits_migrated)

    def test_second_run_copies_nothing(self):
        from application.models.twelve_week import TwelveWeekYear
        from application.services import habit_migration as hm
        hm.migrate_user(self.user, now=self.now)
        st = hm.migrate_user(self.user, now=self.now)
        self.assertEqual((st["tactics"], st["skipped"]), (0, 3))
        self.assertEqual(len(TwelveWeekYear.objects.get().tactics), 3)
        self.assertIsNone(hm.ensure_migrated(self.user))

    def test_a_running_cycle_takes_them(self):
        from application.models.twelve_week import TwelveWeekYear, TwyGoal
        from application.services import habit_migration as hm
        from application.services import twelve_week as tw
        running = TwelveWeekYear(owner=self.user, title="Q4", start=tw.to_start(date(2026, 9, 28)),
                                 goals=[TwyGoal(gid="g", title="Fit")]).save()
        st = hm.migrate_user(self.user, now=self.now)
        self.assertFalse(st["created_cycle"])
        self.assertEqual(TwelveWeekYear.objects.count(), 1)
        running.reload()
        self.assertEqual(len(running.tactics), 3)

    def test_dry_run_writes_nothing(self):
        from application.models.twelve_week import TwelveWeekYear
        from application.services import habit_migration as hm
        st = hm.migrate_user(self.user, dry_run=True, now=self.now)
        self.assertEqual(st["tactics"], 3)
        self.assertEqual(TwelveWeekYear.objects.count(), 0)
        self.user.reload()
        self.assertFalse(self.user.habits_migrated)

    def test_old_addresses_redirect_and_first_visit_migrates(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True
        for url in ("/habits/", f"/habits/{self.habit.pk}/edit"):
            resp = client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertTrue(resp.headers["Location"].endswith("/habits/12-week-year/"))
        resp = client.post(f"/habits/{self.habit.pk}/checkin", data={"tid": "run"})
        self.assertEqual(resp.status_code, 302)
        html = client.get("/habits/12-week-year/", follow_redirects=True).get_data(as_text=True)
        self.assertIn("moved in here", html)
        from application.models.twelve_week import TwelveWeekYear
        self.assertEqual(len(TwelveWeekYear.objects.get().tactics), 3)
