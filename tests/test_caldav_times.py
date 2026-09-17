"""views/caldav — a timed reminder must survive the round trip to a client.

The export used to write DUE as UTC (``20260917T140500Z``). Apple Reminders
reads those digits as local time: a 16:05 Berlin reminder showed up as 14:05,
fired straight away, and was written back as 14:05 local — two hours earlier on
every sync. So DUE/DTSTART go out in the user's timezone, and the two ways a
client can answer (with TZID, or floating because it dropped the TZID) both
have to come back as the very same instant.

Run with ``python -m unittest discover tests``.
"""
import unittest
from datetime import datetime, timezone

from icalendar import Calendar, Todo

from application.views.caldav import _local, _to_dt


class _User:
    def __init__(self, tz="Europe/Berlin"):
        self.timezone = tz


def _emit(prop, dt, user):
    """Serialize ``dt`` the way the export does and give back the VTODO line."""
    todo = Todo()
    todo.add("uid", "x")
    todo.add(prop, _local(dt, user))
    cal = Calendar()
    cal.add("version", "2.0")
    cal.add("prodid", "-//Cairn//test//")
    cal.add_component(todo)
    for line in cal.to_ical().decode().splitlines():
        if line.startswith(prop.upper()):
            return line
    raise AssertionError(f"no {prop} in {cal.to_ical().decode()}")


def _read(line, user):
    ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//\r\nBEGIN:VTODO\r\n"
           f"UID:x\r\n{line}\r\nEND:VTODO\r\nEND:VCALENDAR\r\n")
    todo = Calendar.from_ical(ics).walk("VTODO")[0]
    return _to_dt(todo.get("due"), user)


class CaldavTimeTest(unittest.TestCase):

    # 16:05 Berlin, which is what Cairn stores.
    DUE = datetime(2026, 9, 17, 14, 5, tzinfo=timezone.utc)

    def test_due_goes_out_as_the_users_wall_clock(self):
        self.assertEqual(_emit("due", self.DUE, _User()),
                         "DUE;TZID=Europe/Berlin:20260917T160500")

    def test_dtstart_too(self):
        self.assertEqual(_emit("dtstart", self.DUE, _User()),
                         "DTSTART;TZID=Europe/Berlin:20260917T160500")

    def test_a_client_that_echoes_the_tzid_changes_nothing(self):
        self.assertEqual(_read(_emit("due", self.DUE, _User()), _User()), self.DUE)

    def test_a_client_that_drops_the_tzid_changes_nothing_either(self):
        # Floating time — the same digits without the zone. _to_dt reads them
        # as the user's local time, so the instant is the one we sent.
        floating = _emit("due", self.DUE, _User()).split(":", 1)[1]
        self.assertEqual(_read(f"DUE:{floating}", _User()), self.DUE)

    def test_the_old_utc_form_is_what_drifted(self):
        # Kept as the reason the export changed: read as floating (what the
        # client did), the UTC digits are two hours early in summer.
        self.assertEqual(_read("DUE:20260917T140500", _User()),
                         datetime(2026, 9, 17, 12, 5, tzinfo=timezone.utc))

    def test_a_user_without_a_timezone_falls_back_to_utc(self):
        # No zone to name — icalendar writes the UTC form, as before.
        self.assertEqual(_emit("due", self.DUE, _User(tz="UTC")),
                         "DUE:20260917T140500Z")

    def test_winter_keeps_its_own_offset(self):
        winter = datetime(2026, 1, 17, 14, 5, tzinfo=timezone.utc)
        self.assertEqual(_emit("due", winter, _User()),
                         "DUE;TZID=Europe/Berlin:20260117T150500")

    def test_nothing_stays_nothing(self):
        self.assertIsNone(_local(None, _User()))


if __name__ == "__main__":
    unittest.main()
