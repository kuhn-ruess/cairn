"""CalDAV discovery: one address in, every calendar out.

Run with ``python -m unittest discover tests``. No database and no network —
the multistatus bodies are what a Nextcloud answers, and the request layer is
replaced so the walk itself (address → principal → calendar home → the
collections under it) is what's under test.

The things worth pinning: a calendar home holds more than calendars (the home
itself, the scheduling inbox/outbox, subscriptions, task lists), an address
that already IS one calendar must stay one calendar, and exactly one of the
answers is the ``default`` — the only one that starts out visible in Today.
"""
import unittest

from application.services import calendar_sync as cs


HOME = "https://cloud.example.com/remote.php/dav/calendars/alice/"

LISTING = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
               xmlns:cs="http://calendarserver.org/ns/"
               xmlns:a="http://apple.com/ns/ical/">
  <d:response>
    <d:href>/remote.php/dav/calendars/alice/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>
      </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/alice/personal/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
      <d:displayname>Privat</d:displayname>
      <a:calendar-color>#FF2968FF</a:calendar-color>
      <c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>
      </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/alice/arbeit/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
      <d:displayname>Arbeit</d:displayname>
      <c:supported-calendar-component-set><c:comp name="VEVENT"/><c:comp name="VTODO"/></c:supported-calendar-component-set>
      </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/alice/tasks/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
      <d:displayname>Aufgaben</d:displayname>
      <c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>
      </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/alice/feiertage/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><cs:subscribed/></d:resourcetype>
      <d:displayname>Feiertage</d:displayname>
      </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/alice/inbox/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><c:schedule-inbox/></d:resourcetype>
      </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
    <d:propstat><d:prop><d:displayname/><a:calendar-color/></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
  </d:response>
</d:multistatus>
"""

ROOT = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/remote.php/dav/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/></d:resourcetype>
      <d:current-user-principal><d:href>/remote.php/dav/principals/users/alice/</d:href></d:current-user-principal>
      </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>
"""

PRINCIPAL = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>/remote.php/dav/principals/users/alice/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><d:principal/></d:resourcetype>
      <c:calendar-home-set><d:href>/remote.php/dav/calendars/alice/</d:href></c:calendar-home-set>
      <c:schedule-default-calendar-URL><d:href>/remote.php/dav/calendars/alice/personal/</d:href></c:schedule-default-calendar-URL>
      </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>
"""

ONE_CALENDAR = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>/remote.php/dav/calendars/alice/personal/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
      <d:displayname>Privat</d:displayname>
      </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>
"""


class ListingTests(unittest.TestCase):
    def test_only_event_calendars_come_back(self):
        found = cs.caldav_calendars_from_listing(LISTING, HOME)
        self.assertEqual([c["name"] for c in found], ["Arbeit", "Privat"])

    def test_urls_are_absolute_and_end_in_a_slash(self):
        found = cs.caldav_calendars_from_listing(LISTING, HOME)
        self.assertEqual(
            {c["url"] for c in found},
            {HOME + "personal/", HOME + "arbeit/"},
        )

    def test_apple_colour_loses_its_alpha(self):
        privat = [c for c in cs.caldav_calendars_from_listing(LISTING, HOME)
                  if c["name"] == "Privat"][0]
        self.assertEqual(privat["color"], "#FF2968")

    def test_a_calendar_without_a_colour_says_so(self):
        arbeit = [c for c in cs.caldav_calendars_from_listing(LISTING, HOME)
                  if c["name"] == "Arbeit"][0]
        self.assertEqual(arbeit["color"], "")


class WalkTests(unittest.TestCase):
    """The chain a bare server address has to travel."""

    def setUp(self):
        self.asked = []
        self.answers = {}
        self._real = cs._caldav_propfind
        cs._caldav_propfind = self._fake

    def tearDown(self):
        cs._caldav_propfind = self._real

    def _fake(self, url, auth, body, depth):
        self.asked.append((url, depth))
        try:
            return url, self.answers[url]
        except KeyError:
            raise cs.CalendarError(f"CalDAV PROPFIND 405: {url}") from None

    def _nextcloud(self):
        self.answers = {
            "https://cloud.example.com/remote.php/dav/": ROOT,
            "https://cloud.example.com/remote.php/dav/principals/users/alice/": PRINCIPAL,
            HOME: LISTING,
        }

    def test_base_url_reaches_every_calendar(self):
        self._nextcloud()
        root, found = cs.discover_caldav_calendars(
            "https://cloud.example.com/remote.php/dav/", "alice", "pw")
        self.assertEqual([c["name"] for c in found], ["Arbeit", "Privat"])
        self.assertEqual(root, HOME)
        self.assertEqual(self.asked[-1], (HOME, "1"))

    def test_the_servers_default_calendar_is_the_one_marked_default(self):
        self._nextcloud()
        _root, found = cs.discover_caldav_calendars(
            "https://cloud.example.com/remote.php/dav/", "alice", "pw")
        self.assertEqual([c["name"] for c in found if c["default"]], ["Privat"])

    def test_without_a_default_the_first_calendar_takes_the_role(self):
        self._nextcloud()
        self.answers[
            "https://cloud.example.com/remote.php/dav/principals/users/alice/"
        ] = PRINCIPAL.replace(
            "<c:schedule-default-calendar-URL>"
            "<d:href>/remote.php/dav/calendars/alice/personal/</d:href>"
            "</c:schedule-default-calendar-URL>", "")
        _root, found = cs.discover_caldav_calendars(
            "https://cloud.example.com/remote.php/dav/", "alice", "pw")
        self.assertEqual([c["name"] for c in found if c["default"]], ["Arbeit"])

    def test_bare_server_address_falls_back_to_well_known(self):
        self.answers = {
            "https://cloud.example.com/.well-known/caldav": ROOT,
            "https://cloud.example.com/remote.php/dav/principals/users/alice/": PRINCIPAL,
            HOME: LISTING,
        }
        _root, found = cs.discover_caldav_calendars(
            "https://cloud.example.com", "alice", "pw")
        self.assertEqual([c["name"] for c in found], ["Arbeit", "Privat"])

    def test_a_single_collection_url_stays_a_single_calendar(self):
        url = HOME + "personal/"
        self.answers = {url: ONE_CALENDAR}
        root, found = cs.discover_caldav_calendars(url, "alice", "pw")
        self.assertEqual(
            found, [{"url": url, "name": "Privat", "color": "", "default": True}])
        self.assertEqual(root, url)
        self.assertEqual(len(self.asked), 1)

    def test_an_unreachable_address_reports_the_address_that_was_typed(self):
        with self.assertRaises(cs.CalendarError) as caught:
            cs.discover_caldav_calendars("https://cloud.example.com/dav/", "alice", "pw")
        self.assertIn("/dav/", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
