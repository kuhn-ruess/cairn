"""The ``.ics`` that rides along with a booking confirmation.

Split the same way ``services/booking.py`` is: ``build_ics`` is a PURE
function over values — no database, no clock beyond the stamp it is handed —
and ``invite_for`` is the thin layer that reads one ``Booking``. What goes
wrong with calendar invitations is a wrong instant or a mangled line, and
that has to be checkable in a unit test.

``METHOD:PUBLISH``, not ``REQUEST``: the appointment is already settled — the
slot was taken, the host's calendar was written, both sides got an email. An
invitation would ask the guest to accept something that is not in question
and would put a reply nobody reads into the host's inbox. PUBLISH is what
"add this to my calendar" means.

The UID is derived from the booking's id, so the same appointment mailed
twice updates the entry that is already in the calendar instead of sitting
next to it.
"""
from __future__ import annotations

from datetime import datetime, timezone


def build_ics(*, uid: str, start: datetime, end: datetime, summary: str,
              description: str = "", location: str = "", url: str = "",
              organizer_name: str = "", organizer_email: str = "",
              attendee_name: str = "", attendee_email: str = "",
              stamp: datetime | None = None,
              method: str = "PUBLISH", sequence: int = 0) -> bytes:
    """One VEVENT in a VCALENDAR, as the bytes that go into the mail."""
    from icalendar import Calendar, Event, vCalAddress, vText

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
    if organizer_email:
        who = vCalAddress(f"MAILTO:{organizer_email}")
        if organizer_name:
            who.params["CN"] = vText(organizer_name)
        ev.add("organizer", who)
    if attendee_email:
        guest = vCalAddress(f"MAILTO:{attendee_email}")
        if attendee_name:
            guest.params["CN"] = vText(attendee_name)
        guest.params["ROLE"] = vText("REQ-PARTICIPANT")
        ev.add("attendee", guest, encode=0)
    cal.add_component(ev)
    return cal.to_ical()


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
        uid=f"cairn-booking-{booking.pk}@cairn",
        start=booking.start, end=booking.end,
        summary=f"{host_name} · {booking.name}".strip(" ·"),
        description="\n".join(body),
        location=booking.meeting_url or "",
        url=booking.meeting_url or "",
        organizer_name=host_name, organizer_email=host_email,
        attendee_name=booking.name or "", attendee_email=booking.email or "",
    )


def cancellation_for(booking, *, host_name: str, host_email: str = "") -> bytes:
    """The same appointment, withdrawn.

    Same UID as the invitation, so the guest's calendar takes this as the
    entry it already has and drops it instead of showing a second one."""
    return build_ics(
        uid=f"cairn-booking-{booking.pk}@cairn",
        start=booking.start, end=booking.end,
        summary=f"{host_name} · {booking.name}".strip(" ·"),
        organizer_name=host_name, organizer_email=host_email,
        attendee_name=booking.name or "", attendee_email=booking.email or "",
        method="CANCEL", sequence=1,
    )


def attachment(booking, *, host_name: str, host_email: str = "",
               cancel: bool = False) -> tuple:
    """``(filename, mimetype, bytes)`` — what ``mail.send_email`` wants."""
    build = cancellation_for if cancel else invite_for
    return ("appointment.ics", "text/calendar",
            build(booking, host_name=host_name, host_email=host_email))
