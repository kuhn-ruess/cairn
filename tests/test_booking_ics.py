"""The .ics that goes out with a booking confirmation.

Run with ``python -m unittest discover tests``. No database and no network:
``build_ics`` is a pure function over values, for the same reason
``free_slots`` is one — what goes wrong with a calendar invitation is a wrong
instant or a mangled line, and both are cheap to prove here and expensive to
notice in somebody's inbox.
"""
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from application.services.booking_ics import build_ics

BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
END = datetime(2026, 9, 21, 9, 30, tzinfo=timezone.utc)


def ics(**over) -> str:
    kw = dict(uid="cairn-booking-1@cairn", start=START, end=END,
              summary="Alice · Bob", stamp=START)
    kw.update(over)
    return build_ics(**kw).decode("utf-8")


def unfolded(text: str) -> str:
    """iCalendar wraps at 75 octets; join the continuations back up so a
    property can be searched for as one string."""
    return text.replace("\r\n ", "").replace("\r\n\t", "")


class IcsTests(unittest.TestCase):

    def test_it_is_one_publishable_event(self):
        out = ics()
        self.assertIn("BEGIN:VCALENDAR", out)
        self.assertIn("METHOD:PUBLISH", out)
        self.assertEqual(out.count("BEGIN:VEVENT"), 1)
        self.assertTrue(out.endswith("END:VCALENDAR\r\n"))

    def test_the_instants_go_out_as_utc(self):
        out = ics()
        self.assertIn("DTSTART:20260921T090000Z", out)
        self.assertIn("DTEND:20260921T093000Z", out)

    def test_a_local_time_is_converted_rather_than_relabelled(self):
        """11:00 Berlin is 09:00 UTC in September — the hour must not travel."""
        out = ics(start=START.astimezone(BERLIN), end=END.astimezone(BERLIN))
        self.assertIn("DTSTART:20260921T090000Z", out)
        self.assertIn("DTEND:20260921T093000Z", out)

    def test_a_naive_instant_is_taken_as_utc(self):
        """That is how mongoengine hands a booking's times back."""
        out = ics(start=START.replace(tzinfo=None), end=END.replace(tzinfo=None))
        self.assertIn("DTSTART:20260921T090000Z", out)

    def test_the_uid_is_the_one_it_was_given(self):
        """Same appointment, same UID: a second mail updates the calendar
        entry instead of sitting beside it."""
        self.assertIn("UID:cairn-booking-1@cairn", ics())

    def test_the_meeting_link_is_where_a_calendar_looks_for_it(self):
        out = unfolded(ics(location="https://teams.example/join/abc",
                           url="https://teams.example/join/abc"))
        self.assertIn("LOCATION:https://teams.example/join/abc", out)
        self.assertIn("URL:https://teams.example/join/abc", out)

    def test_both_sides_are_named(self):
        out = unfolded(ics(organizer_name="Alice", organizer_email="a@example.com",
                           attendee_name="Bob", attendee_email="bob@example.com"))
        self.assertIn("ORGANIZER;CN=Alice:MAILTO:a@example.com", out)
        self.assertIn("ATTENDEE;CN=Bob;ROLE=REQ-PARTICIPANT:MAILTO:bob@example.com",
                      out)

    def test_nothing_is_written_for_what_was_not_given(self):
        out = ics()
        for absent in ("ORGANIZER", "ATTENDEE", "LOCATION", "URL:", "DESCRIPTION"):
            self.assertNotIn(absent, out)

    def test_a_newline_in_the_note_cannot_break_out_of_its_property(self):
        out = unfolded(ics(description="two\nlines"))
        self.assertIn("DESCRIPTION:two\\nlines", out)

    def test_umlauts_survive(self):
        self.assertIn("Müller", unfolded(ics(summary="Alice · Müller")))


if __name__ == "__main__":
    unittest.main()
