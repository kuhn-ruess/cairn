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


def busy_spans(events, host_tz) -> list:
    """``(start, end)`` per event — with an ALL-DAY one expanded to the host's
    own civil day.

    A whole-day VEVENT carries a DATE, and the calendar layer turns a date
    into midnight **UTC** (``calendar_sync._normalise_ical_dt``). Two hours
    east of UTC that is 02:00, so a day marked "Urlaub" used to block from
    02:00 to 02:00: it left the first two hours of the NEXT day blocked and,
    west of UTC, blocked the evening before instead of the day itself. A day
    off is a day in the host's calendar, so that is what it becomes here —
    and because the page is handed these same spans, what it lists is exactly
    what the slot maths used.
    """
    out = []
    for ev in events:
        start, end = ev.start, ev.end
        if getattr(ev, "all_day", False):
            first = start.astimezone(timezone.utc).date()
            last = first
            if end > start:
                last = (end.astimezone(timezone.utc) - timedelta(seconds=1)).date()
            if last < first:
                last = first
            start = _at(first, 0, host_tz)
            end = _at(last + timedelta(days=1), 0, host_tz)
        out.append((start, end))
    return out


def blocking_spans(events, host_tz) -> list:
    """``busy_spans`` minus the events that block nothing.

    An event marked free in the calendar — ``TRANSP:TRANSPARENT``, Google's
    ``transparency: transparent``, which is what an all-day birthday or a
    "Homeoffice" marker is by default — must not take a day off the booking
    page. It is not shown to the host either: the panel answers "what took
    this day", and something that took nothing is a line to read past.

    Whether an entry is all-day has no say in it. A whole day marked busy
    blocks the whole day and is listed like any other appointment."""
    return [span for ev, span in zip(events, busy_spans(events, host_tz))
            if getattr(ev, "busy", True)]


def day_states(page, duration_minutes: int, *, busy, booked, now: datetime,
               host_tz) -> dict:
    """Why each day offers what it offers — ``{"YYYY-MM-DD": state}`` in the
    HOST's zone, for the host's own view of their page.

    Pure, like ``free_slots``, and deliberately a SECOND pass rather than a
    second return value from it: the slot maths is the one function that must
    stay easy to read, and this answers a different question — not "when am I
    free" but "what happened to Wednesday".

    The states, in the order they are decided:

    * ``closed`` — no bookable window on that weekday at all
    * ``cap``    — ``max_per_day`` already reached by existing bookings
    * ``notice`` — the whole day falls inside ``min_notice_hours``
    * ``short``  — time is left, but no run of it long enough for this length
      (a window shorter than the appointment lands here too)
    * ``full``   — covered end to end by events, bookings and their buffers
    * ``free``   — something is offered

    The distinction that matters is ``short`` against ``full``: a day with two
    meetings and a 30-minute buffer around each looks half empty in a list of
    appointments and is genuinely unbookable, and "nothing free" without that
    sentence reads like a bug.
    """
    duration = int(duration_minutes)
    step = timedelta(minutes=duration)
    buffer_ = timedelta(minutes=int(page.buffer_minutes or 0))
    blocked = [(s, e) for s, e in list(busy) + list(booked) if s and e]
    earliest = now + timedelta(hours=int(page.min_notice_hours or 0))
    per_day = _bookings_per_day(booked, host_tz)
    cap = int(page.max_per_day or 0)

    today = now.astimezone(host_tz).date()
    horizon = today + timedelta(days=int(page.days_ahead or 30))
    states = {}
    day = today
    while day <= horizon:
        states[day.isoformat()] = _day_state(
            page, day, step, blocked, buffer_, earliest,
            per_day.get(day, 0), cap, host_tz)
        day += timedelta(days=1)
    return states


def _day_state(page, day, step, blocked, buffer_, earliest, booked_today, cap,
               host_tz) -> str:
    windows = page.windows_for(day.weekday())
    if not windows:
        return "closed"
    if cap and booked_today >= cap:
        return "cap"
    bounds = [(_at(day, w.start_minute, host_tz), _at(day, w.end_minute, host_tz))
              for w in windows]
    any_start = False
    late_enough = False
    for start, window_end in bounds:
        while start + step <= window_end:
            any_start = True
            if start >= earliest:
                late_enough = True
                if not _collides(start, start + step, blocked, buffer_):
                    return "free"
            start += step
    if not any_start:
        return "short"          # the window itself is shorter than the length
    if not late_enough:
        return "notice"
    return "short" if _has_gap(bounds, blocked, buffer_, earliest) else "full"


def _has_gap(bounds, blocked, buffer_, earliest) -> bool:
    """Is any minute of the day's windows left over once everything blocking
    has been laid on them? That is what separates "booked solid" from "the
    leftovers are too small"."""
    for start, end in bounds:
        start = max(start, earliest)
        if start >= end:
            continue
        spans = sorted((b_start - buffer_, b_end + buffer_)
                       for b_start, b_end in blocked
                       if b_start - buffer_ < end and b_end + buffer_ > start)
        cursor = start
        for b_start, b_end in spans:
            if b_start > cursor:
                return True
            cursor = max(cursor, b_end)
            if cursor >= end:
                break
        if cursor < end:
            return True
    return False


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
    """Just the blocking ``(start, end)`` pairs — what ``free_slots`` consumes.

    Two things happen here rather than in the caller, so that a visitor and
    the owner are answered from the same rules: an event the calendar itself
    shows as FREE is dropped, and an all-day one is measured in the host's
    own day (see ``busy_spans``)."""
    from application.services.tz import user_zone

    events = busy_events(user, page, since, until)
    return blocking_spans(events, user_zone(user))


def booked_intervals(user, since: datetime, until: datetime) -> list:
    from application.models.booking import Booking
    return [(b.start, b.end) for b in Booking.objects(
        owner=user, start__lt=until, end__gt=since).only("start", "end")]


def bookings_in(user, since: datetime, until: datetime) -> list:
    """The appointments themselves — the host's view needs their names.

    A booking blocks a slot exactly as hard as a calendar event does (see the
    module docstring), so a panel that lists only the calendar is a panel that
    lies about a day taken by this very page."""
    from application.models.booking import Booking
    return list(Booking.objects(owner=user, start__lt=until, end__gt=since)
                .order_by("start"))


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

    # The calendars are asked again only around THIS slot. Whether one start
    # collides depends on nothing farther away than its buffer (a day either
    # side covers an all-day entry in any zone), and asking for the whole
    # booking window a second time is what made the click slow.
    margin = timedelta(days=1, minutes=int(page.buffer_minutes or 0))
    busy = busy_intervals(user, page, start - margin, end + margin)
    if start not in set(slots_for(user, duration, now=now, busy=busy)):
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

    _attach_customer(user, booking)

    # Before the event, so the calendar entry can carry the link.
    _attach_meeting(user, page, booking, guest_link=meeting_url)
    _write_event(user, page, booking)
    return booking


def recent_duplicate(user, *, start: datetime, name: str, email: str,
                     now=None):
    """The booking this same form made a moment ago, if it did.

    Same slot, same name, same address, within half an hour: a resubmit, not a
    second appointment. Anything else still goes through ``confirm``."""
    from application.models.booking import Booking

    now = now or datetime.now(timezone.utc)
    start = start.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return Booking.objects(owner=user, start=start, name=name[:120],
                           email=email[:255],
                           created_at__gte=now - timedelta(minutes=30)).first()


def _attach_customer(user, booking) -> None:
    """File the appointment under the customer whose mail domain it came from.

    Whose call this is, answered by the one thing the form already carries. It
    is a *filing* decision, not a billing one: nothing is booked into the time
    sheet here, and nothing ever is without somebody saying so from the list —
    which is why a cancelled or moved appointment needs no unwinding.

    Never allowed to break a booking: the visitor has their slot either way, and
    an unassigned appointment is one dropdown in the list."""
    from application.services.customers import customer_for_email

    try:
        from application.services.timetracking import user_teams
        cust = customer_for_email(booking.email, user_teams(user))
    except Exception:
        return
    if cust is None:
        return
    booking.customer = cust
    booking.save()


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
        # In the body as well, for the clients that show only the description
        # in a notification — but LOCATION below is what makes it a button.
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
            timezone=(user.timezone or None),
            # The meeting link belongs in LOCATION: that is the field a
            # calendar turns into a join button, on the lock screen too.
            location=booking.meeting_url or "")
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
    from flask import current_app, url_for

    from application.services.mail import smtp_configured
    from application.services.tz import user_zone

    when_host = booking.start.astimezone(user_zone(user))
    stamp = when_host.strftime("%a %d %b %Y, %H:%M")
    who = booking.name or booking.email or "Someone"

    user.add_notification(
        kind="booking",
        text=f"{who} booked {booking.duration_minutes} min on {stamp}",
        # The appointment list, not the settings form: the bell is telling the
        # host that something ARRIVED, and the answer to "who, when" is the
        # list. Settings is where the page is configured, which is touched
        # once — it was the target only because the feature started out there.
        link=url_for("bookings.index"),
        icon="bi-calendar-check",
        actor=who,
    )
    user.save()

    if not smtp_configured():
        return
    # The mails go out after the visitor has their answer: two SMTP sessions
    # (connect, TLS, login) are seconds the confirmation page need not wait
    # for. No broker, or none reachable, sends them right here as before.
    if current_app.config.get("BOOKING_MAILS_IN_BACKGROUND"):
        try:
            from application.services.sync_tasks import enqueue_booking_mails
            enqueue_booking_mails(str(booking.pk))
            return
        except Exception as exc:  # noqa: BLE001 (any broker failure means "send here")
            log.info("booking %s: mails not queued (%s), sending inline",
                     booking.pk, exc)
    send_confirmations(user, booking)


def send_confirmations(user, booking) -> None:
    """The two confirmation mails, each with the appointment as an ``.ics``.

    Called by ``announce`` when there is no broker, else by the worker
    (``booking.send_mails``). Best effort, like ``announce``: ``send_email``
    swallows what SMTP answers."""
    from application.services.booking_ics import attachment
    from application.services.mail import send_email, smtp_configured
    from application.services.tz import user_zone

    if not smtp_configured():
        return
    when_host = booking.start.astimezone(user_zone(user))
    stamp = when_host.strftime("%a %d %b %Y, %H:%M")
    who = booking.name or booking.email or "Someone"
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


def cancel(user, booking, *, notify: bool = True) -> list:
    """Withdraw one appointment: calendar entry, guest, row — in that order.

    Returns the warnings worth showing the host. None of them stop the
    cancellation: once the host has said "this is off", leaving the row in
    place because a calendar or an SMTP server was unreachable would be the
    worse answer — the slot has to become free again, and the host can chase
    a stuck calendar entry by hand.

    The row is DELETED rather than flagged. A cancelled booking must stop
    blocking its slot in every one of the places that ask (the public page,
    the day states, the host's own panel, the ceilings), and one collection
    that only ever holds live appointments is how that stays true without
    four filters that can each be forgotten.
    """
    from application.services.booking_ics import attachment
    from application.services.calendar_sync import CalendarError
    from application.services.calendar_writeback import delete_event
    from application.services.mail import send_email, smtp_configured
    from application.services.tz import user_zone

    warnings = []
    cal = user.find_calendar(booking.calendar_id) if booking.calendar_id else None
    if cal is not None and booking.event_id:
        try:
            delete_event(cal, user, booking.event_id)
        except CalendarError as exc:
            log.warning("booking %s: event delete failed: %s", booking.pk, exc)
            warnings.append(f"The calendar entry couldn't be removed: {exc}")

    if notify and booking.email and smtp_configured():
        host_name = user.display_name or user.username
        stamp = booking.start.astimezone(user_zone(user)).strftime(
            "%a %d %b %Y, %H:%M")
        send_email(
            booking.email,
            f"Cancelled: your appointment with {host_name}",
            f"Your {booking.duration_minutes}-minute appointment with "
            f"{host_name} on {stamp} ({user_zone(user)}) has been cancelled.\n\n"
            "The attached file removes it from your calendar. You're welcome "
            "to book another time.\n",
            attachments=[attachment(booking, host_name=host_name,
                                    host_email=user.email or "", cancel=True)],
        )
    elif notify and booking.email and not smtp_configured():
        warnings.append("No SMTP is configured, so the guest wasn't told.")

    booking.delete()
    return warnings
