"""Reminders for planned blocks — the VALARM every calendar export carries.

Run with ``python -m unittest discover tests``. Pure: no database, no network.

Cairn owns no time-triggered reminder, so the alarm is the calendar's and these
tests pin the decisions rather than the plumbing:

* it rides on a **timed plan date** — never on a due date, never on a plan date
  at local midnight (which the whole app reads as "this day" and which would
  ring at ten to midnight the evening before), never on a closed task;
* **one lead time per user**, ``0`` meaning off, so the off switch and the
  setting are one control;
* the **VTODO trigger is absolute and the VEVENT trigger is relative** — a VTODO
  can carry both a DTSTART and a DUE and clients disagree about which end a bare
  ``TRIGGER:-PT10M`` hangs off, while a VEVENT has exactly one start and an
  alarm that follows the block when it is moved is worth more than one pinned to
  an instant;
* a **booking** ``.ics`` carries none: the guest's calendar has its own default
  and this setting is not about a stranger's phone.
"""
import unittest
from datetime import datetime, timezone

from icalendar import Calendar

from application.services import reminders


class _User:
    """Enough of a user for the timezone and the setting."""

    def __init__(self, tz="Europe/Berlin", lead=10):
        self.timezone = tz
        self.reminder_lead_min = lead


class _Task:
    caldav_uid = ""
    note = ""
    location = ""
    url = ""
    priority = None
    tags = ()
    defer_date = None
    due_date = None
    completed_at = None

    def __init__(self, **kw):
        self.pk = "6ab5629aeeb1bc4235bc8012"
        self.title = "Angebot schreiben"
        self.status = "open"
        self.plan_date = None
        self.created_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.updated_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.__dict__.update(kw)


# 14:00 Berlin on a summer day — a real block, which is the only thing that
# gets an alarm.
BLOCK = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
# 00:00 Berlin — how "planned for the 24th, no time" is stored.
UNTIMED = datetime(2026, 9, 23, 22, 0, tzinfo=timezone.utc)


class TheSettingTests(unittest.TestCase):

    def test_ten_minutes_is_the_default(self):
        self.assertEqual(reminders.DEFAULT_LEAD_MIN, 10)

    def test_zero_is_the_off_switch(self):
        self.assertEqual(reminders.lead_minutes(_User(lead=0)), 0)

    def test_a_lead_time_is_taken_as_it_is(self):
        self.assertEqual(reminders.lead_minutes(_User(lead=30)), 30)

    def test_nonsense_never_becomes_an_alarm(self):
        for value in (None, "", "soon", -5, 99999):
            self.assertEqual(reminders.lead_minutes(_User(lead=value)), 0, value)


class WhatGetsAnAlarmTests(unittest.TestCase):

    def test_a_timed_plan_date(self):
        task = _Task(plan_date=BLOCK)
        self.assertEqual(reminders.alarm_minutes_for(task, _User()), 10)

    def test_midnight_in_the_users_zone_is_a_day_not_a_block(self):
        """Without this the reminder for "Thursday" rings at 23:50 Wednesday."""
        task = _Task(plan_date=UNTIMED)
        self.assertEqual(reminders.alarm_minutes_for(task, _User()), 0)

    def test_the_zone_that_decides_is_the_users(self):
        """The same instant is midnight in Berlin and 22:00 in London — one is a
        day, the other is an evening block, and the user's zone says which."""
        task = _Task(plan_date=UNTIMED)
        self.assertEqual(reminders.alarm_minutes_for(task, _User("Europe/London")), 10)

    def test_a_due_date_alone_gets_nothing(self):
        """A deadline is not a block. Ten minutes' notice of one is noise."""
        task = _Task(due_date=BLOCK)
        self.assertEqual(reminders.alarm_minutes_for(task, _User()), 0)

    def test_a_finished_task_is_never_reminded_of(self):
        for status in ("done", "dropped"):
            task = _Task(plan_date=BLOCK, status=status)
            self.assertEqual(reminders.alarm_minutes_for(task, _User()), 0, status)

    def test_reminders_off_means_nothing_anywhere(self):
        task = _Task(plan_date=BLOCK)
        self.assertEqual(reminders.alarm_minutes_for(task, _User(lead=0)), 0)


class TheVtodoTests(unittest.TestCase):
    """The CalDAV lists Apple Reminders sees."""

    @classmethod
    def setUpClass(cls):
        from application import create_app
        cls.app = create_app("application.config.TestConfig")

    def _ics(self, task, user):
        from application.views.caldav import _todo_ics
        with self.app.test_request_context("/caldav/"):
            return _todo_ics(task, user)

    def _alarm(self, raw):
        todo = Calendar.from_ical(raw).walk("VTODO")[0]
        alarms = todo.walk("VALARM")
        return alarms[0] if alarms else None

    def test_a_planned_block_carries_one_alarm(self):
        alarm = self._alarm(self._ics(_Task(plan_date=BLOCK), _User()))
        self.assertIsNotNone(alarm)
        self.assertEqual(str(alarm.get("action")), "DISPLAY")
        self.assertEqual(str(alarm.get("description")), "Angebot schreiben")

    def test_the_trigger_is_the_instant_ten_minutes_before(self):
        """Absolute, because a VTODO has two ends and clients disagree about
        which one a relative trigger hangs off."""
        alarm = self._alarm(self._ics(_Task(plan_date=BLOCK), _User()))
        self.assertEqual(alarm.get("trigger").dt,
                         datetime(2026, 9, 24, 11, 50, tzinfo=timezone.utc))

    def test_the_absolute_trigger_goes_out_in_utc(self):
        """RFC 5545 requires it, and unlike DUE nobody displays a trigger — so
        the wall-clock reason DUE carries a TZID does not apply."""
        raw = self._ics(_Task(plan_date=BLOCK), _User())
        line = [ln for ln in raw.replace("\r", "").splitlines()
                if ln.startswith("TRIGGER")][0]
        self.assertEqual(line, "TRIGGER;VALUE=DATE-TIME:20260924T115000Z")

    def test_the_lead_time_is_the_users(self):
        alarm = self._alarm(self._ics(_Task(plan_date=BLOCK), _User(lead=60)))
        self.assertEqual(alarm.get("trigger").dt,
                         datetime(2026, 9, 24, 11, 0, tzinfo=timezone.utc))

    def test_an_unplanned_task_exports_exactly_as_before(self):
        self.assertIsNone(self._alarm(self._ics(_Task(due_date=BLOCK), _User())))

    def test_reminders_off_exports_exactly_as_before(self):
        self.assertIsNone(
            self._alarm(self._ics(_Task(plan_date=BLOCK), _User(lead=0))))


class TheVeventFeedTests(unittest.TestCase):
    """The read-only "Cairn (Kalender)" collection of the same server."""

    @classmethod
    def setUpClass(cls):
        from application import create_app
        cls.app = create_app("application.config.TestConfig")

    def _alarm(self, source, user=None, **kw):
        from application.views.caldav import _event_ics
        with self.app.test_request_context("/caldav/"):
            raw = _event_ics(_Task(**kw), source, user or _User())
        ev = Calendar.from_ical(raw).walk("VEVENT")[0]
        alarms = ev.walk("VALARM")
        return alarms[0] if alarms else None

    def test_a_plan_feed_alarms_relative_to_the_start(self):
        """Relative, so the alarm follows the block if the block is dragged."""
        alarm = self._alarm("plan", plan_date=BLOCK)
        self.assertIsNotNone(alarm)
        self.assertEqual(alarm.get("trigger").dt.total_seconds(), -600)

    def test_a_due_feed_alarms_nothing(self):
        self.assertIsNone(self._alarm("due", plan_date=BLOCK, due_date=BLOCK))


class TheWrittenBackEventTests(unittest.TestCase):
    """What Cairn pushes into the calendar the user actually looks at."""

    def test_the_caldav_ics_carries_a_relative_valarm(self):
        from application.services.calendar_sync import _caldav_event_ics
        raw = _caldav_event_ics("uid-1", "Plan: Angebot schreiben", BLOCK,
                                False, "", 45, "Europe/Berlin",
                                alarm_minutes=15)
        ev = Calendar.from_ical(raw).walk("VEVENT")[0]
        alarm = ev.walk("VALARM")[0]
        self.assertEqual(alarm.get("trigger").dt.total_seconds(), -900)
        self.assertEqual(str(alarm.get("action")), "DISPLAY")

    def test_no_lead_time_means_a_byte_for_byte_unchanged_event(self):
        from application.services.calendar_sync import _caldav_event_ics
        args = ("uid-1", "Plan: x", BLOCK, False, "", 45, "Europe/Berlin")
        self.assertEqual(_caldav_event_ics(*args, alarm_minutes=0),
                         _caldav_event_ics(*args))
        self.assertNotIn("VALARM", _caldav_event_ics(*args))

    def test_google_gets_an_override_not_the_calendars_default(self):
        """A planned block's reminder is the user's setting in Cairn, not
        whatever that calendar does to every entry it holds."""
        from application.services.calendar_sync import _event_body
        body = _event_body("Plan: x", BLOCK, False, "", 45, "Europe/Berlin",
                           "", 10)
        self.assertEqual(body["reminders"],
                         {"useDefault": False,
                          "overrides": [{"method": "popup", "minutes": 10}]})

    def test_google_is_left_alone_when_there_is_no_reminder(self):
        from application.services.calendar_sync import _event_body
        self.assertNotIn("reminders",
                         _event_body("Plan: x", BLOCK, False, "", 45,
                                     "Europe/Berlin"))


class TheBookingInviteTests(unittest.TestCase):

    def test_a_guests_appointment_carries_no_alarm_of_ours(self):
        from application.services.booking_ics import build_ics
        raw = build_ics(uid="cairn-booking-1@cairn", start=BLOCK,
                        end=BLOCK, summary="Bastian · Anna")
        self.assertNotIn(b"VALARM", raw)
