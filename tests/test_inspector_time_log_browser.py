"""Logging time from the task inspector, in a real browser — and on the server.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); the browser half also needs Playwright with
Chromium (``.venv``). Each half skips without what it needs.

The bug this pins down: "Zeit-Tracking im Inspector speichert die Zeiten nicht
mehr." Enter in the time section's duration field did two things at once: it
booked the entry (time-entries.js) AND saved the task (the inspector form's
Enter-to-save), and the task save re-rendered the whole panel. Whenever that
render ran before the booking was written, it replaced the time section with
one that did not have the new entry — the hours looked lost, and the booking's
own answer was applied to a section no longer on the page. The browser test
slows the booking down so that ordering is certain, not a coin flip.
"""
import threading
import time
import unittest

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    disconnect = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - no playwright installed
    sync_playwright = None


def _make_app():
    from application import create_app
    disconnect(alias="default")
    try:
        app = create_app("application.config.TestConfig")
        with app.app_context():
            from application.models.user import User
            User.objects.first()  # forces a real round trip
    except (ServerSelectionTimeoutError, PyMongoError) as exc:
        raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")
    return app


def _wipe():
    from application.models.item import Item
    from application.models.team import Team
    from application.models.timetracking import (
        Customer, TimeBucket, TimeTrackingProject)
    from application.models.user import User
    for doc in (TimeBucket, TimeTrackingProject, Customer, Team, User):
        doc.objects.delete()
    Item.all_objects.delete()


def _seed():
    """A team admin, a billing project, a project bound to it, one task."""
    from application.models.item import Item
    from application.models.team import Team
    from application.models.timetracking import Customer, TimeTrackingProject
    from application.models.user import User
    _wipe()
    user = User(username="insp-time", email="insp-time@example.com",
                level="team_admin")
    user.set_password("pw-insp-time")
    user.save()
    team = Team(name="T", admin=user, admins=[user]).save()
    user.team = team
    user.save()
    acme = Customer(team=team, name="Acme").save()
    support = TimeTrackingProject(team=team, customer=acme, name="Support").save()
    project = Item(owner=user, kind="project", title="P",
                   project_type="parallel", time_project=support).save()
    task = Item(owner=user, kind="task", title="Do it", parent=project).save()
    return user, support, project, task


def _active_entries():
    from application.models.timetracking import TimeBucket
    return [e for b in TimeBucket.objects for e in b.entries
            if e.status == "active"]


@unittest.skipIf(disconnect is None, "mongoengine not installed")
class InspectorTimeLogServerTests(unittest.TestCase):
    """The endpoint the inspector's time section posts to books the entry."""

    @classmethod
    def setUpClass(cls):
        cls.app = _make_app()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            _wipe()
        disconnect(alias="default")

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.user, self.support, _project, self.task = _seed()
        self.client = self.app.test_client()
        self.client.post("/auth/login", data={
            "email": "insp-time@example.com", "password": "pw-insp-time"})

    def tearDown(self):
        self.ctx.pop()

    def test_the_time_section_log_books_and_renders_the_entry(self):
        resp = self.client.post(
            f"/time/log/{self.task.pk}",
            data={"duration": "1h30m", "note": "from the inspector",
                  "project_id": str(self.support.pk)},
            headers={"X-Partial": "task"})
        self.assertEqual(resp.status_code, 200)
        entries = _active_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].minutes, 90)
        self.assertEqual(entries[0].source_item_id, str(self.task.pk))
        self.assertEqual(entries[0].note, "from the inspector")
        # The block the inspector swaps in shows it.
        self.assertIn("from the inspector", resp.get_json()["block"])


@unittest.skipIf(disconnect is None or sync_playwright is None,
                 "mongoengine / playwright not installed")
class InspectorTimeLogBrowserTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        cls.app = _make_app()

        # The booking is slower than the task save here — the ordering under
        # which the old double action showed a time section without the entry.
        @cls.app.before_request
        def _slow_booking():
            from flask import request
            if request.path.startswith("/time/log/"):
                time.sleep(0.6)

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
            _wipe()
        disconnect(alias="default")

    def setUp(self):
        with self.app.app_context():
            _user, _support, self.project, self.task = _seed()
        self.context = self.browser.new_context(
            viewport={"width": 1600, "height": 1000}, timezone_id="Europe/Berlin")
        self.context.add_cookies([{"name": "tz", "value": "Europe/Berlin",
                                   "url": self.base}])
        self.page = self.context.new_page()
        self.errors, self.posts = [], []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.on("request", lambda r: r.method == "POST"
                     and self.posts.append(r.url.split(self.base, 1)[-1]))
        self.page.goto(f"{self.base}/auth/login")
        self.page.fill('input[name="email"]', "insp-time@example.com")
        self.page.fill('input[name="password"]', "pw-insp-time")
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")
        self.page.goto(f"{self.base}/projects/{self.project.pk}")
        self.page.wait_for_load_state("networkidle")
        self.page.evaluate(
            f"window.bktodoOpenInspector('/tasks/{self.task.pk}', 'Do it')")
        self.page.wait_for_selector(".time-block .time-add-duration")
        self.posts.clear()

    def tearDown(self):
        self.context.close()

    def _shown_rows(self):
        return self.page.locator(".time-block .time-row").count()

    def _entries(self):
        with self.app.app_context():
            return _active_entries()

    def test_enter_in_the_duration_books_and_the_entry_stays_on_screen(self):
        self.page.fill(".time-block .time-add-duration", "20m")
        self.page.press(".time-block .time-add-duration", "Enter")
        self.page.wait_for_timeout(2000)
        self.assertEqual([e.minutes for e in self._entries()], [20])
        self.assertEqual(self._shown_rows(), 1)
        # Enter there is a booking, not also a task save.
        self.assertFalse(any(p.endswith("/edit") for p in self.posts), self.posts)
        self.assertEqual(self.errors, [])

    def test_enter_in_the_note_books_too(self):
        self.page.fill(".time-block .time-add-duration", "25m")
        self.page.fill(".time-block .time-add-note", "a note")
        self.page.press(".time-block .time-add-note", "Enter")
        self.page.wait_for_timeout(2000)
        self.assertEqual([e.note for e in self._entries()], ["a note"])
        self.assertEqual(self._shown_rows(), 1)

    def test_the_log_button_books_and_shows_it(self):
        self.page.fill(".time-block .time-add-duration", "15m")
        self.page.click(".time-block .time-add-btn")
        self.page.wait_for_timeout(2000)
        self.assertEqual([e.minutes for e in self._entries()], [15])
        self.assertEqual(self._shown_rows(), 1)

    def test_enter_in_a_task_field_still_saves_the_task(self):
        title = self.page.locator('form[data-inspector-form] input[name="title"]')
        title.fill("Renamed")
        title.press("Enter")
        self.page.wait_for_timeout(1500)
        self.assertTrue(any(p.endswith("/edit") for p in self.posts), self.posts)


if __name__ == "__main__":
    unittest.main()
