"""services/calendar_sync.py — which events belong to a day.

The Today page fetches a WIDER window than it shows (an all-day event encodes
its date as midnight UTC, so it can sit hours outside its own local day) and
cuts it back with ``events_in_window``. When that second step is missing the
slack leaks and yesterday's appointments appear under Today, which is the
regression these cases pin down.
"""
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from application.services.calendar_sync import (
    CalendarEvent, FETCH_SLACK, event_day, events_in_window,
)

BERLIN = ZoneInfo("Europe/Berlin")        # UTC+2 in summer
LA = ZoneInfo("America/Los_Angeles")      # UTC-7 in summer


def timed(start_iso, minutes=60):
    start = datetime.fromisoformat(start_iso)
    return CalendarEvent(uid="t", title="timed", start=start,
                         end=start + timedelta(minutes=minutes))


def all_day(first, last=None):
    """All-day event as every reader encodes it: midnight UTC, DTEND exclusive."""
    start = datetime.fromisoformat(first + "T00:00:00+00:00")
    end = datetime.fromisoformat((last or first) + "T00:00:00+00:00") + timedelta(days=1)
    return CalendarEvent(uid="a", title="all-day", start=start, end=end, all_day=True)


def day_window(d, tz, days=1):
    start = datetime.fromisoformat(d + "T00:00:00").replace(tzinfo=tz)
    return start, start + timedelta(days=days)


class TimedEventTest(unittest.TestCase):
    def test_yesterday_evening_is_not_today(self):
        since, until = day_window("2026-08-24", BERLIN)
        # 20:00 Berlin yesterday — inside the widened FETCH window, and the bug.
        ev = timed("2026-08-23T20:00:00+02:00")
        self.assertGreater(ev.start, since - FETCH_SLACK)
        self.assertEqual(events_in_window([ev], since, until), [])

    def test_tomorrow_morning_is_not_today(self):
        since, until = day_window("2026-08-24", BERLIN)
        self.assertEqual(
            events_in_window([timed("2026-08-25T07:00:00+02:00")], since, until), [])

    def test_own_day_survives_at_both_edges(self):
        since, until = day_window("2026-08-24", BERLIN)
        first = timed("2026-08-24T00:05:00+02:00")
        last = timed("2026-08-24T23:30:00+02:00")
        self.assertEqual(events_in_window([first, last], since, until), [first, last])

    def test_late_event_belongs_to_the_day_it_starts_on(self):
        # 00:30 Berlin is 22:30 UTC the day before — the UTC date must not decide.
        since, until = day_window("2026-08-24", BERLIN)
        ev = timed("2026-08-24T00:30:00+02:00")
        self.assertEqual(events_in_window([ev], since, until), [ev])
        self.assertEqual(event_day(ev, BERLIN), ev.start.astimezone(BERLIN).date())


class AllDayEventTest(unittest.TestCase):
    def test_todays_all_day_survives_east_and_west_of_utc(self):
        ev = all_day("2026-08-24")
        for tz in (BERLIN, LA):
            since, until = day_window("2026-08-24", tz)
            self.assertGreaterEqual(ev.start, since - FETCH_SLACK, tz.key)
            self.assertLess(ev.start, until + FETCH_SLACK, tz.key)
            self.assertEqual(events_in_window([ev], since, until), [ev], tz.key)

    def test_neighbouring_all_day_is_excluded(self):
        since, until = day_window("2026-08-24", BERLIN)
        self.assertEqual(
            events_in_window([all_day("2026-08-23"), all_day("2026-08-25")],
                             since, until), [])

    def test_multi_day_all_day_shows_on_every_day_it_covers(self):
        ev = all_day("2026-08-23", "2026-08-25")
        for d in ("2026-08-23", "2026-08-24", "2026-08-25"):
            since, until = day_window(d, BERLIN)
            self.assertEqual(events_in_window([ev], since, until), [ev], d)
        since, until = day_window("2026-08-26", BERLIN)
        self.assertEqual(events_in_window([ev], since, until), [])

    def test_day_is_read_off_the_date_not_the_zone(self):
        # Midnight UTC is 17:00 the previous day in LA — converting would
        # file the event one day early.
        self.assertEqual(event_day(all_day("2026-08-24"), LA),
                         datetime(2026, 8, 24, tzinfo=timezone.utc).date())


class MultiDayWindowTest(unittest.TestCase):
    def test_window_covers_exactly_its_days(self):
        since, until = day_window("2026-08-24", BERLIN, days=3)
        keep = [timed("2026-08-24T09:00:00+02:00"),
                all_day("2026-08-26"),
                timed("2026-08-26T23:00:00+02:00")]
        drop = [timed("2026-08-23T23:00:00+02:00"),
                all_day("2026-08-27"),
                timed("2026-08-27T00:30:00+02:00")]
        self.assertEqual(events_in_window(keep + drop, since, until), keep)


if __name__ == "__main__":
    unittest.main()
