"""The day sheet's start and end fields in a real browser (WebKit and Chromium).

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright (``.venv``); each engine that is not
downloaded is skipped, and so is the whole file without either.

The report: "ich kann auf der timelogging seite keine zeit mehr loggen, das
starttime feld wird angemeckert", in Safari, on a project booked by the clock.
The fields were ``type="time"``. Safari draws such a field, empty or half
typed, with placeholder digits that look like a time ("12:00" after typing
only "12") while its value is empty, and the Log button then only put the
cursor back into the START field: nothing sent, nothing said. The fields are
plain text now and read 9, 930, 9.30 and 09:30 alike; one that can't be read
is marked and named, and it is that field which gets the cursor.
"""
import threading
import unittest
from datetime import datetime

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover (no playwright or mongoengine installed)
    sync_playwright = None

EMAIL = "clockfields@example.com"
PASSWORD = "pw-clock-fields"


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class ClockFieldsBrowserTests(unittest.TestCase):

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
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browsers = {}
        for name in ("webkit", "chromium"):
            try:
                cls.browsers[name] = getattr(cls.pw, name).launch()
            except Exception:  # that engine is not downloaded
                pass
        if not cls.browsers:
            cls.pw.stop()
            cls.server.shutdown()
            raise unittest.SkipTest("no browser for Playwright")

    @classmethod
    def tearDownClass(cls):
        for b in cls.browsers.values():
            b.close()
        cls.pw.stop()
        cls.server.shutdown()
        with cls.app.app_context():
            cls._wipe()
        disconnect(alias="default")

    @staticmethod
    def _wipe():
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        for doc in (TimeBucket, TimeTrackingProject, Customer, Team):
            doc.objects.delete()
        User.objects(email=EMAIL).delete()

    def setUp(self):
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeTrackingProject)
        from application.models.user import User
        with self.app.app_context():
            self._wipe()
            user = User(username="clockfields", email=EMAIL,
                        level="team_admin", timezone="Europe/Berlin")
            user.set_password(PASSWORD)
            user.save()
            team = Team(name="T", admin=user, admins=[user]).save()
            user.team = team
            drv = Customer(team=team, name="Deutsche Rentenversicherung Bund").save()
            project = TimeTrackingProject(team=team, customer=drv, name="Beratung",
                                          time_mode="clock").save()
            user.tt_last_project = str(project.pk)
            user.save()

    def _entries(self):
        from application.models.timetracking import TimeBucket
        with self.app.app_context():
            return [e for b in TimeBucket.objects for e in b.entries
                    if e.status == "active"]

    def _day_sheet(self, browser):
        page = browser.new_page(locale="de-DE")
        page.goto(self.base + "/auth/login")
        page.fill("input[name=email]", EMAIL)
        page.fill("input[name=password]", PASSWORD)
        page.click("input[type=submit]")
        page.wait_for_load_state("networkidle")
        page.goto(self.base + "/time/day")
        page.wait_for_load_state("networkidle")
        return page, page.locator("#tt-day .time-add").first

    def _type(self, page, field, text):
        field.click()
        page.keyboard.press("ControlOrMeta+a")
        page.keyboard.type(text)

    def test_times_typed_the_short_way_are_booked(self):
        for name, browser in self.browsers.items():
            with self.subTest(browser=name):
                with self.app.app_context():
                    self._wipe()
                self.setUp()
                page, add = self._day_sheet(browser)
                start = add.locator(".time-add-start")
                end = add.locator(".time-add-end")
                self.assertEqual(start.get_attribute("type"), "text")
                self._type(page, start, "10.30")
                # "12" was what Safari showed as "12:00" and sent as nothing.
                self._type(page, end, "12")
                with page.expect_response(lambda r: "/time/day/log" in r.url):
                    add.locator(".time-add-btn").click()
                page.wait_for_timeout(300)
                [entry] = self._entries()
                self.assertEqual((entry.start_time, entry.end_time, entry.minutes),
                                 ("10:30", "12:00", 90))
                page.close()

    def test_an_unreadable_end_is_named_not_the_start(self):
        for name, browser in self.browsers.items():
            with self.subTest(browser=name):
                page, add = self._day_sheet(browser)
                posted = []
                page.on("request", lambda r: posted.append(r.url)
                        if r.method == "POST" else None)
                self._type(page, add.locator(".time-add-start"), "10:30")
                self._type(page, add.locator(".time-add-end"), "25")
                add.locator(".time-add-btn").click()
                page.wait_for_timeout(300)
                self.assertEqual(posted, [])
                self.assertEqual(self._entries(), [])
                end_class = add.locator(".time-add-end").get_attribute("class")
                self.assertIn("is-invalid", end_class)
                self.assertNotIn("is-invalid",
                                 add.locator(".time-add-start").get_attribute("class"))
                self.assertIn("not a time of day",
                              page.locator(".tt-warning").first.inner_text())
                page.close()


class ParseClockTests(unittest.TestCase):

    def test_short_spellings_read_as_times_of_day(self):
        from application.services.timetracking import parse_clock
        self.assertEqual(parse_clock("9", "12"), (180, "09:00", "12:00"))
        self.assertEqual(parse_clock("930", "10.15"), (45, "09:30", "10:15"))
        self.assertIsNone(parse_clock("25", "12"))
        self.assertIsNone(parse_clock("9:7", "12"))


if __name__ == "__main__":
    unittest.main()
