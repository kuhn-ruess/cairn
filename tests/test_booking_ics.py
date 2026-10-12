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

from types import SimpleNamespace

from application.services.booking_ics import (
    attachment,
    build_ics,
    booking_uid,
    participants,
)

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


class CancellationTests(unittest.TestCase):
    """What makes a calendar REMOVE the entry instead of adding a second."""

    def test_it_withdraws_the_same_appointment(self):
        text = unfolded(ics(method="CANCEL", sequence=1))
        self.assertIn("METHOD:CANCEL", text)
        self.assertIn("STATUS:CANCELLED", text)
        self.assertIn("SEQUENCE:1", text)
        # The UID has to be the one the invitation carried, or the guest's
        # calendar keeps the appointment and files a cancellation beside it.
        self.assertIn("UID:cairn-booking-1@cairn", text)

    def test_an_invitation_says_none_of_that(self):
        text = unfolded(ics())
        self.assertIn("METHOD:REQUEST", text)
        self.assertNotIn("STATUS:CANCELLED", text)
        self.assertNotIn("SEQUENCE", text)


class IcsTests(unittest.TestCase):

    def test_it_is_one_event_with_a_request(self):
        out = ics()
        self.assertIn("BEGIN:VCALENDAR", out)
        self.assertIn("METHOD:REQUEST", out)
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
                           attendees=[{"name": "Bob", "email": "bob@example.com",
                                       "rsvp": True}]))
        self.assertIn("ORGANIZER;CN=Alice:mailto:a@example.com", out)
        self.assertIn("ATTENDEE;CN=Bob;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT;"
                      "RSVP=TRUE:mailto:bob@example.com", out)

    def test_nothing_is_written_for_what_was_not_given(self):
        out = ics()
        for absent in ("ORGANIZER", "ATTENDEE", "LOCATION", "URL:", "DESCRIPTION"):
            self.assertNotIn(absent, out)

    def test_a_newline_in_the_note_cannot_break_out_of_its_property(self):
        out = unfolded(ics(description="two\nlines"))
        self.assertIn("DESCRIPTION:two\\nlines", out)

    def test_umlauts_survive(self):
        self.assertIn("Müller", unfolded(ics(summary="Alice · Müller")))


def booking(**over):
    kw = dict(pk="abc123", start=START, end=END, name="Bob",
              email="bob@example.com", note="", meeting_url="")
    kw.update(over)
    return SimpleNamespace(**kw)


class OneMeetingTests(unittest.TestCase):
    """The invitation the guest gets and the event in the host's calendar are
    ONE meeting: same UID, the host as organizer, everybody as attendee. Any
    reply then lands with the host and matches the host's entry."""

    def invite(self, cancel=False, **over):
        name, mimetype, data = attachment(booking(**over), host_name="Alice",
                                          host_email="alice@example.com",
                                          cancel=cancel)
        return mimetype, unfolded(data.decode("utf-8"))

    def test_the_host_organizes_and_both_attend(self):
        _mt, text = self.invite()
        self.assertIn("ORGANIZER;CN=Alice:mailto:alice@example.com", text)
        self.assertIn("ATTENDEE;CN=Alice;PARTSTAT=ACCEPTED;ROLE=CHAIR:"
                      "mailto:alice@example.com", text)
        self.assertIn("ATTENDEE;CN=Bob;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT;"
                      "RSVP=TRUE:mailto:bob@example.com", text)
        self.assertEqual(text.count("BEGIN:VEVENT"), 1)

    def test_it_is_an_imip_request(self):
        """RFC 5546: a PUBLISH must not carry ATTENDEEs. RFC 6047: the
        method rides on the MIME type too, Outlook and Gmail read it there."""
        mimetype, text = self.invite()
        self.assertEqual(mimetype, "text/calendar; method=REQUEST")
        self.assertIn("METHOD:REQUEST", text)
        self.assertIn(f"UID:{booking_uid(booking())}", text)

    def test_the_cancellation_names_the_same_people(self):
        mimetype, text = self.invite(cancel=True)
        self.assertEqual(mimetype, "text/calendar; method=CANCEL")
        self.assertIn(f"UID:{booking_uid(booking())}", text)
        self.assertIn("ORGANIZER;CN=Alice:mailto:alice@example.com", text)
        self.assertIn("mailto:bob@example.com", text)

    def test_a_host_booking_themselves_is_one_attendee(self):
        people = participants(booking(email="Alice@example.com"),
                              host_name="Alice", host_email="alice@example.com")
        self.assertEqual([p["email"] for p in people], ["alice@example.com"])

    def test_without_a_host_address_only_the_guest_attends(self):
        people = participants(booking(), host_name="Alice", host_email="")
        self.assertEqual([p["email"] for p in people], ["bob@example.com"])


class HostCalendarCopyTests(unittest.TestCase):
    """The event PUT into the host's CalDAV calendar is the same meeting."""

    def test_it_carries_the_invitation_uid_and_the_people(self):
        from application.services.calendar_sync import _caldav_event_ics

        b = booking()
        people = participants(b, host_name="Alice", host_email="alice@example.com")
        text = unfolded(_caldav_event_ics(
            booking_uid(b), "Bob (30 min)", START, False, "", 30, None,
            organizer=("Alice", "alice@example.com"), attendees=people))
        self.assertIn(f"UID:{booking_uid(b)}", text)
        # SCHEDULE-AGENT=CLIENT (RFC 6638): Cairn mails the invitation, the
        # server must not send a second one.
        self.assertIn("ORGANIZER;CN=Alice;SCHEDULE-AGENT=CLIENT:"
                      "mailto:alice@example.com", text)
        self.assertIn("RSVP=TRUE;SCHEDULE-AGENT=CLIENT:mailto:bob@example.com",
                      text)
        self.assertNotIn("METHOD", text)   # a stored object, not a message

    def test_a_plain_task_event_has_nobody_in_it(self):
        from application.services.calendar_sync import _caldav_event_ics

        text = _caldav_event_ics("cairn-x", "Task", START, False, "", 30, None)
        self.assertNotIn("ORGANIZER", text)
        self.assertNotIn("ATTENDEE", text)


class MailPartTests(unittest.TestCase):

    def test_the_method_reaches_the_mime_part(self):
        from application.services import mail

        msg = mail._build(  # noqa: SLF001
            "cairn@example.com", "bob@example.com", "s", "b", None,
            [("appointment.ics", "text/calendar; method=REQUEST", b"X")])
        part = msg.get_payload()[-1]
        self.assertEqual(part.get_content_type(), "text/calendar")
        self.assertEqual(part.get_param("method"), "REQUEST")
        self.assertEqual(part.get_param("charset"), "utf-8")


if __name__ == "__main__":
    unittest.main()
