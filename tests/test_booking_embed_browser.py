"""The clean embed in a real browser, framed by a site on another origin.

What only a browser can tell: that the frame is allowed to load at all
(frame-ancestors), that the page inside reports a height the host page then
applies, and that a bare, German embed shows the booking and nothing around it.
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
# Another origin than the app's 127.0.0.1. The host page is handed over by a
# route, which Chromium counts as a public page; its local network check would
# refuse the frame before any header is read, so it is switched off below.
SITE = "http://localhost:8765"

HOST_PAGE = """<!doctype html><html><body style="background:#f4f6f3">
<h1>Termine</h1>
<iframe id="cairn-booking" src="{src}" style="width:100%;border:0;height:820px"></iframe>
<script>
  window.addEventListener("message", function (e) {{
    var f = document.getElementById("cairn-booking");
    if (!f || e.source !== f.contentWindow) return;
    if (!e.data || e.data.cairnBooking !== "height") return;
    f.style.height = Math.max(320, e.data.height) + "px";
  }});
</script></body></html>"""


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class BookingEmbedBrowserTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")
        cls.server = make_server("127.0.0.1", 0, cls.app, threaded=True)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        try:
            cls.pw = sync_playwright().start()
            cls.browser = cls.pw.chromium.launch(
                args=["--disable-features=LocalNetworkAccessChecks"])
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
            for u in User.objects(username="book-embed"):
                Booking.objects(owner=u).delete()
                u.delete()

    def setUp(self):
        from application.models.calendar import ExternalCalendar
        from application.models.user import User
        from application.services import booking as svc
        from tests.test_booking_page import form_data

        self._wipe()
        with self.app.app_context():
            user = User(username="book-embed", email="embed@example.com",
                        level="premium", timezone=BERLIN)
            user.set_password("pw-book-embed")
            cal = ExternalCalendar(provider="caldav", name="Work", can_write=True,
                                   caldav_url="https://dav.example/w/")
            user.calendars = [cal]
            user.save()
            user_id, cal_id = str(user.pk), str(cal.pk_ref)
        self._events, self._busy = svc.busy_events, svc.busy_intervals
        svc.busy_events = lambda user, page, since, until: []
        svc.busy_intervals = lambda user, page, since, until: []
        self.svc = svc
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = user_id
            sess["_fresh"] = True
        data = form_data(slug="embedded", write_calendar_id=cal_id,
                         title="Talk to me", intro="Hi there.")
        for day in range(7):
            data[f"day-{day}"] = "y"
            data[f"start-{day}"] = "00:00"
            data[f"end-{day}"] = "23:30"
        resp = client.post("/settings/booking", data=data,
                           content_type="multipart/form-data",
                           follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.old_ancestors = self.app.config["BOOKING_FRAME_ANCESTORS"]

    def tearDown(self):
        self.svc.busy_events = self._events
        self.svc.busy_intervals = self._busy
        self.app.config["BOOKING_FRAME_ANCESTORS"] = self.old_ancestors

    def _framed(self, query, shot=None):
        """The host page on another origin, framing the booking page.
        Returns (frame or None, iframe height in px)."""
        context = self.browser.new_context(viewport={"width": 900, "height": 900},
                                           timezone_id=BERLIN, color_scheme="light")
        page = context.new_page()
        src = f"{self.base}/book/embedded?{query}"
        page.route(SITE + "/", lambda route: route.fulfill(
            status=200, content_type="text/html", body=HOST_PAGE.format(src=src)))
        try:
            page.goto(SITE + "/")
            frame = None
            for _ in range(50):
                frame = next((f for f in page.frames if "/book/embedded" in f.url), None)
                if frame and frame.query_selector("#slots .book-pick"):
                    break
                page.wait_for_timeout(100)
            ok = frame is not None and frame.query_selector("#slots .book-pick") is not None
            page.wait_for_timeout(300)
            height = page.evaluate(
                "parseInt(document.getElementById('cairn-booking').style.height)")
            if shot:
                page.screenshot(path=shot, full_page=True)
            text = frame.inner_text("body") if ok else ""
            return ok, text, height
        finally:
            context.close()

    def test_a_bare_german_embed_loads_and_sizes_itself(self):
        ok, text, height = self._framed("embed=1&bare=1&lang=de&accent=4f8a2c")
        self.assertTrue(ok, "the frame did not load the booking page")
        # German from the template and from the script.
        self.assertIn("Tage mit freien Zeiten", text)
        self.assertIn("wechseln", text)
        self.assertNotIn("Pick a time", text)
        self.assertNotIn("Talk to me", text)
        self.assertNotIn("Hi there.", text)
        self.assertNotIn("Powered by", text)
        # The host page applied the reported height: no longer the fixed 820.
        self.assertNotEqual(height, 820)
        self.assertGreater(height, 320)

    def test_a_site_not_in_the_list_cannot_frame_it(self):
        self.app.config["BOOKING_FRAME_ANCESTORS"] = "https://elsewhere.example"
        ok, _, _ = self._framed("embed=1&bare=1")
        self.assertFalse(ok)
        self.app.config["BOOKING_FRAME_ANCESTORS"] = SITE
        ok, _, _ = self._framed("embed=1&bare=1")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
