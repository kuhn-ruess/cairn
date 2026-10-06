"""The booking form in a real browser: what the visitor sees while it books.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skipped
without either. No calendar is reached: ``busy_events`` is stubbed, and so is
the event write, which is held open until the test lets it finish.

The report: "wenn man bucht dauert es recht lange. Ein User hat daher sogar
die Seite schon mal neu geladen." The request writes the calendar entry before
it answers, and for those seconds the page looked exactly as it did before the
click. Now the button turns into a disabled spinner, a line says to wait, and
the form cannot be sent a second time.
"""
import threading
import unittest

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover (no playwright or mongoengine installed)
    sync_playwright = None

BERLIN = "Europe/Berlin"


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class BookingSubmitBusyTests(unittest.TestCase):

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
            for u in User.objects(username="book-busy"):
                Booking.objects(owner=u).delete()
                u.delete()

    def setUp(self):
        from application.models.calendar import ExternalCalendar
        from application.models.user import User
        from application.services import booking as svc
        from tests.test_booking_page import form_data

        self._wipe()
        with self.app.app_context():
            user = User(username="book-busy", email="busy@example.com",
                        level="premium", timezone=BERLIN)
            user.set_password("pw-book-busy")
            cal = ExternalCalendar(provider="caldav", name="Work", can_write=True,
                                   caldav_url="https://dav.example/w/")
            user.calendars = [cal]
            user.save()
            user_id, cal_id = str(user.pk), str(cal.pk_ref)

        self.svc = svc
        self._events, self._busy = svc.busy_events, svc.busy_intervals
        self._write = svc._write_event
        svc.busy_events = lambda user, page, since, until: []
        svc.busy_intervals = lambda user, page, since, until: []

        # The slow part of a real booking, held until the test says so.
        self.writing = threading.Event()
        self.release = threading.Event()

        def slow_write(user, page, booking):
            self.writing.set()
            self.release.wait(20)
            booking.event_id = "evt-1"
            booking.save()

        svc._write_event = slow_write

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = user_id
            sess["_fresh"] = True
        data = form_data(slug="busy", write_calendar_id=cal_id)
        for day in range(7):
            data[f"day-{day}"] = "y"
            data[f"start-{day}"] = "00:00"
            data[f"end-{day}"] = "23:30"
        resp = client.post("/settings/booking", data=data,
                           content_type="multipart/form-data",
                           follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    def tearDown(self):
        self.release.set()
        self.svc.busy_events = self._events
        self.svc.busy_intervals = self._busy
        self.svc._write_event = self._write

    def test_the_form_says_it_is_booking_and_cannot_be_sent_twice(self):
        context = self.browser.new_context(
            viewport={"width": 1000, "height": 1000}, timezone_id=BERLIN)
        try:
            page = context.new_page()
            errors, posts = [], []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("request", lambda req: posts.append(req.url)
                    if req.method == "POST" else None)
            page.goto(f"{self.base}/book/busy")
            page.click(".book-slot >> nth=0")
            page.fill("#name", "Bob")
            page.fill("#email", "bob@example.com")
            # A page whose own form POST is still pending answers nothing to
            # the test driver until the response arrives, so the answer is
            # sent into a frame instead. The page itself, and everything its
            # submit handler does, stays exactly what the visitor has.
            page.evaluate("""() => {
                const sink = document.createElement("iframe");
                sink.name = "sink";
                document.body.appendChild(sink);
                document.getElementById("book-form").target = "sink";
            }""")

            # Before the click nothing says "busy".
            self.assertFalse(page.is_visible("#book-busy"))
            self.assertTrue(page.is_enabled("#book-submit"))

            page.click("#book-submit", no_wait_after=True)
            self.assertTrue(self.writing.wait(10), "the POST never arrived")

            # The server is still at work: the page says so.
            self.assertTrue(page.is_visible("#book-busy"))
            self.assertIn("please wait", page.inner_text("#book-busy"))
            self.assertTrue(page.is_disabled("#book-submit"))
            self.assertTrue(page.is_visible("#book-submit .spinner-border"))
            self.assertIn("Booking", page.inner_text("#book-submit"))
            self.assertTrue(page.is_disabled("#cancel"))

            # A second send while the first runs goes nowhere.
            page.evaluate("document.getElementById('book-form').requestSubmit()")

            self.release.set()
            page.frame_locator("iframe[name=sink]").get_by_text(
                "You're booked").wait_for(timeout=10000)
            self.assertEqual([u for u in posts if u.endswith("/book/busy/book")],
                             [f"{self.base}/book/busy/book"])
            self.assertEqual(errors, [])
        finally:
            context.close()

        with self.app.app_context():
            from application.models.booking import Booking
            from application.models.user import User
            owner = User.objects.get(username="book-busy")
            self.assertEqual(Booking.objects(owner=owner).count(), 1)


if __name__ == "__main__":
    unittest.main()
