"""Where a meeting link lands in the event Cairn writes.

Run with ``python -m unittest discover tests``. No network: both writers
build their payload in a pure function, and that payload is what is checked.

LOCATION, not the description. It is the field a calendar turns into a join
button — on the phone's lock screen, in the notification five minutes before
— while a link in the body is a line of text somebody has to find and copy
at the moment they are already late.
"""
import unittest
from datetime import datetime, timezone

from application.services.calendar_sync import _caldav_event_ics, _event_body

WHEN = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
LINK = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc"


class GoogleBodyTests(unittest.TestCase):

    def body(self, **kw):
        kw.setdefault("title", "Bob (30 min)")
        kw.setdefault("when", WHEN)
        kw.setdefault("all_day", False)
        kw.setdefault("description", "Booked through Cairn")
        return _event_body(kw["title"], kw["when"], kw["all_day"],
                           kw["description"], kw.get("duration_minutes", 30),
                           kw.get("timezone"), kw.get("location", ""))

    def test_the_link_goes_into_location(self):
        self.assertEqual(self.body(location=LINK)["location"], LINK)

    def test_no_link_means_no_field_at_all(self):
        """An empty location on a Google event is still a location — and a
        calendar shows the row for it."""
        self.assertNotIn("location", self.body())

    def test_an_all_day_event_carries_it_too(self):
        self.assertEqual(self.body(all_day=True, location=LINK)["location"], LINK)

    def test_a_naive_time_still_gets_its_zone_and_the_link(self):
        got = self.body(when=WHEN.replace(tzinfo=None), timezone="Europe/Berlin",
                        location=LINK)
        self.assertEqual(got["start"]["timeZone"], "Europe/Berlin")
        self.assertEqual(got["location"], LINK)


class CaldavIcsTests(unittest.TestCase):

    def ics(self, location=""):
        return _caldav_event_ics("uid-1", "Bob (30 min)", WHEN, False,
                                 "Booked through Cairn", 30, None,
                                 location).replace("\r\n ", "")

    def test_location_and_url_are_both_written(self):
        """LOCATION is the join button; URL is what the clients with a link
        field read."""
        out = self.ics(LINK)
        self.assertIn("LOCATION:" + LINK, out)
        self.assertIn("URL:" + LINK, out)

    def test_nothing_is_written_without_one(self):
        out = self.ics()
        self.assertNotIn("LOCATION", out)
        self.assertNotIn("URL", out)


if __name__ == "__main__":
    unittest.main()
