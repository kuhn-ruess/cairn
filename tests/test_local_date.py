"""``tzutil.local_date`` — which calendar day a stored datetime belongs to.

An all-day plan/due date is written as midnight in the user's zone, so in
Berlin it sits at 22:00 UTC on the day *before*. Reading it back with a bare
``.date()`` therefore answers with yesterday, and everything keyed off that —
Today membership, the sidebar badge, the "today's plan" chip, "tomorrow" —
fires a day early. This is the one place that conversion happens now.

Run with ``python -m unittest discover tests``.
"""
import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from application.services.tzutil import local_date

BERLIN = ZoneInfo("Europe/Berlin")


class LocalDateTest(unittest.TestCase):

    def test_none_passes_through(self):
        self.assertIsNone(local_date(None, BERLIN))

    def test_all_day_plan_belongs_to_its_own_day(self):
        # 17 Sep 00:00 Berlin — stored as 16 Sep 22:00 UTC. The bug this
        # guards: a task planned for tomorrow showing up in Today.
        plan = datetime(2026, 9, 16, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(local_date(plan, BERLIN), date(2026, 9, 17))

    def test_planned_for_today_stays_today(self):
        plan = datetime(2026, 9, 15, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(local_date(plan, BERLIN), date(2026, 9, 16))

    def test_winter_offset(self):
        # +01:00 in January: local midnight is 23:00 UTC the day before.
        plan = datetime(2026, 1, 14, 23, 0, tzinfo=timezone.utc)
        self.assertEqual(local_date(plan, BERLIN), date(2026, 1, 15))

    def test_timed_value_keeps_its_day(self):
        plan = datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(local_date(plan, BERLIN), date(2026, 9, 15))

    def test_naive_value_reads_as_utc(self):
        plan = datetime(2026, 9, 16, 22, 0)
        self.assertEqual(local_date(plan, BERLIN), date(2026, 9, 17))

    def test_utc_user_sees_the_stored_day(self):
        plan = datetime(2026, 9, 16, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(local_date(plan, timezone.utc), date(2026, 9, 16))


if __name__ == "__main__":
    unittest.main()
