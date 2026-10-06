"""Zammad time imported into a project booked by the clock gets times of day.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

What is pinned: a booking is made when the work is done, so the day's entry
ends at the last booking and starts its minutes earlier; a project booked by
duration still gets none; a day that grows gets new times with its new total;
a hand correction of the times survives a sync that changes nothing; a span
that would start the day before begins at midnight instead. An imported
span steers clear of the person's other timed entries that day, which are
never moved; a manual entry on top of another gets a hint, never a refusal.
The stopwatch's end is its start plus the minutes it logs, so a short run no
longer reads 09:43 to 09:43, and a typed start equal to the end is no booking.
"""
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover (no mongoengine installed)
    connect = None

BERLIN = ZoneInfo("Europe/Berlin")


class ClockSpanTests(unittest.TestCase):

    def test_ends_at_the_booking(self):
        from application.services.timetracking import clock_span
        end = datetime(2026, 10, 6, 11, 30, tzinfo=BERLIN)
        self.assertEqual(clock_span(end, 45), ("10:45", "11:30"))

    def test_never_starts_the_day_before(self):
        from application.services.timetracking import clock_span
        end = datetime(2026, 10, 6, 0, 15, tzinfo=BERLIN)
        self.assertEqual(clock_span(end, 45), ("00:00", "00:45"))

    def test_no_times_for_nothing_or_a_whole_day(self):
        from application.services.timetracking import clock_span
        end = datetime(2026, 10, 6, 12, 0, tzinfo=BERLIN)
        self.assertIsNone(clock_span(end, 0))
        self.assertIsNone(clock_span(end, 24 * 60))


class FreeSlotTests(unittest.TestCase):

    def test_no_neighbours_keeps_the_booking_time(self):
        from application.services.timetracking import clock_span
        end = datetime(2026, 10, 6, 11, 30, tzinfo=BERLIN)
        self.assertEqual(clock_span(end, 45, [(13 * 60, 14 * 60)]),
                         ("10:45", "11:30"))

    def test_ends_where_the_entry_in_the_way_starts(self):
        from application.services.timetracking import clock_span
        end = datetime(2026, 10, 6, 11, 30, tzinfo=BERLIN)
        # 11:00 to 12:00 booked by hand: the 45 minutes end at 11:00.
        self.assertEqual(clock_span(end, 45, [(11 * 60, 12 * 60)]),
                         ("10:15", "11:00"))

    def test_skips_back_over_a_chain_of_entries(self):
        from application.services.timetracking import clock_span
        end = datetime(2026, 10, 6, 11, 30, tzinfo=BERLIN)
        busy = [(11 * 60, 12 * 60), (10 * 60 + 30, 11 * 60)]
        self.assertEqual(clock_span(end, 45, busy), ("09:45", "10:30"))

    def test_fits_into_a_gap_between_two(self):
        from application.services.timetracking import clock_span
        end = datetime(2026, 10, 6, 11, 30, tzinfo=BERLIN)
        busy = [(11 * 60, 12 * 60), (9 * 60, 10 * 60)]
        self.assertEqual(clock_span(end, 30, busy), ("10:30", "11:00"))

    def test_no_room_before_goes_after(self):
        from application.services.timetracking import clock_span
        end = datetime(2026, 10, 6, 1, 0, tzinfo=BERLIN)
        self.assertEqual(clock_span(end, 60, [(0, 2 * 60)]),
                         ("02:00", "03:00"))

    def test_no_room_at_all_stays_put(self):
        from application.services.timetracking import clock_span
        end = datetime(2026, 10, 6, 11, 0, tzinfo=BERLIN)
        self.assertEqual(clock_span(end, 60, [(0, 24 * 60)]),
                         ("10:00", "11:00"))

    def test_touching_is_not_overlapping(self):
        from application.services.timetracking import clock_span
        end = datetime(2026, 10, 6, 11, 0, tzinfo=BERLIN)
        self.assertEqual(clock_span(end, 60, [(11 * 60, 12 * 60)]),
                         ("10:00", "11:00"))


class ParseClockTests(unittest.TestCase):

    def test_same_time_twice_is_no_booking(self):
        from application.services.timetracking import parse_clock
        self.assertIsNone(parse_clock("09:43", "09:43"))

    def test_night_work_still_crosses_midnight(self):
        from application.services.timetracking import parse_clock
        self.assertEqual(parse_clock("23:30", "00:15"), (45, "23:30", "00:15"))


@unittest.skipIf(connect is None, "mongoengine not installed")
class ZammadClockImportTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()  # forces a real round trip
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls._wipe()
        disconnect(alias="default")

    @staticmethod
    def _wipe():
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        for doc in (TimeBucket, TimeTrackingProject, Customer, Team, User):
            doc.objects.delete()
        Item.all_objects.delete()

    def setUp(self):
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeTrackingProject)
        from application.models.user import User
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._wipe()
        self.user = User(username="zclock", email="zclock@test.invalid",
                         password_hash="x", level="team_admin").save()
        team = Team(name="T", admin=self.user, admins=[self.user]).save()
        self.user.team = team
        self.user.save()
        acme = Customer(team=team, name="Acme").save()
        self.project = TimeTrackingProject(team=team, customer=acme,
                                           name="Support",
                                           time_mode="clock").save()
        folder = Item(owner=self.user, kind="project", title="Acme work",
                      time_project=self.project).save()
        self.task = Item(owner=self.user, kind="task", title="fix it",
                         parent=folder).save()

    def tearDown(self):
        self.ctx.pop()

    def _sync(self, bookings):
        from application.services import zammad_import as zi
        actor = {"user_id": str(self.user.id), "user_name": "zclock",
                 "external_user": "agent@example.invalid"}
        opts = zi.TimeOptions(zone=BERLIN)
        with mock.patch.object(zi, "_fetch_time_accounting",
                               return_value=bookings), \
                mock.patch.object(zi, "_zammad_actor", return_value=actor):
            zi._import_time_accounting(
                self.user, self.task, "https://z.invalid/api/v1", "tok",
                "42", "4711", zi.ZammadStats(), raw={}, opts=opts)

    def _entries(self):
        from application.models.timetracking import TimeBucket
        return [e for b in TimeBucket.objects for e in b.entries
                if e.status == "active"]

    @staticmethod
    def _booking(bid, at, minutes):
        return {"id": bid, "created_at": at, "time_unit": str(minutes),
                "created_by_id": 7}

    def test_day_ends_at_last_booking(self):
        # 08:10 and 09:40 UTC are 10:10 and 11:40 in Berlin (summer time).
        self._sync([self._booking(1, "2026-10-05T08:10:00Z", 20),
                    self._booking(2, "2026-10-05T09:40:00Z", 25)])
        [e] = self._entries()
        self.assertEqual(e.minutes, 45)
        self.assertEqual((e.start_time, e.end_time), ("10:55", "11:40"))

    def test_duration_project_gets_no_times(self):
        self.project.time_mode = "duration"
        self.project.save()
        self._sync([self._booking(1, "2026-10-05T09:40:00Z", 25)])
        [e] = self._entries()
        self.assertIsNone(e.start_time)
        self.assertIsNone(e.end_time)

    def test_growing_day_moves_the_times(self):
        first = self._booking(1, "2026-10-05T08:10:00Z", 20)
        self._sync([first])
        self._sync([first, self._booking(2, "2026-10-05T12:00:00Z", 10)])
        [e] = self._entries()
        self.assertEqual(e.minutes, 30)
        self.assertEqual((e.start_time, e.end_time), ("13:30", "14:00"))

    def test_hand_correction_survives_an_unchanged_sync(self):
        from application.models.timetracking import TimeBucket
        bookings = [self._booking(1, "2026-10-05T08:10:00Z", 20)]
        self._sync(bookings)
        [e] = self._entries()
        TimeBucket.objects(entries__entry_id=e.entry_id).update_one(
            set__entries__S__start_time="09:00",
            set__entries__S__end_time="09:20")
        self._sync(bookings)
        [e] = self._entries()
        self.assertEqual((e.start_time, e.end_time), ("09:00", "09:20"))

    def _manual(self, start, end, day=(2026, 10, 5)):
        from application.services.timetracking import add_entry, parse_clock
        minutes, start, end = parse_clock(start, end)
        h, m = int(start[:2]), int(start[3:])
        return add_entry(project=self.project, user=self.user, minutes=minutes,
                         spent_on=datetime(*day, h, m, tzinfo=BERLIN),
                         start_time=start, end_time=end)

    def test_import_steps_out_of_a_manual_entry(self):
        manual = self._manual("11:00", "12:00")
        # 09:40 UTC is 11:40 in Berlin: 25 minutes would read 11:15 to 11:40,
        # inside the hour booked by hand, so they end at 11:00 instead.
        self._sync([self._booking(1, "2026-10-05T09:40:00Z", 25)])
        imported = [e for e in self._entries() if e.external_id]
        [e] = imported
        self.assertEqual((e.start_time, e.end_time), ("10:35", "11:00"))
        [m] = [e for e in self._entries() if e.entry_id == manual.entry_id]
        self.assertEqual((m.start_time, m.end_time), ("11:00", "12:00"))

    def test_placed_import_stays_put_on_resync(self):
        bookings = [self._booking(1, "2026-10-05T09:40:00Z", 25)]
        self._manual("11:00", "12:00")
        self._sync(bookings)
        self._sync(bookings)
        [e] = [e for e in self._entries() if e.external_id]
        self.assertEqual((e.start_time, e.end_time), ("10:35", "11:00"))

    def test_growing_day_does_not_collide_with_itself(self):
        first = self._booking(1, "2026-10-05T09:40:00Z", 20)
        self._sync([first])
        # Grows to 30 minutes ending 11:50: its own old span is not in the way.
        self._sync([first, self._booking(2, "2026-10-05T09:50:00Z", 10)])
        [e] = self._entries()
        self.assertEqual((e.start_time, e.end_time), ("11:20", "11:50"))

    def test_other_peoples_entries_are_not_in_the_way(self):
        from application.models.user import User
        from application.services.timetracking import add_entry
        other = User(username="zother", email="zother@test.invalid",
                     password_hash="x", team=self.project.team).save()
        add_entry(project=self.project, user=other, minutes=60,
                  spent_on=datetime(2026, 10, 5, 11, 0, tzinfo=BERLIN),
                  start_time="11:00", end_time="12:00")
        self._sync([self._booking(1, "2026-10-05T09:40:00Z", 25)])
        [e] = [e for e in self._entries() if e.external_id]
        self.assertEqual((e.start_time, e.end_time), ("11:15", "11:40"))

    def test_manual_overlap_gets_a_hint(self):
        from application.services.timetracking import overlap_warning
        self._manual("11:00", "12:00")
        clash = self._manual("11:30", "12:30")
        self.assertIn("11:00 to 12:00", overlap_warning(clash))
        apart = self._manual("13:00", "14:00")
        self.assertEqual(overlap_warning(apart), "")
        elsewhere = self._manual("11:30", "12:30", day=(2026, 10, 6))
        self.assertEqual(overlap_warning(elsewhere), "")

    def test_short_stopwatch_run_has_a_span(self):
        from datetime import timedelta, timezone
        from application.models.timer import RunningTimer
        from application.services.timetracking import stop_timer
        self.user.running_timer = RunningTimer(
            project_id=str(self.project.id), team_id=str(self.project.team.id),
            started_at=datetime.now(timezone.utc) - timedelta(seconds=20))
        self.user.save()
        entry = stop_timer(self.user)
        self.assertEqual(entry.minutes, 1)
        self.assertNotEqual(entry.start_time, entry.end_time)
        start = datetime.strptime(entry.start_time, "%H:%M")
        end = datetime.strptime(entry.end_time, "%H:%M")
        self.assertEqual((end - start) % timedelta(days=1), timedelta(minutes=1))


if __name__ == "__main__":
    unittest.main()
