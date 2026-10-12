"""A calendar event's customer chip and its "Log time" button, in a browser.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``)
and Playwright with Chromium (``.venv``); skipped without either.

``test_event_customers.py`` pins the rules; this drives the two buttons on a
Plan card the way they are used: the chip shows the customer the title
suggests, its panel lists that customer's open tasks and files the event under
another one, the chip is redrawn in place, and the stopwatch then opens the
Log time dialog with the meeting's title and length and THAT customer's
project selected.
"""
import threading
import unittest
from datetime import datetime, time
from unittest import mock
from zoneinfo import ZoneInfo

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover, no playwright / mongoengine installed
    sync_playwright = None

BERLIN = ZoneInfo("Europe/Berlin")


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class EventCustomerBrowserTests(unittest.TestCase):

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
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:  # no Chromium downloaded
            cls.server.shutdown()
            raise unittest.SkipTest(f"no Chromium for Playwright: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        with cls.app.app_context():
            cls._wipe()
        disconnect(alias="default")

    @staticmethod
    def _wipe():
        from application.models.calendar import EventCustomer
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        for doc in (EventCustomer, TimeBucket, TimeTrackingProject, Customer,
                    Team, User):
            doc.objects.delete()
        Item.all_objects.delete()

    def setUp(self):
        from application.models.calendar import ExternalCalendar
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import Customer, TimeTrackingProject
        from application.models.user import User
        from application.services.calendar_sync import CalendarEvent
        today = datetime.now(BERLIN).date()
        with self.app.app_context():
            self._wipe()
            user = User(username="evc-browser", email="evc@example.com",
                        level="team_admin", timezone="Europe/Berlin",
                        plan_layout="timeline", plan_day_start=0, plan_day_end=24,
                        calendars=[ExternalCalendar(name="Work", provider="ical",
                                                    show_in_today=True)])
            user.set_password("pw-evc-test")
            user.save()
            team = Team(name="T", admin=user, admins=[user]).save()
            user.team = team
            acme = Customer(team=team, name="Acme GmbH").save()
            self.globex = Customer(team=team, name="Globex").save()
            TimeTrackingProject(team=team, customer=acme, name="Support").save()
            self.globex_web = TimeTrackingProject(team=team, customer=self.globex,
                                                  name="Website").save()
            user.tt_last_project = ""
            user.save()
            self.user = user
            folder = Item(owner=user, kind="project", title="Acme work",
                          customer=acme).save()
            Item(owner=user, kind="task", title="Angebot Acme", parent=folder,
                 status="next").save()
        self.events = [CalendarEvent(
            uid="weekly-1#20260101T090000Z", title="Acme Weekly",
            start=datetime.combine(today, time(10, 0)).replace(tzinfo=BERLIN),
            end=datetime.combine(today, time(11, 30)).replace(tzinfo=BERLIN))]
        self.patch = mock.patch("application.services.calendar_sync.fetch_events",
                                return_value=self.events)
        self.patch.start()
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 1400}, timezone_id="Europe/Berlin")
        self.context.add_cookies([{"name": "tz", "value": "Europe/Berlin",
                                   "url": self.base}])
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(f"{self.base}/auth/login")
        self.page.fill('input[name="email"]', "evc@example.com")
        self.page.fill('input[name="password"]', "pw-evc-test")
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")
        self.page.goto(f"{self.base}/tasks/today?view=plan&playout=timeline")
        self.page.wait_for_load_state("networkidle")

    def tearDown(self):
        self.context.close()
        self.patch.stop()

    def test_file_the_event_then_log_its_time(self):
        page = self.page
        card = page.locator(".plan-card-event", has_text="Acme Weekly")
        chip = card.locator(".event-cust-btn")
        self.assertEqual(chip.inner_text().strip(), "Acme GmbH?")
        self.assertIn("is-suggested", chip.get_attribute("class"))

        # The panel: the suggested customer's open task, then file it under
        # Globex instead — typed into the search, Enter takes the first row.
        chip.click()
        panel = page.locator(".event-cust-panel")
        panel.locator(".enp-item", has_text="Angebot Acme").wait_for()
        panel.locator(".ecp-search").fill("glob")
        panel.locator(".ecp-search").press("Enter")
        panel.wait_for(state="detached")
        self.assertEqual(chip.inner_text().strip(), "Globex")
        self.assertNotIn("is-suggested", chip.get_attribute("class"))
        with self.app.app_context():
            from application.models.calendar import EventCustomer
            row = EventCustomer.objects(owner=self.user.pk).first()
            self.assertEqual(row.uid, "weekly-1")
            self.assertEqual(row.customer.pk, self.globex.pk)

        # The stopwatch: title, length and Globex's project in the dialog.
        card.locator(".event-log-btn").click()
        dlg = page.locator("#quickLogModal")
        dlg.locator(".time-add-note").wait_for()
        self.assertEqual(dlg.locator(".time-add-note").input_value(), "Acme Weekly")
        self.assertEqual(dlg.locator(".time-add-duration").input_value(), "1h30m")
        self.assertEqual(dlg.locator(".time-add-start").input_value(), "10:00")
        self.assertEqual(dlg.locator(".time-add-end").input_value(), "11:30")
        self.assertEqual(dlg.locator(".time-add-project").input_value(),
                         str(self.globex_web.pk))

        # And it books. Bootstrap ignores hide() while the dialog is still
        # fading in, so the test waits that out the way a person does.
        page.wait_for_function(
            "() => !bootstrap.Modal.getInstance("
            "document.getElementById('quickLogModal'))._isTransitioning")
        dlg.locator(".time-add-note").press("Enter")
        dlg.wait_for(state="hidden")
        with self.app.app_context():
            from application.models.timetracking import TimeBucket
            entries = [e for b in TimeBucket.objects for e in b.entries]
        self.assertEqual([(e.minutes, e.note) for e in entries], [(90, "Acme Weekly")])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
