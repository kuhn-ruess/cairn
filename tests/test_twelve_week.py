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
        # Two Habitify days of three is not done, but two thirds executed.
        self.assertEqual(tw.week_score(c, 2), {"week": 2, "planned": 3, "done": 0, "score": 22})
        # Week 3: run and gym planned, nothing done.
        self.assertEqual(tw.week_score(c, 3)["planned"], 2)

    def test_a_habit_partly_done_counts_its_share(self):
        c = _cycle()
        c.tactics = [TwyTactic(tid="read", gid="g1", title="Read", recurring=True,
                               habitify_id="h-read", per_week=4)]
        c.habitify_counts = {"read:1": 1}
        self.assertEqual(tw.week_score(c, 1), {"week": 1, "planned": 1, "done": 0, "score": 25})
        self.assertEqual(tw.goal_week_score(c, "g1", 1)["score"], 25)
        self.assertEqual(tw.goal_average(c, "g1", date(2026, 10, 14)), 25)
        # "+1" logs count the same way; more than planned is still 100 %.
        c.tactics.append(TwyTactic(tid="walk", gid="g1", title="Walk", recurring=True,
                                   per_week=2, log=["2026-10-06"]))
        self.assertEqual(tw.week_score(c, 1)["score"], 38)  # (1/4 + 1/2) / 2
        c.habitify_counts = {"read:1": 6}
        self.assertAlmostEqual(tw.credit(c, c.tactic("read"), 1), 1.0)
        # A manual tick is the whole tactic.
        c.done = ["walk:1"]
        self.assertEqual(tw.week_score(c, 1), {"week": 1, "planned": 2, "done": 2, "score": 100})

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

    def test_a_habit_serving_two_goals_counts_once_in_the_week(self):
        c = _cycle()
        c.goals.append(TwyGoal(gid="g2", title="Calm"))
        run = c.tactic("run")
        run.gids, run.goals_listed = ["g1", "g2"], True
        c.done = ["run:1"]
        self.assertEqual(tw.week_score(c, 1)["planned"], 3)
        self.assertEqual(tw.week_score(c, 1)["done"], 1)
        self.assertEqual(tw.goal_week_score(c, "g2", 1),
                         {"week": 1, "planned": 1, "done": 1, "score": 100})
        self.assertEqual(tw.goal_week_score(c, "g1", 1)["planned"], 3)
        self.assertEqual(tw.goal_average(c, "g2", date(2026, 10, 14)), 100)

    def test_old_single_goal_link_is_read_and_backfilled(self):
        c = _cycle()
        t = c.tactic("plan")
        self.assertEqual(tw.goal_ids(t), ["g1"])
        self.assertTrue(tw.backfill_goal_ids(c))
        self.assertEqual((t.gids, t.gid, t.goals_listed), (["g1"], "g1", True))
        self.assertFalse(tw.backfill_goal_ids(c))
        t.gids = []                     # unpicked later: no goal, gid not revived
        self.assertEqual(tw.goal_ids(t), [])

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

    def test_weekly_goal_counts_the_days_logged(self):
        stats = {"data": {"periodicity": "weekly", "dailyProgress": [
            {"date": "2026-10-05", "totalLog": 1, "status": "inprogress"},
            {"date": "2026-10-06", "totalLog": 0, "status": "inprogress"},
            {"date": "2026-10-08", "totalLog": 1, "status": "completed"},
            {"date": "2026-10-09", "totalLog": 0, "status": "completed"},
            {"date": "2026-10-10", "totalLog": 1, "status": "skipped"},
        ]}}
        self.assertEqual(habitify.parse_completed_days(stats),
                         {date(2026, 10, 5), date(2026, 10, 8)})

    def test_weekly_rep_goal_counts_every_rep_logged(self):
        # "3 times per week": one day with 2 reps and one with 1 is the
        # whole week in Habitify, so it must be 3 here, not 2 days.
        stats = {"data": {"periodicity": "weekly",
                          "unit": {"id": "rep", "name": "rep", "symbol": "rep"},
                          "dailyProgress": [
            {"date": "2026-10-05", "totalLog": 2, "status": "inprogress"},
            {"date": "2026-10-07", "totalLog": 1, "status": "completed"},
            {"date": "2026-10-08", "totalLog": 0, "status": "completed"},
        ]}}
        counts = habitify.parse_daily_counts(stats)
        self.assertEqual(counts, {date(2026, 10, 5): 2, date(2026, 10, 7): 1})
        self.assertEqual(habitify.count_by_week(_cycle(), "gym", counts), {"gym:1": 3})

    def test_weekly_goal_in_another_unit_counts_days(self):
        stats = {"data": {"periodicity": "weekly", "unit": {"symbol": "min"},
                          "dailyProgress": [
            {"date": "2026-10-05", "totalLog": 45, "status": "inprogress"},
        ]}}
        self.assertEqual(habitify.parse_daily_counts(stats), {date(2026, 10, 5): 1})

    def test_frequency(self):
        self.assertEqual(habitify.frequency(7), {
            "occurrence": {"type": "daily"},
            "goal": {"periodicity": "daily", "value": 1, "unit": "rep"}})
        for n in (1, 3, 6):
            self.assertEqual(habitify.frequency(n), {
                "occurrence": {"type": "daily"},
                "goal": {"periodicity": "weekly", "value": n, "unit": "rep"}})
        self.assertEqual(habitify.frequency(0)["goal"]["value"], 1)

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
                        self._url("/plan"), "/habits/12-week-year/new",
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

    def _plan_data(self, **over):
        data = {
            "title": "Q4 new", "vision": "v", "start": "2026-10-05",
            "goal_gid": ["g1", "new2", "new3"], "goal_title": ["Fit", "Write", ""],
            "goal_lag_target": ["6", "", ""], "goal_lag_current": ["2,5", "", ""],
            "goal_lag_unit": ["kg", "", ""],
            "row": ["0", "7", "9"],
            "t-0-tid": "run", "t-0-title": "Run", "t-0-goal": "g1",
            "t-0-week": [str(w) for w in range(1, 13)], "t-0-per_week": "1",
            "t-7-tid": "", "t-7-title": "Draft", "t-7-goal": ["new2"],
            "t-7-week": ["2", "3"], "t-7-per_week": "1",
            "t-9-title": "", "t-9-per_week": "1",
        }
        data.update(over)
        return data

    def test_plan_save_keeps_ids_and_drops_removed_tactics(self):
        self.cycle.done = ["plan:1", "run:1"]
        self.cycle.save()
        resp = self.client.post(self._url("/plan"), data=self._plan_data())
        self.assertEqual(resp.status_code, 302)
        self.cycle.reload()
        self.assertEqual(self.cycle.title, "Q4 new")
        self.assertEqual([g.title for g in self.cycle.goals], ["Fit", "Write"])
        self.assertEqual(self.cycle.goals[0].lag_current, 2.5)
        self.assertEqual([t.tid for t in self.cycle.tactics][0], "run")
        self.assertTrue(self.cycle.tactics[0].recurring)
        self.assertEqual(self.cycle.tactics[1].weeks, [2, 3])
        self.assertFalse(self.cycle.tactics[1].recurring)
        self.assertEqual(self.cycle.tactics[1].gids, ["new2"])
        self.assertEqual(self.cycle.tactics[0].gid, "g1")       # old field untouched
        self.assertEqual(len(self.cycle.tactics), 2)
        self.assertEqual(self.cycle.done, ["run:1"])

    def test_a_mistake_keeps_everything_typed(self):
        data = self._plan_data(**{"t-7-week": [], "goal_lag_target": ["six", "", ""],
                                  "vision": "my long vision"})
        resp = self.client.post(self._url("/plan"), data=data)
        self.assertEqual(resp.status_code, 422)
        html = resp.get_data(as_text=True)
        for kept in ("Q4 new", "my long vision", "Draft", 'value="six"', 'value="2,5"', "Write"):
            self.assertIn(kept, html)
        self.assertIn("Click the week(s) it is planned for.", html)
        self.assertIn("A number, please.", html)
        # The weeks picked on the other rows are still picked.
        self.assertIn('name="t-0-week" value="12" id="t-0-w12" autocomplete="off" checked', html)
        self.cycle.reload()
        self.assertEqual(self.cycle.title, "Q4")
        self.assertEqual([t.tid for t in self.cycle.tactics], ["run", "plan", "gym"])

    def test_new_cycle_mistake_keeps_values(self):
        resp = self.client.post("/habits/12-week-year/new",
                                data={"title": "", "vision": "keep me", "start": "2026-10-12"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("keep me", html)
        self.assertIn('value="2026-10-12"', html)
        self.assertIn("Give the cycle a name.", html)

    def test_lag_mistake_keeps_value(self):
        resp = self.client.post(self._url("/lag"), data={"gid": "g1", "lag_current": "abc"})
        self.assertEqual(resp.status_code, 422)
        self.assertIn('value="abc"', resp.get_data(as_text=True))
        self.cycle.reload()
        self.assertEqual(self.cycle.goals[0].lag_current, 1.5)

    def test_habit_counter(self):
        self.cycle.tactics.append(TwyTactic(tid="read", gid="", title="Read", recurring=True,
                                            weeks=list(range(1, 13)), per_week=2))
        self.cycle.save()
        with mock.patch("application.views.twelve_week._today", return_value=date(2026, 10, 6)):
            for _ in range(2):
                self.client.post(self._url("/log"), data={"tid": "read", "week": "1"})
            self.client.post(self._url("/log"), data={"tid": "read", "week": "2"})
            self.client.post(self._url("/log"), data={"tid": "read", "week": "2", "undo": "1"})
        self.cycle.reload()
        t = self.cycle.tactic("read")
        self.assertEqual(t.log, ["2026-10-06", "2026-10-06"])
        self.assertTrue(tw.is_done(self.cycle, t, 1))
        self.assertFalse(tw.is_done(self.cycle, t, 2))

    def test_picker_lists_habitify_habits_and_writes_only_after_confirming(self):
        from application.services.crypto import encrypt_for_user
        self.user.habitify_api_key = encrypt_for_user(self.user, "SECRET")
        self.user.save()
        listing = {"data": [{"id": "h-gym", "name": "Gym"}, {"id": "h-es", "name": "Spanisch"}]}
        with mock.patch.object(habitify, "_fetch", return_value=listing), \
                mock.patch.object(habitify, "_send") as send:
            html = self.client.get(self._url("/plan")).get_data(as_text=True)
            self.assertIn('<option value="h-es"', html)
            self.assertIn("Create it in Habitify", html)
            data = self._plan_data(**{"t-7-habit": "__new__", "t-7-week": ["2", "3", "5"],
                                      "t-7-per_week": "3"})
            resp = self.client.post(self._url("/plan"), data=data)
            self.assertEqual(resp.status_code, 302)
            self.assertTrue(resp.headers["Location"].endswith("/habitify"))
            send.assert_not_called()
            preview = self.client.get(self._url("/habitify")).get_data(as_text=True)
            self.assertIn("2026-10-12", preview)      # week 2 starts
            self.assertIn("2026-11-08", preview)      # week 5 ends
            self.assertIn("week 4 stays active", preview)
            self.assertIn("3&times; per week", preview)
            send.return_value = {"id": "h-new", "name": "Draft"}
            resp = self.client.post(self._url("/habitify"))
            self.assertEqual(resp.status_code, 302)
        method, path, key, body = send.call_args[0]
        self.assertEqual((method, path, key), ("POST", "/habits", "SECRET"))
        self.assertEqual(body, {"name": "Draft", "type": "good", "startDate": "2026-10-12",
                                "occurrence": {"type": "daily"},
                                "goal": {"periodicity": "weekly", "value": 3, "unit": "rep"},
                                "endCondition": {"type": "date", "date": "2026-11-08"}})
        self.cycle.reload()
        t = [t for t in self.cycle.tactics if t.title == "Draft"][0]
        self.assertEqual((t.habitify_id, t.habitify_start, t.habitify_end, t.habitify_per_week),
                         ("h-new", "2026-10-12", "2026-11-08", 3))
        self.assertEqual(habitify.pending_changes(self.cycle), [])
        # Saving the plan again with the same habit keeps what was written.
        data = self._plan_data(**{"t-7-tid": t.tid, "t-7-habit": "h-new",
                                  "t-7-manage": "1", "t-7-week": ["2", "3", "5"],
                                  "t-7-per_week": "3"})
        with mock.patch.object(habitify, "_fetch", return_value=listing):
            self.client.post(self._url("/plan"), data=data)
        self.cycle.reload()
        self.assertEqual(self.cycle.tactic(t.tid).habitify_per_week, 3)
        self.assertEqual(habitify.pending_changes(self.cycle), [])

    def test_a_habit_created_daily_before_gets_its_frequency(self):
        # Created by an older Cairn: the dates are right, the frequency was
        # never sent, so Habitify runs it every day.
        t = self.cycle.tactic("gym")
        t.per_week = 2
        t.habitify_manage = True
        t.habitify_start, t.habitify_end = tw.habitify_window(t, self.cycle)
        changes = habitify.pending_changes(self.cycle)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["action"], "update")
        self.assertEqual(changes[0]["body"]["goal"],
                         {"periodicity": "weekly", "value": 2, "unit": "rep"})
        self.assertEqual(changes[0]["body"]["startDate"], t.habitify_start)
        from application.services.crypto import encrypt_for_user
        self.user.habitify_api_key = encrypt_for_user(self.user, "SECRET")
        with mock.patch.object(habitify, "_send", return_value=None) as send:
            done, errs = habitify.apply_changes(self.cycle, self.user, changes)
        self.assertEqual((done, errs), (1, []))
        self.assertEqual(send.call_args[0][:2], ("PUT", "/habits/h-gym"))
        self.assertEqual(t.habitify_per_week, 2)
        self.assertEqual(habitify.pending_changes(self.cycle), [])
        # A habit only mapped, not managed by Cairn, is never touched.
        t.habitify_manage, t.per_week = False, 5
        self.assertEqual(habitify.pending_changes(self.cycle), [])

    def test_moving_weeks_updates_a_managed_habit(self):
        t = self.cycle.tactic("gym")
        t.habitify_manage, t.habitify_start, t.habitify_end = True, "2026-10-05", "2026-12-27"
        t.recurring, t.weeks = False, [1, 2, 3]
        changes = habitify.pending_changes(self.cycle)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["action"], "update")
        self.assertEqual(changes[0]["body"]["endCondition"]["date"], "2026-10-25")
        from application.services.crypto import encrypt_for_user
        self.user.habitify_api_key = encrypt_for_user(self.user, "SECRET")
        with mock.patch.object(habitify, "_send",
                               side_effect=habitify.HabitifyError("nope")) as send:
            done, errs = habitify.apply_changes(self.cycle, self.user, changes)
        self.assertEqual((done, errs), (0, ["Gym: nope"]))
        self.assertEqual(send.call_args[0][:2], ("PUT", "/habits/h-gym"))
        self.assertEqual(len(habitify.pending_changes(self.cycle)), 1)

    def test_plan_links_a_habit_to_two_goals(self):
        data = self._plan_data(**{"t-0-goal": ["g1", "new2"]})
        self.assertEqual(self.client.post(self._url("/plan"), data=data).status_code, 302)
        self.cycle.reload()
        self.assertEqual(self.cycle.tactic("run").gids, ["g1", "new2"])
        html = self.client.get(self._url("?week=1")).get_data(as_text=True)
        self.assertEqual(html.count('data-tid="run"'), 2)     # under both goals
        self.assertIn('id="twy-g1-run"', html)
        self.assertIn('id="twy-new2-run"', html)
        # Ticked once, done under both; the week counts it once.
        self.client.post(self._url("/toggle"), data={"tid": "run", "week": "1"})
        self.cycle.reload()
        self.assertEqual(tw.week_score(self.cycle, 1), {"week": 1, "planned": 1, "done": 1, "score": 100})

    def test_unnamed_goal_pick_keeps_the_form(self):
        data = self._plan_data(**{"t-0-goal": ["g1", "new3"]})
        resp = self.client.post(self._url("/plan"), data=data)
        self.assertEqual(resp.status_code, 422)
        html = resp.get_data(as_text=True)
        self.assertIn("A picked goal has no name", html)
        self.assertIn('value="new3" id="t-0-g2" autocomplete="off"\n                   checked', html)

    def test_creating_in_habitify_needs_a_key(self):
        resp = self.client.post(self._url("/plan"), data=self._plan_data(**{"t-7-habit": "__new__"}))
        self.assertEqual(resp.status_code, 422)
        self.assertIn("Connect Habitify first", resp.get_data(as_text=True))

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
