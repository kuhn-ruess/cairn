"""The 12 Week Year: week math, scoring, the Habitify read, the pages.

Run with ``python -m unittest discover tests``. The first two classes are
pure; the last needs a MongoDB (``TestConfig`` -> ``bktodo_test``) and is
skipped without one. Habitify is never called: ``habitify._fetch`` is
replaced in every test that would reach it.
"""
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from application.models.twelve_week import TwelveWeekYear, TwyGoal, TwyTactic
from application.services import habitify
from application.services import twelve_week as tw

START = date(2026, 10, 5)  # a Monday


def _cycle(**kw):
    c = TwelveWeekYear(title="Q4", start=tw.to_start(START), **kw)
    c.goals = [TwyGoal(gid="g1", title="Fit", lag_target=6, lag_current=1.5, lag_unit="kg")]
    c.tactics = [
        TwyTactic(tid="run", gid="g1", title="Run", recurring=True),
        TwyTactic(tid="plan", gid="g1", title="Meal plan", weeks=[1, 2]),
        TwyTactic(tid="gym", gid="g1", title="Gym", recurring=True,
                  habitify_id="h-gym", per_week=3),
    ]
    return c


class WeekMathTests(unittest.TestCase):

    def test_week_of(self):
        c = _cycle()
        self.assertEqual(tw.week_of(c, date(2026, 10, 4)), 0)
        self.assertEqual(tw.week_of(c, START), 1)
        self.assertEqual(tw.week_of(c, date(2026, 10, 11)), 1)
        self.assertEqual(tw.week_of(c, date(2026, 10, 12)), 2)
        self.assertEqual(tw.week_of(c, date(2026, 12, 27)), 12)
        self.assertEqual(tw.week_of(c, date(2026, 12, 28)), 13)
        self.assertEqual(tw.week_of(c, date(2027, 1, 4)), 14)
        self.assertEqual(tw.week_of(c, date(2027, 6, 1)), 14)

    def test_week_range_and_end(self):
        c = _cycle()
        self.assertEqual(tw.week_range(c, 2), (date(2026, 10, 12), date(2026, 10, 18)))
        self.assertEqual(tw.end_date(c), date(2027, 1, 3))

    def test_start_read_back_from_an_aware_datetime(self):
        c = TwelveWeekYear(title="x", start=datetime(2026, 10, 5, tzinfo=timezone.utc))
        self.assertEqual(tw.as_date(c.start), START)
        self.assertEqual(tw.monday_of(date(2026, 10, 9)), START)

    def test_parse_and_format_weeks(self):
        self.assertEqual(tw.parse_weeks("1-4, 6;9, x, 0, 13, 12-11"), [1, 2, 3, 4, 6, 9, 11, 12])
        self.assertEqual(tw.parse_weeks(""), [])
        self.assertEqual(tw.format_weeks([6, 1, 2, 3, 9, 10]), "1-3, 6, 9-10")


class ScoringTests(unittest.TestCase):

    def test_planned(self):
        c = _cycle()
        run, plan, _ = c.tactics
        self.assertTrue(tw.planned(run, 12))
        self.assertFalse(tw.planned(run, 13))
        self.assertTrue(tw.planned(plan, 2))
        self.assertFalse(tw.planned(plan, 3))

    def test_week_score_counts_ticks_and_habitify(self):
        c = _cycle()
        c.done = ["run:1"]
        c.habitify_counts = {"gym:1": 3, "gym:2": 2}
        self.assertEqual(tw.week_score(c, 1), {"week": 1, "planned": 3, "done": 2, "score": 67})
        # Two Habitify days of three is not done.
        self.assertEqual(tw.week_score(c, 2)["done"], 0)
        # Week 3: run and gym planned, nothing done.
        self.assertEqual(tw.week_score(c, 3)["planned"], 2)

    def test_empty_week_has_no_score(self):
        c = _cycle()
        c.tactics = [TwyTactic(tid="a", gid="g1", title="once", weeks=[5])]
        self.assertIsNone(tw.week_score(c, 1)["score"])

    def test_scoreboard_averages_finished_weeks_only(self):
        c = _cycle()
        c.done = ["run:1", "plan:1", "run:2", "plan:2"]
        c.habitify_counts = {"gym:1": 4, "gym:2": 0}
        board = tw.scoreboard(c, date(2026, 10, 20))  # week 3
        self.assertEqual(board["current"], 3)
        self.assertEqual([w["score"] for w in board["weeks"][:3]], [100, 67, 0])
        self.assertEqual(board["average"], 84)   # (100 + 67) / 2, week 3 still running
        self.assertFalse(board["on_track"])

    def test_toggle_only_planned(self):
        c = _cycle()
        self.assertTrue(tw.toggle_done(c, "plan", 1))
        self.assertIn("plan:1", c.done)
        self.assertFalse(tw.toggle_done(c, "plan", 1))
        self.assertNotIn("plan:1", c.done)
        self.assertIsNone(tw.toggle_done(c, "plan", 5))
        self.assertIsNone(tw.toggle_done(c, "nope", 1))

    def test_goal_progress(self):
        c = _cycle()
        self.assertEqual(tw.goal_progress(c.goals[0]), 25)
        self.assertIsNone(tw.goal_progress(TwyGoal(gid="x", title="no number")))


class HabitifyParsingTests(unittest.TestCase):

    STATS = {"data": {"id": "h-gym", "name": "Gym", "dailyProgress": [
        {"date": "2026-10-05", "totalLog": 1, "status": "completed"},
        {"date": "2026-10-06", "totalLog": 0, "status": "failed"},
        {"date": "2026-10-07", "totalLog": 1, "status": "completed"},
        {"date": "2026-10-12", "totalLog": 1, "status": "completed"},
        {"date": "garbage", "status": "completed"},
        {"date": "2026-10-13", "status": "skipped"},
    ]}}

    def test_completed_days(self):
        days = habitify.parse_completed_days(self.STATS)
        self.assertEqual(days, {date(2026, 10, 5), date(2026, 10, 7), date(2026, 10, 12)})
        self.assertEqual(habitify.parse_completed_days({"data": None}), set())
        self.assertEqual(habitify.parse_completed_days("nonsense"), set())

    def test_count_by_week_ignores_days_outside_the_execution_weeks(self):
        c = _cycle()
        days = {date(2026, 10, 5), date(2026, 10, 7), date(2026, 10, 12),
                date(2026, 10, 1), date(2026, 12, 30)}
        self.assertEqual(habitify.count_by_week(c, "gym", days), {"gym:1": 2, "gym:2": 1})

    def test_list_habits_pages(self):
        pages = [
            {"data": [{"id": f"h{i}", "name": f"H{i}"} for i in range(100)],
             "pagination": {"total": 101, "limit": 100, "offset": 0}},
            {"data": [{"id": "h100", "name": "last"}],
             "pagination": {"total": 101, "limit": 100, "offset": 100}},
        ]
        calls = []

        def fake(path, key, params=None):
            calls.append((path, key, dict(params)))
            return pages[len(calls) - 1]

        with mock.patch.object(habitify, "_fetch", side_effect=fake):
            habits = habitify.list_habits("KEY")
        self.assertEqual(len(habits), 101)
        self.assertEqual(habits[-1], {"id": "h100", "name": "last"})
        self.assertEqual(calls[1][2]["offset"], 100)
        self.assertEqual(calls[0][1], "KEY")

    def test_list_habits_rejects_a_strange_shape(self):
        with mock.patch.object(habitify, "_fetch", return_value={"data": {"x": 1}}):
            with self.assertRaises(habitify.HabitifyError):
                habitify.list_habits("KEY")

    def test_the_request_uses_v2_and_the_key_header(self):
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"data": []}'

        def fake_open(req, timeout=None):
            seen["url"] = req.full_url
            seen["key"] = req.get_header("X-api-key")
            return Resp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            habitify.completed_days("KEY", "a/b", date(2026, 10, 5), date(2026, 10, 11))
        self.assertEqual(seen["key"], "KEY")
        self.assertTrue(seen["url"].startswith(
            "https://api.habitify.me/v2/habits/a%2Fb/statistics?"))
        self.assertIn("startDate=2026-10-05", seen["url"])
        self.assertIn("endDate=2026-10-11", seen["url"])


try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover
    disconnect = None


@unittest.skipIf(disconnect is None, "mongoengine not installed")
class TwelveWeekDbTests(unittest.TestCase):

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
        from application.models.user import User
        TwelveWeekYear.objects.delete()
        User.objects.delete()

    def setUp(self):
        from application.models.user import User
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._wipe()
        self.user = User(username="twy", email="twy@test.invalid",
                         password_hash="x", level="premium").save()
        self.other = User(username="twy2", email="twy2@test.invalid",
                          password_hash="x", level="premium").save()
        self.cycle = _cycle(owner=self.user)
        self.cycle.save()
        self.client = self._login(self.user)

    def tearDown(self):
        self.ctx.pop()

    def _login(self, user):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.pk)
            sess["_fresh"] = True
        return client

    def _url(self, suffix=""):
        return f"/habits/12-week-year/{self.cycle.pk}{suffix}"

    def test_pages_render_without_habitify(self):
        with mock.patch.object(habitify, "_fetch") as fetch:
            for url in ("/habits/12-week-year/", self._url(), self._url("?week=13"),
                        self._url("/plan"), "/habits/12-week-year/new", "/habits/",
                        "/settings/habitify"):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200, url)
            fetch.assert_not_called()
        html = self.client.get(self._url("?week=1")).get_data(as_text=True)
        self.assertIn("Meal plan", html)
        self.assertNotIn("Read Habitify", html)

    def test_toggle_and_note(self):
        resp = self.client.post(self._url("/toggle"), data={"tid": "plan", "week": "1"})
        self.assertEqual(resp.status_code, 200)
        self.cycle.reload()
        self.assertIn("plan:1", self.cycle.done)
        self.assertEqual(self.client.post(self._url("/toggle"),
                                          data={"tid": "plan", "week": "7"}).status_code, 400)
        self.client.post(self._url("/note"), data={"week": "13", "note": "celebrate"})
        self.cycle.reload()
        self.assertEqual(self.cycle.week_notes, {"13": "celebrate"})

    def test_another_user_gets_404(self):
        other = self._login(self.other)
        self.assertEqual(other.get(self._url()).status_code, 404)
        self.assertEqual(other.post(self._url("/toggle"),
                                    data={"tid": "plan", "week": "1"}).status_code, 404)
        self.assertEqual(other.post(self._url("/delete")).status_code, 404)
        self.assertEqual(TwelveWeekYear.objects.count(), 1)

    def test_plan_save_keeps_ids_and_drops_removed_tactics(self):
        self.cycle.done = ["plan:1", "run:1"]
        self.cycle.save()
        data = {
            "title": "Q4 new", "vision": "v", "start": "2026-10-05",
            "goal_gid": ["g1", "new2", "new3"], "goal_title": ["Fit", "Write", ""],
            "goal_lag_target": ["6", "", ""], "goal_lag_current": ["2,5", "", ""],
            "goal_lag_unit": ["kg", "", ""],
            "tactic_tid": ["run", "", ""], "tactic_title": ["Run", "Draft", ""],
            "tactic_goal": ["g1", "new2", "g1"], "tactic_mode": ["recurring", "weeks", "weeks"],
            "tactic_weeks": ["", "2-3", ""], "tactic_habitify": ["", "", ""],
            "tactic_per_week": ["1", "1", "1"],
        }
        resp = self.client.post(self._url("/plan"), data=data)
        self.assertEqual(resp.status_code, 302)
        self.cycle.reload()
        self.assertEqual(self.cycle.title, "Q4 new")
        self.assertEqual([g.title for g in self.cycle.goals], ["Fit", "Write"])
        self.assertEqual(self.cycle.goals[0].lag_current, 2.5)
        self.assertEqual([t.tid for t in self.cycle.tactics][0], "run")
        self.assertEqual(self.cycle.tactics[1].weeks, [2, 3])
        self.assertEqual(self.cycle.tactics[1].gid, "new2")
        self.assertEqual(self.cycle.done, ["run:1"])

    def test_sync_reads_habitify_and_scores(self):
        from application.services.crypto import encrypt_for_user
        self.user.habitify_api_key = encrypt_for_user(self.user, "SECRET")
        self.user.save()
        calls = []

        def fake(path, key, params=None):
            calls.append((path, key, params))
            return HabitifyParsingTests.STATS

        with mock.patch.object(habitify, "_fetch", side_effect=fake):
            n = habitify.sync_cycle(self.cycle, self.user, today=date(2026, 10, 14), full=True)
        self.assertEqual(n, 1)
        self.assertEqual(calls[0][1], "SECRET")
        self.assertEqual(calls[0][2], {"startDate": "2026-10-05", "endDate": "2026-10-14"})
        self.cycle.reload()
        self.assertEqual(self.cycle.habitify_counts, {"gym:1": 2, "gym:2": 1})
        self.assertEqual(self.cycle.habitify_error, "")

        def broken(path, key, params=None):
            raise habitify.HabitifyError("refused")

        with mock.patch.object(habitify, "_fetch", side_effect=broken):
            self.assertIsNone(habitify.sync_cycle(self.cycle, self.user,
                                                  today=date(2026, 10, 14)))
        self.cycle.reload()
        self.assertEqual(self.cycle.habitify_error, "refused")
        self.assertEqual(self.cycle.habitify_counts, {"gym:1": 2, "gym:2": 1})

    def test_background_round_only_reads_due_cycles_with_a_key(self):
        from application.services.crypto import encrypt_for_user
        with mock.patch.object(habitify, "_fetch") as fetch:
            self.assertEqual(habitify.sync_due(), 0)
            fetch.assert_not_called()
        self.user.habitify_api_key = encrypt_for_user(self.user, "SECRET")
        self.user.save()
        now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
        self.assertEqual(len(habitify.due_cycles(now)), 1)
        self.cycle.habitify_synced_at = now
        self.cycle.save()
        self.assertEqual(habitify.due_cycles(now), [])

    def test_settings_saves_the_key_encrypted(self):
        with mock.patch.object(habitify, "_fetch",
                               return_value={"data": [{"id": "h1", "name": "Gym"}]}):
            resp = self.client.post("/settings/habitify", data={"api_key": "SECRET"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Gym", resp.get_data(as_text=True))
        self.user.reload()
        self.assertNotEqual(self.user.habitify_api_key, "SECRET")
        self.client.post("/settings/habitify", data={"action": "remove"})
        self.user.reload()
        self.assertEqual(self.user.habitify_api_key, "")
