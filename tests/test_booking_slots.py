"""Free/busy maths for the booking page.

Run with ``python -m unittest discover tests``. No database and no network:
``free_slots`` is a pure function over intervals, which is the whole reason it
was written as one — a slot handed out wrongly is somebody's afternoon booked
twice, and that must be provable without a Nextcloud.

The clock is frozen at Monday 2026-09-14 06:00 UTC (08:00 in Berlin) so
"today", the notice window and the DST cases all mean something fixed.
"""
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from application.models.booking import BookingHour, BookingPage
from application.services.booking import free_slots

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc)   # Mon 08:00 Berlin


def page(**kw):
    """A page bookable Mon–Fri 09:00–17:00 Berlin, 30 and 60 minutes."""
    defaults = dict(
        enabled=True, slug="host", durations=[30, 60], days_ahead=7,
        min_notice_hours=0, buffer_minutes=0, max_per_day=0,
        hours=[BookingHour(weekday=d, start_minute=9 * 60, end_minute=17 * 60)
               for d in range(5)],
    )
    defaults.update(kw)
    return BookingPage(**defaults)


def berlin(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=BERLIN)


def hhmm(slots, day):
    """The slot times on one Berlin day, as ``"09:00"`` strings."""
    return [s.astimezone(BERLIN).strftime("%H:%M")
            for s in slots if s.astimezone(BERLIN).date() == day]


class SlotTests(unittest.TestCase):

    def call(self, p, duration=30, busy=(), booked=(), now=NOW):
        return free_slots(p, duration, busy=list(busy), booked=list(booked),
                          now=now, host_tz=BERLIN)

    def test_a_day_is_filled_from_the_window_start(self):
        slots = self.call(page(), 60)
        self.assertEqual(hhmm(slots, berlin(2026, 9, 14, 9).date()),
                         ["09:00", "10:00", "11:00", "12:00", "13:00",
                          "14:00", "15:00", "16:00"])

    def test_a_slot_that_would_run_past_the_window_is_not_offered(self):
        p = page(hours=[BookingHour(weekday=0, start_minute=9 * 60,
                                    end_minute=10 * 60 + 30)])
        self.assertEqual(hhmm(self.call(p, 60), berlin(2026, 9, 14, 9).date()),
                         ["09:00"])

    def test_weekends_are_not_offered(self):
        slots = self.call(page())
        days = {s.astimezone(BERLIN).weekday() for s in slots}
        self.assertEqual(days, {0, 1, 2, 3, 4})

    def test_an_event_blocks_exactly_the_slots_it_covers(self):
        busy = [(berlin(2026, 9, 14, 10), berlin(2026, 9, 14, 11))]
        slots = hhmm(self.call(page(), 30, busy=busy), berlin(2026, 9, 14, 9).date())
        self.assertNotIn("10:00", slots)
        self.assertNotIn("10:30", slots)
        self.assertIn("09:30", slots)
        self.assertIn("11:00", slots)

    def test_a_buffer_eats_the_slot_on_each_side(self):
        busy = [(berlin(2026, 9, 14, 11), berlin(2026, 9, 14, 12))]
        slots = hhmm(self.call(page(buffer_minutes=15), 30, busy=busy),
                     berlin(2026, 9, 14, 9).date())
        self.assertNotIn("10:30", slots)   # would end 11:00, inside the buffer
        self.assertNotIn("12:00", slots)   # would start inside it too
        self.assertIn("10:00", slots)
        self.assertIn("12:30", slots)

    def test_an_existing_booking_blocks_like_an_event(self):
        booked = [(berlin(2026, 9, 14, 14), berlin(2026, 9, 14, 15))]
        slots = hhmm(self.call(page(), 60, booked=booked),
                     berlin(2026, 9, 14, 9).date())
        self.assertNotIn("14:00", slots)
        self.assertIn("15:00", slots)

    def test_the_notice_window_hides_everything_before_it(self):
        slots = hhmm(self.call(page(min_notice_hours=4)),
                     berlin(2026, 9, 14, 9).date())
        # 08:00 Berlin + 4h → the first bookable start is 12:00.
        self.assertEqual(slots[0], "12:00")

    def test_a_day_at_its_cap_disappears_entirely(self):
        booked = [(berlin(2026, 9, 14, 9), berlin(2026, 9, 14, 9, 30)),
                  (berlin(2026, 9, 14, 15), berlin(2026, 9, 14, 15, 30))]
        slots = self.call(page(max_per_day=2), 30, booked=booked)
        self.assertEqual(hhmm(slots, berlin(2026, 9, 14, 9).date()), [])
        # …and only that day: the next one is untouched.
        self.assertTrue(hhmm(slots, berlin(2026, 9, 15, 9).date()))

    def test_the_horizon_is_the_last_day_offered(self):
        slots = self.call(page(days_ahead=2))
        last = max(s.astimezone(BERLIN).date() for s in slots)
        self.assertEqual(last, berlin(2026, 9, 16, 9).date())

    def test_a_length_the_page_does_not_offer_yields_nothing(self):
        self.assertEqual(self.call(page(), 45), [])

    def test_slots_come_back_in_utc_and_in_order(self):
        slots = self.call(page(), 60)
        self.assertTrue(all(s.tzinfo == timezone.utc for s in slots))
        self.assertEqual(slots, sorted(slots))

    def test_wall_clock_survives_the_dst_change(self):
        """The last Sunday in October Berlin goes 02:00 → 01:00 (CEST → CET).
        A 09:00 window has to stay 09:00 local on both sides of it, which is
        exactly what a fixed UTC offset would get wrong."""
        p = page(days_ahead=45,
                 hours=[BookingHour(weekday=d, start_minute=9 * 60,
                                    end_minute=10 * 60) for d in range(5)])
        slots = free_slots(p, 60, busy=[], booked=[], now=NOW, host_tz=BERLIN)
        before = [s for s in slots
                  if s.astimezone(BERLIN).date() == berlin(2026, 10, 23, 9).date()]
        after = [s for s in slots
                 if s.astimezone(BERLIN).date() == berlin(2026, 10, 26, 9).date()]
        self.assertEqual(before[0].astimezone(BERLIN).strftime("%H:%M"), "09:00")
        self.assertEqual(after[0].astimezone(BERLIN).strftime("%H:%M"), "09:00")
        # And the UTC instants really are an hour apart — the offset moved.
        self.assertNotEqual(before[0].hour, after[0].hour)

    def test_two_windows_on_one_day_are_both_offered(self):
        p = page(hours=[
            BookingHour(weekday=0, start_minute=9 * 60, end_minute=11 * 60),
            BookingHour(weekday=0, start_minute=14 * 60, end_minute=16 * 60),
        ])
        self.assertEqual(hhmm(self.call(p, 60), berlin(2026, 9, 14, 9).date()),
                         ["09:00", "10:00", "14:00", "15:00"])

    def test_an_all_day_event_takes_the_whole_day(self):
        busy = [(berlin(2026, 9, 15, 0), berlin(2026, 9, 16, 0))]
        slots = self.call(page(), 30, busy=busy)
        self.assertEqual(hhmm(slots, berlin(2026, 9, 15, 9).date()), [])
        self.assertTrue(hhmm(slots, berlin(2026, 9, 16, 9).date()))

    def test_a_window_ending_at_midnight_is_not_an_error(self):
        p = page(hours=[BookingHour(weekday=0, start_minute=22 * 60,
                                    end_minute=24 * 60)])
        self.assertEqual(hhmm(self.call(p, 60), berlin(2026, 9, 14, 9).date()),
                         ["22:00", "23:00"])

    def test_today_starts_where_the_clock_is_not_where_the_window_is(self):
        """A page visited at 13:20 must not offer 09:00 today."""
        now = berlin(2026, 9, 14, 13, 20).astimezone(timezone.utc)
        slots = hhmm(self.call(page(), 30, now=now), berlin(2026, 9, 14, 9).date())
        self.assertEqual(slots[0], "13:30")


class HorizonTests(unittest.TestCase):
    """The cheapest possible guard on the loop: it terminates and stays small."""

    def test_a_years_horizon_is_still_one_pass(self):
        p = page(days_ahead=365)
        slots = free_slots(p, 60, busy=[], booked=[], now=NOW, host_tz=BERLIN)
        self.assertLess(len(slots), 365 * 8 + 1)
        self.assertGreater(len(slots), 200 * 8)
        span = max(slots) - min(slots)
        self.assertLess(span, timedelta(days=366))


if __name__ == "__main__":
    unittest.main()
