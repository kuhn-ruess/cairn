"""Reminders for planned blocks — a ``VALARM`` in whatever Cairn hands a calendar.

Cairn has no time-triggered reminder of its own: the bell
(``models/notification.py``) fires on an assignment, a hand-back or a
completion, and ``followup_at`` is a review nudge, not an alarm. The cheapest
reminder that is nevertheless a real one is therefore the calendar's own — every
place a planned task leaves this app as an iCalendar object carries an alarm,
and the device that already wakes the user for meetings does the alerting. No
dispatcher, no push, nothing to schedule, nothing that can quietly stop running.

Four decisions, deliberately narrow:

* **One setting and a default, not a field per task.** ``User.reminder_lead_min``
  — minutes before the block, **10** by default, ``0`` = no alarm. A lead time
  that is different on every task is a field nobody fills; the alarm that
  matters is "this starts in a moment".
* **It rides on the PLAN date, never on the due date.** A due date is a
  deadline, and usually a day rather than an instant. A plan date with a time on
  it is the block the user sat down and reserved — that is the thing there is to
  be reminded of. ``Item.plan_date`` is also what the Plan timeline writes.
* **Midnight is not a time.** A plan date at 00:00 in the user's zone means
  "this day", which every planner in the app treats as untimed. An alarm on one
  would fire at 23:50 the evening before, so those get none.
* **A finished task is never reminded of.** Done and dropped drop out.

Three exports carry it, all through :func:`alarm_minutes_for`:

* the CalDAV ``VTODO`` (``views/caldav.py``) — an absolute trigger, because a
  VTODO's relative trigger is ambiguous between DTSTART and DUE and clients
  disagree about which one they anchor to;
* the read-only ``VEVENT`` feed of the same server, when it is showing plan
  dates — a relative trigger, so the alarm follows the block if it is moved;
* the write-back event pushed to Google/CalDAV (``services/calendar_writeback``)
  — the same relative trigger, ``reminders.overrides`` on Google's side.

The booking ``.ics`` (``services/booking_ics.py``) deliberately carries none: an
appointment is already an appointment in the guest's calendar, where their own
default alert applies, and what a stranger's calendar does is not this setting's
business.
"""
from __future__ import annotations

from datetime import timedelta, timezone

#: What a reminder is worth without anybody choosing: long enough to walk to
#: the desk, short enough that it still means "now".
DEFAULT_LEAD_MIN = 10

#: The lead times offered in Settings. ``0`` is "no reminder" — the off switch
#: and the setting are one control on purpose.
LEAD_CHOICES = (0, 5, 10, 15, 30, 60)

#: Statuses that never get an alarm, whatever their plan date says.
_CLOSED = ("done", "dropped")


def lead_minutes(user) -> int:
    """The user's lead time in minutes, ``0`` when reminders are off."""
    try:
        value = int(getattr(user, "reminder_lead_min", DEFAULT_LEAD_MIN) or 0)
    except (TypeError, ValueError):
        return 0
    return value if 0 < value <= 1440 else 0


def is_timed(when, user) -> bool:
    """True if ``when`` carries a real time of day in the user's zone.

    Midnight is how the app stores "this day" — the date pickers, the drag onto
    a day cell and the CalDAV import all land there — so it is the one value
    that must not be treated as an instant.
    """
    if when is None:
        return False
    from application.services.tz import user_zone
    local = when.astimezone(user_zone(user)) if when.tzinfo else when
    return not (local.hour == 0 and local.minute == 0)


def alarm_minutes_for(item, user) -> int:
    """Lead time for this task's alarm, ``0`` when it gets none.

    The single gate every export asks. When it answers non-zero, the instant
    the alarm belongs to is ``item.plan_date``.
    """
    lead = lead_minutes(user)
    if not lead:
        return 0
    if getattr(item, "status", "") in _CLOSED:
        return 0
    if not is_timed(getattr(item, "plan_date", None), user):
        return 0
    return lead


def add_alarm(component, minutes: int, summary: str = "", *, at=None) -> bool:
    """Attach one ``VALARM`` to an icalendar component. False if none was due.

    ``at`` makes the trigger absolute (that instant, in UTC — the RFC allows no
    other zone there), otherwise it is relative: ``minutes`` before the
    component's start. ``ACTION:DISPLAY`` with a ``DESCRIPTION`` is the
    combination every client renders; an ``AUDIO`` alarm is silently upgraded or
    dropped depending on the device.

    ``VALUE=DATE-TIME`` is spelled out on an absolute trigger because TRIGGER's
    DEFAULT value type is DURATION — without the parameter a strict parser reads
    ``20260924T115000Z`` as a duration and throws the alarm away.
    """
    if not minutes or minutes <= 0:
        return False
    from icalendar import Alarm
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", summary or "Reminder")
    if at is not None:
        stamp = at if at.tzinfo is None else at.astimezone(timezone.utc)
        alarm.add("trigger", stamp - timedelta(minutes=minutes),
                  parameters={"VALUE": "DATE-TIME"})
    else:
        alarm.add("trigger", timedelta(minutes=-minutes))
    component.add_component(alarm)
    return True
