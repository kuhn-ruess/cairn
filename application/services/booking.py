"""Free/busy for the public booking page.

The split in this module is the point of it: ``free_slots`` is a PURE
function over intervals — no database, no network, no clock — and everything
that talks to a calendar sits in ``busy_intervals`` around it. Slot maths is
where an off-by-one costs somebody a double-booked afternoon, so it has to be
testable without a Nextcloud.

Two rules that are safety, not preference:

* **A calendar that fails to answer stops the page.** Offering times computed
  from half the busy list is worse than offering none: the visitor books, and
  the slot was never free. ``busy_intervals`` raises, and the page says it
  can't load times right now.
* **Bookings count as busy on their own.** The event goes to the write
  calendar, which need not be one of the calendars checked for busy times —
  so the ``bookings`` collection, not the calendar, is what keeps two
  visitors out of the same slot.

Everything is computed in UTC; the HOST's timezone only decides where a
weekday starts and what "09:00" means. The visitor's timezone never reaches
this module — the browser formats the UTC instants it is handed.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone

log = logging.getLogger(__name__)


class BookingError(Exception):
    """Something the visitor should be told about, in their own words."""


def free_slots(page, duration_minutes: int, *, busy, booked, now: datetime,
               host_tz) -> list[datetime]:
    """Bookable starts (UTC, ascending) for one duration.

    ``busy`` and ``booked`` are both ``[(start, end)]`` in UTC — calendar
    events and existing bookings. They differ in one way: the buffer applies
    to calendar events and to bookings alike, so they are simply concatenated;
    ``booked`` stays a separate argument because the caller gets it from a
    different place and a missing one is a very different bug.
    """
    duration = int(duration_minutes)
    if duration <= 0 or duration not in page.ordered_durations():
        return []
    step = timedelta(minutes=duration)
    buffer_ = timedelta(minutes=int(page.buffer_minutes or 0))
    blocked = [(s, e) for s, e in list(busy) + list(booked) if s and e]
    earliest = now + timedelta(hours=int(page.min_notice_hours or 0))

    today = now.astimezone(host_tz).date()
    horizon = today + timedelta(days=int(page.days_ahead or 30))
    per_day = _bookings_per_day(booked, host_tz)
    cap = int(page.max_per_day or 0)

    out: list[datetime] = []
    day = today
    while day <= horizon:
        if cap and per_day.get(day, 0) >= cap:
            day += timedelta(days=1)
            continue
        for window in page.windows_for(day.weekday()):
            start = _at(day, window.start_minute, host_tz)
            window_end = _at(day, window.end_minute, host_tz)
            while start + step <= window_end:
                end = start + step
                if start >= earliest and not _collides(start, end, blocked, buffer_):
                    out.append(start.astimezone(timezone.utc))
                start += step
        day += timedelta(days=1)
    out.sort()
    return out


def _at(day: date, minute: int, host_tz) -> datetime:
    """Wall-clock ``minute`` on ``day`` in the host's zone.

    Minutes rather than an hour/minute pair so that 24:00 — a window ending at
    midnight — lands on the next day instead of raising."""
    base = datetime.combine(day, time(0, 0), tzinfo=host_tz)
    return base + timedelta(minutes=int(minute))


def _collides(start: datetime, end: datetime, blocked, buffer_) -> bool:
    for b_start, b_end in blocked:
        if start < b_end + buffer_ and end + buffer_ > b_start:
            return True
    return False


def _bookings_per_day(booked, host_tz) -> dict:
    counts: dict = {}
    for start, _end in booked:
        day = start.astimezone(host_tz).date()
        counts[day] = counts.get(day, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# The parts that talk to the outside world.


def busy_events(user, page, since: datetime, until: datetime) -> list:
    """The events from the calendars the page is told to check.

    The one place that talks to a calendar for this page. ``busy_intervals``
    is its projection — slot maths only ever wanted the two instants, but the
    OWNER, looking at their own page, wants to know which appointment took
    the morning away.

    Raises ``BookingError`` if any calendar fails — see the module docstring:
    an incomplete busy list is how a booking page double-books its owner.
    """
    from application.services.calendar_sync import CalendarError, fetch_events

    wanted = {str(cid) for cid in (page.busy_calendar_ids or [])}
    events = []
    for cal in (user.calendars or []):
        if str(cal.pk_ref) not in wanted:
            continue
        try:
            events.extend(fetch_events(cal, since, until, user=user))
        except CalendarError as exc:
            log.warning("booking page: calendar %s unreadable: %s", cal.name, exc)
            raise BookingError(
                f"The calendar “{cal.name}” could not be read, so free times "
                "can't be shown right now.") from exc
    return events


def busy_intervals(user, page, since: datetime, until: datetime) -> list:
    """Just the ``(start, end)`` pairs — what ``free_slots`` consumes."""
    return [(e.start, e.end) for e in busy_events(user, page, since, until)]


def booked_intervals(user, since: datetime, until: datetime) -> list:
    from application.models.booking import Booking
    return [(b.start, b.end) for b in Booking.objects(
        owner=user, start__lt=until, end__gt=since).only("start", "end")]


def window_for(user, *, now=None) -> tuple:
    """The span the page answers about: now → the end of ``days_ahead``."""
    now = now or datetime.now(timezone.utc)
    return now, now + timedelta(days=int(user.booking.days_ahead or 30) + 1)


def slots_for(user, duration_minutes: int, *, now=None, busy=None):
    """The page's answer to "when are you free?" — UTC starts.

    ``busy`` is passed in by a caller that has already read the calendars
    (the owner's view wants the events themselves), so one page load stays
    one round of calendar requests."""
    from application.services.tz import user_zone

    page = user.booking
    host_tz = user_zone(user)
    now, until = window_for(user, now=now)
    if busy is None:
        busy = busy_intervals(user, page, now, until)
    booked = booked_intervals(user, now, until)
    return free_slots(page, duration_minutes, busy=busy, booked=booked,
                      now=now, host_tz=host_tz)


def confirm(user, *, start: datetime, duration_minutes: int, name: str,
            email: str, note: str, guest_timezone: str = "",
            meeting_url: str = "", now=None):
    """Take the slot if it is still free, then write the event.

    The order is deliberate. The Booking row goes in FIRST, behind a unique
    index on ``(owner, start)``, because that is the only step two
    simultaneous visitors can't both win; the calendar event follows. An event
    that fails to write leaves the slot taken and the failure recorded rather
    than handing the same time to somebody else.
    """
    from mongoengine.errors import NotUniqueError

    from application.models.booking import Booking

    page = user.booking
    duration = int(duration_minutes)
    if duration not in page.ordered_durations():
        raise BookingError("That appointment length isn't offered.")
    start = start.astimezone(timezone.utc).replace(second=0, microsecond=0)
    end = start + timedelta(minutes=duration)

    if start not in set(slots_for(user, duration, now=now)):
        raise BookingError("That time was just taken — please pick another.")

    booking = Booking(owner=user, start=start, end=end, duration_minutes=duration,
                      name=name[:120], email=email[:255], note=(note or "")[:2000],
                      guest_timezone=guest_timezone[:64])
    try:
        booking.save(force_insert=True)
    except NotUniqueError as exc:
        raise BookingError("That time was just taken — please pick another.") from exc

    # The unique index only settles an identical start. Two different starts
    # that OVERLAP (10:00–11:00 and 10:30–11:00) can both be written in the
    # same instant, so the loser of that race withdraws here.
    clash = Booking.objects(owner=user, start__lt=end, end__gt=start,
                            pk__ne=booking.pk).order_by("created_at").first()
    if clash is not None and clash.created_at <= booking.created_at:
        booking.delete()
        raise BookingError("That time was just taken — please pick another.")

    # Before the event, so the calendar entry can carry the link.
    _attach_meeting(user, page, booking, guest_link=meeting_url)
    _write_event(user, page, booking)
    return booking


def _attach_meeting(user, page, booking, guest_link: str = "") -> None:
    """Give the booking the place it happens.

    A link the GUEST brought wins over whatever the page would have done —
    somebody who pastes their own room is telling us where this meeting is,
    and a second link beside it only invites the two of you to different
    places. It also short-circuits the Teams call, so no meeting is created
    for a link nobody will open.

    Never fatal, for the same reason ``_write_event`` isn't: the slot is
    taken, the visitor has been told so, and a Teams outage must not turn a
    confirmed appointment into an error page. What failed is recorded on the
    booking, which is where the host reads it."""
    mode = page.meeting_mode or "none"
    if guest_link:
        booking.meeting_url = guest_link[:500]
        booking.meeting_provider = "guest"
    elif mode == "link":
        booking.meeting_url = (page.meeting_link or "")[:500]
        booking.meeting_provider = "link" if booking.meeting_url else ""
    elif mode == "teams":
        from application.services.msgraph import GraphError, create_meeting
        try:
            made = create_meeting(
                user, subject=f"{booking.name or 'Appointment'} · "
                              f"{user.display_name or user.username}",
                start=booking.start, end=booking.end)
        except GraphError as exc:
            log.warning("booking %s: Teams meeting failed: %s", booking.pk, exc)
            booking.meeting_error = str(exc)
        else:
            booking.meeting_url = made["join_url"][:500]
            booking.meeting_provider = "teams"
    if booking.meeting_url or booking.meeting_error:
        booking.save()


def _write_event(user, page, booking) -> None:
    """Put the appointment in the user's calendar. Never fatal to the booking:
    a visitor who has already been told "confirmed" must not see an error
    because an ics PUT timed out — the owner sees it, on the booking."""
    from application.services.calendar_sync import CalendarError
    from application.services.calendar_writeback import create_event

    cal = user.find_calendar(page.write_calendar_id) if page.write_calendar_id else None
    if cal is None:
        booking.event_error = "No calendar is configured for new appointments."
        booking.save()
        return
    body = [f"Booked through Cairn by {booking.name}".strip()]
    if booking.email:
        body.append(booking.email)
    if booking.meeting_url:
        body.append(f"Join: {booking.meeting_url}")
    if booking.guest_timezone:
        body.append(f"Their timezone: {booking.guest_timezone}")
    if booking.note:
        body.extend(["", booking.note])
    try:
        created = create_event(
            cal, user,
            title=f"{booking.name or 'Appointment'} ({booking.duration_minutes} min)",
            when=booking.start, all_day=False, description="\n".join(body),
            duration_minutes=booking.duration_minutes,
            timezone=(user.timezone or None))
        booking.calendar_id = str(cal.pk_ref)
        booking.event_id = created.get("id") or ""
    except CalendarError as exc:
        log.warning("booking %s: event write failed: %s", booking.pk, exc)
        booking.event_error = str(exc)
    booking.save()


def announce(user, booking) -> None:
    """Tell the owner (bell + mail) and the guest (mail) that it's booked.

    Best effort on purpose: the appointment exists either way, and an SMTP
    box that isn't configured is the normal case on a fresh deployment."""
    from flask import url_for

    from application.services.booking_ics import attachment
    from application.services.mail import send_email, smtp_configured
    from application.services.tz import user_zone

    when_host = booking.start.astimezone(user_zone(user))
    stamp = when_host.strftime("%a %d %b %Y, %H:%M")
    who = booking.name or booking.email or "Someone"

    user.add_notification(
        kind="booking",
        text=f"{who} booked {booking.duration_minutes} min on {stamp}",
        link=url_for("settings.booking"),
        icon="bi-calendar-check",
        actor=who,
    )
    user.save()

    if not smtp_configured():
        return
    host_name = user.display_name or user.username
    # The same invitation goes to both sides — one appointment, one UID, so
    # whichever calendar it is dropped into shows the same entry.
    invite = [attachment(booking, host_name=host_name, host_email=user.email or "")]
    where = f"Where: {booking.meeting_url}\n" if booking.meeting_url else ""
    # The host is the one who needs to know whose room this is: a link they
    # didn't set is the guest's, and it is worth a look before the day.
    theirs = where.rstrip("\n") + " (their link)\n" \
        if booking.meeting_provider == "guest" else where
    send_email(
        user.email,
        f"New booking: {who}, {stamp}",
        f"{who} booked {booking.duration_minutes} minutes with you.\n\n"
        f"When: {stamp} ({user_zone(user)})\n"
        + theirs +
        f"Email: {booking.email or '—'}\n"
        f"Note: {booking.note or '—'}\n",
        attachments=invite,
    )
    if booking.email:
        guest_stamp = stamp
        if booking.guest_timezone:
            try:
                from zoneinfo import ZoneInfo
                guest_stamp = booking.start.astimezone(
                    ZoneInfo(booking.guest_timezone)).strftime("%a %d %b %Y, %H:%M")
                guest_stamp += f" ({booking.guest_timezone})"
            except Exception:  # noqa: BLE001 — a bad tz name only costs the suffix
                pass
        send_email(
            booking.email,
            f"Your appointment with {host_name}",
            f"Your {booking.duration_minutes}-minute appointment with "
            f"{host_name} is confirmed.\n\nWhen: {guest_stamp}\n"
            + where
            + (f"\nWhat you wrote: {booking.note}\n" if booking.note else "")
            + "\nThe attached file adds it to your calendar.\n",
            attachments=invite,
        )
