"""The booking page's month grid in a real browser: days already over.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skipped
without either. No calendar is reached: ``busy_events`` is stubbed.

The bug: "die seite zeigt im Kalender vergangenen tage die mal buchbar waren,
noch mit einem kreis an, den man nicht klicken kann". A page left open keeps
the times it loaded, and for the host a week off that began last Monday was
filed under every day since then, so days already gone kept the shape of a
day worth opening. They are struck through now, and never a button.

The browser's clock is set a few days past the server's, which is exactly a
page whose times were loaded and whose days have since gone by.
"""
import threading
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - no playwright / mongoengine installed
    sync_playwright = None

BERLIN = "Europe/Berlin"

# Every cell of the grid as [day of month, "past" | "chosen" | "button" | "flat"].
CELLS = """() => Array.from(document.querySelectorAll(".book-cal-cell > *"))
  .map((el) => [parseInt(el.textContent, 10),
                el.classList.contains("book-cal-past") ? "past"
                  : el.classList.contains("btn-primary") ? "chosen"
                  : el.tagName === "BUTTON" ? "button" : "flat"])"""


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class BookingCalendarPastDaysTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()  # forces a real round trip
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")
        cls.server = make_server("127.0.0.1", 0, cls.app, threaded=True)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        try:
            cls.pw = sync_playwright().start()
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:  # no Chromium downloaded
            cls.server.shutdown()
            raise unittest.SkipTest(f"no Chromium for Playwright: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        cls._wipe()
        disconnect(alias="default")

    @classmethod
    def _wipe(cls):
        with cls.app.app_context():
            from application.models.booking import Booking
            from application.models.user import User
            for u in User.objects(username="book-past"):
                Booking.objects(owner=u).delete()
                u.delete()

    def setUp(self):
        from application.models.calendar import ExternalCalendar
        from application.models.user import User
        from application.services import booking as svc
        from tests.test_booking_page import event, form_data

        self._wipe()
        with self.app.app_context():
            user = User(username="book-past", email="past@example.com",
                        level="premium", timezone=BERLIN)
            user.set_password("pw-book-past")
            cal = ExternalCalendar(provider="caldav", name="Work", can_write=True,
                                   caldav_url="https://dav.example/w/")
            user.calendars = [cal]
            user.save()
            user_id, cal_id = str(user.pk), str(cal.pk_ref)

        # The browser lives a few days ahead; never on the 1st, so the month
        # it shows always has days before "today" in it.
        self.now = datetime.now(timezone.utc)
        shift = 3
        if (self.now + timedelta(days=shift)).day == 1:
            shift += 1
        self.browser_now = self.now + timedelta(days=shift)

        # A week off that began before the server's today and runs past the
        # browser's: every one of those days is blocked for the host.
        events = [event(self.now - timedelta(days=4),
                        self.browser_now + timedelta(days=1), title="Urlaub")]
        self._events, self._busy = svc.busy_events, svc.busy_intervals
        svc.busy_events = lambda user, page, since, until: list(events)
        svc.busy_intervals = lambda user, page, since, until: [
            (e.start, e.end) for e in events]
        self.svc = svc

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = user_id
            sess["_fresh"] = True
        # Seven days a week, so whichever days the shift lands on had times.
        data = form_data(slug="past", write_calendar_id=cal_id)
        for day in range(7):
            data[f"day-{day}"] = "y"
            data[f"start-{day}"] = "00:00"
            data[f"end-{day}"] = "23:30"
        resp = client.post("/settings/booking", data=data,
                           content_type="multipart/form-data",
                           follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    def tearDown(self):
        self.svc.busy_events = self._events
        self.svc.busy_intervals = self._busy

    def _grid(self, owner):
        context = self.browser.new_context(
            viewport={"width": 1000, "height": 1000}, timezone_id=BERLIN)
        try:
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            if owner:
                page.goto(f"{self.base}/auth/login")
                page.fill('input[name="email"]', "past@example.com")
                page.fill('input[name="password"]', "pw-book-past")
                page.click('button[type="submit"], input[type="submit"]')
                page.wait_for_load_state("networkidle")
            page.clock.set_fixed_time(self.browser_now)
            page.goto(f"{self.base}/book/past")
            page.wait_for_selector(".book-cal-grid")
            title = page.inner_text(".book-cal-title")
            cells = page.evaluate(CELLS)
            self.assertEqual(errors, [])
            return title, cells
        finally:
            context.close()

    def _check(self, owner):
        title, cells = self._grid(owner)
        local_today = self.browser_now.astimezone(ZoneInfo(BERLIN)).date()
        # The month on screen is the browser's own: its first bookable day.
        self.assertIn(local_today.strftime("%Y"), title)
        # Every day before today is struck through and never a button; today
        # and what follows are not.
        self.assertEqual([n for n, kind in cells if kind == "past"],
                         list(range(1, local_today.day)))
        # The day whose times are listed is not one that is gone either.
        chosen = [n for n, kind in cells if kind == "chosen"]
        self.assertEqual(len(chosen), 1)
        self.assertGreaterEqual(chosen[0], local_today.day)

    def test_a_visitor_sees_days_gone_by_struck_through(self):
        self._check(owner=False)

    def test_the_host_sees_no_past_day_as_blocked(self):
        """The week off began before today: only today onwards is opened."""
        self._check(owner=True)


if __name__ == "__main__":
    unittest.main()
