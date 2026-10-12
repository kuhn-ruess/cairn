"""The ``.ics`` that rides along with a booking confirmation.

Split the same way ``services/booking.py`` is: ``build_ics`` is a PURE
function over values (no database, no clock beyond the stamp it is handed)
and ``invite_for`` is the thin layer that reads one ``Booking``. What goes
wrong with calendar invitations is a wrong instant or a mangled line, and
that has to be checkable in a unit test.

ONE appointment, ONE event, everywhere. The UID (``booking_uid``) is the same
in the guest's invitation, in the host's own calendar entry
(``services/booking._write_event`` writes it with that UID) and in the
cancellation. The host is the ORGANIZER and both sides are ATTENDEEs, so the
reply the guest's calendar sends back (Outlook and Google send one as soon as
the entry is accepted) lands in the host's mailbox and matches the event that
is already in the host's calendar, instead of an entry the host never had.

``METHOD:REQUEST`` (RFC 5546), not ``PUBLISH``: an event with an organizer
and attendees is a meeting, and a ``PUBLISH`` with ATTENDEEs is not valid
iTIP. Outlook and Google answered it with a reply anyway, for a UID the host's
calendar did not know, which is what "passt nicht zu meinem Kalender" was.
The host is ``PARTSTAT=ACCEPTED`` (it is their page), the guest
``NEEDS-ACTION`` with ``RSVP=TRUE``.
"""
from __future__ import annotations

from datetime import datetime, timezone


def build_ics(*, uid: str, start: datetime, end: datetime, summary: str,
              description: str = "", location: str = "", url: str = "",
              organizer_name: str = "", organizer_email: str = "",
              attendees=(), stamp: datetime | None = None,
              method: str = "REQUEST", sequence: int = 0) -> bytes:
    """One VEVENT in a VCALENDAR, as the bytes that go into the mail.

    ``attendees`` are dicts as ``participants`` builds them."""
    from icalendar import Calendar, Event

    cal = Calendar()
    cal.add("prodid", "-//Cairn//Booking//EN")
    cal.add("version", "2.0")
    cal.add("method", method)

    ev = Event()
    ev.add("uid", uid)
    if method == "CANCEL":
        # The three things that make a client REMOVE the entry rather than
        # add a second one: the UID it already has, a higher SEQUENCE, and
        # the status. Without the bump a calendar may keep the old version.
        ev.add("status", "CANCELLED")
    if sequence:
        ev.add("sequence", sequence)
    ev.add("summary", summary or "Appointment")
    ev.add("dtstart", _utc(start))
    ev.add("dtend", _utc(end))
    ev.add("dtstamp", _utc(stamp or datetime.now(timezone.utc)))
    if description:
        ev.add("description", description)
    if location:
        ev.add("location", location)
    if url:
        ev.add("url", url)
    add_participants(ev, organizer_name, organizer_email, attendees)
    cal.add_component(ev)
    return cal.to_ical()


def add_participants(ev, organizer_name: str, organizer_email: str,
                     attendees=(), *, schedule_agent: str = "") -> None:
    """ORGANIZER and ATTENDEE lines on one VEVENT.

    ``schedule_agent="CLIENT"`` is for the copy PUT into the host's CalDAV
    calendar (RFC 6638): it tells a scheduling server such as Nextcloud that
    Cairn sends the invitation itself, so the guest does not get a second one
    from the server."""
    from icalendar import vCalAddress, vText

    if organizer_email:
        who = vCalAddress(f"mailto:{organizer_email}")
        if organizer_name:
            who.params["CN"] = vText(organizer_name)
        if schedule_agent:
            who.params["SCHEDULE-AGENT"] = schedule_agent
        ev.add("organizer", who)
    for person in attendees:
        if not person.get("email"):
            continue
        guest = vCalAddress(f"mailto:{person['email']}")
        if person.get("name"):
            guest.params["CN"] = vText(person["name"])
        guest.params["ROLE"] = person.get("role") or "REQ-PARTICIPANT"
        guest.params["PARTSTAT"] = person.get("partstat") or "NEEDS-ACTION"
        if person.get("rsvp"):
            guest.params["RSVP"] = "TRUE"
        if schedule_agent:
            guest.params["SCHEDULE-AGENT"] = schedule_agent
        ev.add("attendee", guest, encode=0)


def booking_uid(booking) -> str:
    """The one UID an appointment has, in every calendar it reaches."""
    return f"cairn-booking-{booking.pk}@cairn"


def participants(booking, *, host_name: str, host_email: str = "") -> list:
    """Everybody in the appointment: the host first (accepted, it is their
    page), then the guest, asked to answer."""
    out = []
    if host_email:
        out.append({"name": host_name, "email": host_email,
                    "role": "CHAIR", "partstat": "ACCEPTED"})
    if booking.email and booking.email.lower() != (host_email or "").lower():
        out.append({"name": booking.name or "", "email": booking.email,
                    "role": "REQ-PARTICIPANT", "partstat": "NEEDS-ACTION",
                    "rsvp": True})
    return out


def _utc(value: datetime) -> datetime:
    """Every instant leaves here in UTC — a naive one was UTC to begin with
    (that is how the booking is stored) and saying so is cheaper than a
    VTIMEZONE block every mail client would have to agree about."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def invite_for(booking, *, host_name: str, host_email: str = "") -> bytes:
    """The attachment for one booking."""
    body = []
    if booking.meeting_url:
        body.append(f"Join: {booking.meeting_url}")
    if booking.note:
        body.append(booking.note)
    return build_ics(
        uid=booking_uid(booking),
        start=booking.start, end=booking.end,
        summary=f"{host_name} · {booking.name}".strip(" ·"),
        description="\n".join(body),
        location=booking.meeting_url or "",
        url=booking.meeting_url or "",
        organizer_name=host_name, organizer_email=host_email,
        attendees=participants(booking, host_name=host_name,
                               host_email=host_email),
    )


def cancellation_for(booking, *, host_name: str, host_email: str = "") -> bytes:
    """The same appointment, withdrawn.

    Same UID as the invitation, so the guest's calendar takes this as the
    entry it already has and drops it instead of showing a second one."""
    return build_ics(
        uid=booking_uid(booking),
        start=booking.start, end=booking.end,
        summary=f"{host_name} · {booking.name}".strip(" ·"),
        organizer_name=host_name, organizer_email=host_email,
        attendees=participants(booking, host_name=host_name,
                               host_email=host_email),
        method="CANCEL", sequence=1,
    )


def attachment(booking, *, host_name: str, host_email: str = "",
               cancel: bool = False) -> tuple:
    """``(filename, mimetype, bytes)``, what ``mail.send_email`` wants.

    The ``method`` parameter on the MIME type is what iMIP (RFC 6047) asks
    for and what makes Outlook and Gmail show the accept buttons."""
    build = cancellation_for if cancel else invite_for
    method = "CANCEL" if cancel else "REQUEST"
    return ("appointment.ics", f"text/calendar; method={method}",
            build(booking, host_name=host_name, host_email=host_email))
