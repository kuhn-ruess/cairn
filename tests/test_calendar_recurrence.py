"""services/calendar_sync.py — a recurring appointment is many appointments.

A calendar hands over a series as ONE component carrying an RRULE: neither a
CalDAV calendar-query nor a subscribed .ics expands it. Read literally, a
weekly jour fixe is a single event on the day the series began — which every
window check then drops, so the appointment is invisible for as long as it
recurs, in the past as well as the future. These cases pin the expansion down,
including the two things that make an occurrence differ from its rule: a
cancelled one (EXDATE) and a moved one (RECURRENCE-ID).
"""
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from icalendar import Calendar as ICalendar

from application.services.calendar_sync import expand_vevents


class _Cal:
    """The handful of attributes the reader copies off a calendar."""
    color = "#458588"
    name = "Nextcloud"
    pk_ref = ""


def parse(*vevents: str):
    body = "".join(vevents)
    return ICalendar.from_ical(
        f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n{body}END:VCALENDAR\r\n"
    ).walk("VEVENT")


def expand(*vevents, days=7, start="2026-08-24"):
    since = datetime.fromisoformat(f"{start}T00:00:00+00:00")
    return expand_vevents(parse(*vevents), _Cal(), since, since + timedelta(days=days))


WEEKLY = (
    "BEGIN:VEVENT\r\nUID:weekly\r\nSUMMARY:Jour fixe\r\n"
    "DTSTART;TZID=Europe/Berlin:20250825T140000\r\n"
    "DTEND;TZID=Europe/Berlin:20250825T150000\r\n"
    "RRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\n"
)


class ExpandVeventsTest(unittest.TestCase):
    def test_a_year_old_weekly_series_reaches_this_week(self):
        events = expand(WEEKLY)
        self.assertEqual(1, len(events))
        occ = events[0]
        self.assertEqual("2026-08-24", occ.start.astimezone(timezone.utc).date().isoformat())
        self.assertEqual(timedelta(hours=1), occ.end - occ.start)
        self.assertFalse(occ.all_day)

    def test_every_occurrence_in_the_window_comes_through(self):
        events = expand(WEEKLY, days=28)
        self.assertEqual(4, len(events))
        self.assertEqual(4, len({e.start for e in events}))

    def test_each_occurrence_has_its_own_id(self):
        """Notes and links hang off the uid — two occurrences of one series
        must not be the same event."""
        events = expand(WEEKLY, days=28)
        self.assertEqual(4, len({e.uid for e in events}))
        self.assertTrue(all(e.uid.startswith("weekly#") for e in events))

    def test_a_single_event_is_left_exactly_as_it_was(self):
        single = (
            "BEGIN:VEVENT\r\nUID:one\r\nSUMMARY:Kickoff\r\n"
            "DTSTART;TZID=Europe/Berlin:20260824T100000\r\n"
            "DTEND;TZID=Europe/Berlin:20260824T110000\r\nEND:VEVENT\r\n"
        )
        events = expand(single)
        self.assertEqual(["one"], [e.uid for e in events])

    def test_exdate_cancels_that_occurrence(self):
        cancelled = WEEKLY.replace(
            "RRULE:FREQ=WEEKLY\r\n",
            "RRULE:FREQ=WEEKLY\r\nEXDATE;TZID=Europe/Berlin:20260824T140000\r\n")
        self.assertEqual([], expand(cancelled))

    def test_a_moved_occurrence_replaces_the_one_the_rule_would_generate(self):
        moved = (
            "BEGIN:VEVENT\r\nUID:weekly\r\nSUMMARY:Jour fixe (moved)\r\n"
            "RECURRENCE-ID;TZID=Europe/Berlin:20260824T140000\r\n"
            "DTSTART;TZID=Europe/Berlin:20260824T160000\r\n"
            "DTEND;TZID=Europe/Berlin:20260824T170000\r\nEND:VEVENT\r\n"
        )
        events = expand(WEEKLY, moved)
        self.assertEqual(1, len(events), "the moved one must not show twice")
        self.assertEqual("Jour fixe (moved)", events[0].title)
        berlin = ZoneInfo("Europe/Berlin")
        self.assertEqual(16, events[0].start.astimezone(berlin).hour)

    def test_yearly_all_day_stays_all_day(self):
        birthday = (
            "BEGIN:VEVENT\r\nUID:bday\r\nSUMMARY:Birthday\r\n"
            "DTSTART;VALUE=DATE:20200826\r\nRRULE:FREQ=YEARLY\r\nEND:VEVENT\r\n"
        )
        events = expand(birthday)
        self.assertEqual(1, len(events))
        self.assertTrue(events[0].all_day)
        self.assertEqual("2026-08-26", events[0].start.date().isoformat())

    def test_rdate_adds_an_occurrence_without_a_rule(self):
        extra = (
            "BEGIN:VEVENT\r\nUID:extra\r\nSUMMARY:Extra date\r\n"
            "DTSTART;TZID=Europe/Berlin:20260820T090000\r\n"
            "DTEND;TZID=Europe/Berlin:20260820T093000\r\n"
            "RDATE;TZID=Europe/Berlin:20260826T090000\r\nEND:VEVENT\r\n"
        )
        events = expand(extra)
        self.assertEqual(["2026-08-26"],
                         [e.start.astimezone(timezone.utc).date().isoformat() for e in events])

    def test_the_wall_clock_survives_a_daylight_saving_change(self):
        """"Every Tuesday at 14:00" is a wall-clock rule. Expanded in UTC it
        drifts by an hour the moment the window crosses the switch — and an
        appointment at the wrong time is worse than one that is missing."""
        berlin = ZoneInfo("Europe/Berlin")
        weekly_14h = (
            "BEGIN:VEVENT\r\nUID:w\r\nSUMMARY:Weekly 14:00\r\n"
            "DTSTART;TZID=Europe/Berlin:20260901T140000\r\n"
            "DTEND;TZID=Europe/Berlin:20260901T150000\r\n"
            "RRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\n"
        )
        # 2026-10-25 is the end of summer time in Europe/Berlin.
        events = expand(weekly_14h, start="2026-10-20", days=21)
        self.assertEqual(3, len(events))
        for occ in events:
            self.assertEqual(14, occ.start.astimezone(berlin).hour,
                             f"{occ.start.isoformat()} is not 14:00 in Berlin")
            self.assertEqual(15, occ.end.astimezone(berlin).hour)

    def test_an_unreadable_rule_keeps_the_appointment_itself(self):
        broken = WEEKLY.replace("RRULE:FREQ=WEEKLY", "RRULE:FREQ=NONSENSE")
        events = expand(broken, start="2025-08-25", days=1)
        self.assertEqual(["weekly"], [e.uid for e in events])


if __name__ == "__main__":
    unittest.main()
