"""Zammad time imported into a project booked by the clock gets times of day.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

What is pinned: a booking is made when the work is done, so the day's entry
ends at the last booking and starts its minutes earlier; a project booked by
duration still gets none; a day that grows gets new times with its new total;
a hand correction of the times survives a sync that changes nothing; a span
that would start the day before begins at midnight instead.
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


if __name__ == "__main__":
    unittest.main()
