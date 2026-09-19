"""Which calendar entries actually make their owner unavailable.

Run with ``python -m unittest discover tests``. No database, no network: a
VEVENT goes in, ``CalendarEvent.busy`` comes out.

This exists because the booking page blocked on everything a calendar
returned, and a calendar returns a great deal that its owner is not busy
with. An all-day entry is created as "free" by default in Google and in
most CalDAV clients — a birthday, a public holiday, a "Homeoffice" marker —
and blocking on those takes whole days off the page for no reason anybody
can see: the day looks free in the calendar and gone on the page.

Being all-day is not itself the question. A whole day marked busy blocks the
whole day, and the host's panel lists it like any other appointment.
"""
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from application.services.booking import blocking_spans, busy_spans

BERLIN = ZoneInfo("Europe/Berlin")

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:{uid}
SUMMARY:{summary}
DTSTART{start}
DTEND{end}
{extra}END:VEVENT
END:VCALENDAR
"""


def parse(summary="Thing", start=";VALUE=DATE:20260923", end=";VALUE=DATE:20260924",
          extra=""):
    from icalendar import Calendar

    from application.services.calendar_sync import _vevent_to_event

    class Cal:
        color = "#458588"
        name = "Work"
        pk_ref = "abc"

    text = ICS.format(uid="u1", summary=summary, start=start, end=end, extra=extra)
    comp = next(iter(Calendar.from_ical(text).walk("VEVENT")))
    return _vevent_to_event(comp, Cal())


class TranspTests(unittest.TestCase):

    def test_a_plain_event_blocks(self):
        self.assertTrue(parse().busy)

    def test_opaque_blocks(self):
        self.assertTrue(parse(extra="TRANSP:OPAQUE\n").busy)

    def test_transparent_does_not(self):
        """"Shows me as free" is the calendar's own word on the matter."""
        self.assertFalse(parse(extra="TRANSP:TRANSPARENT\n").busy)

    def test_a_cancelled_occurrence_does_not_either(self):
        self.assertFalse(parse(extra="STATUS:CANCELLED\n").busy)

    def test_the_value_is_read_case_insensitively(self):
        self.assertFalse(parse(extra="TRANSP:transparent\n").busy)


class BlockingSpanTests(unittest.TestCase):
    """The projection the booking page consumes."""

    def events(self):
        return [parse(summary="Geburtstag", extra="TRANSP:TRANSPARENT\n"),
                parse(summary="Workshop",
                      start=":20260923T070000Z", end=":20260923T150000Z")]

    def test_busy_spans_is_the_normaliser_and_keeps_every_event(self):
        """It answers "when is this event", not "does it block" — the filter
        is the other function."""
        self.assertEqual(len(busy_spans(self.events(), BERLIN)), 2)

    def test_only_the_busy_one_takes_time_off_the_page(self):
        spans = blocking_spans(self.events(), BERLIN)
        self.assertEqual(spans, [(datetime(2026, 9, 23, 7, tzinfo=timezone.utc),
                                  datetime(2026, 9, 23, 15, tzinfo=timezone.utc))])

    def test_a_day_marked_free_leaves_the_day_bookable(self):
        from application.models.booking import BookingHour, BookingPage
        from application.services.booking import day_states

        page = BookingPage(
            enabled=True, slug="host", durations=[30], days_ahead=3,
            min_notice_hours=0, buffer_minutes=0, max_per_day=0,
            hours=[BookingHour(weekday=d, start_minute=9 * 60, end_minute=17 * 60)
                   for d in range(7)])
        free_marked = [parse(summary="Feiertag", extra="TRANSP:TRANSPARENT\n")]
        now = datetime(2026, 9, 22, 6, tzinfo=timezone.utc)
        states = day_states(page, 30, busy=blocking_spans(free_marked, BERLIN),
                            booked=[], now=now, host_tz=BERLIN)
        self.assertEqual(states["2026-09-23"], "free")


if __name__ == "__main__":
    unittest.main()
