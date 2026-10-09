"""Changing the task link of a booked entry on the day sheet, in a browser.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skips
without either.

The bug: an entry that was linked to the wrong task showed the link on the day
sheet, and its editor had no way to take it back. The editor now carries the
link: × removes it, the search box moves it onto another task, Save sends it.
"""
import threading
import unittest
from datetime import datetime, timezone

from tests.test_inspector_time_log_browser import (
    _active_entries, _make_app, _seed, _wipe, disconnect, sync_playwright)


@unittest.skipIf(disconnect is None or sync_playwright is None,
                 "mongoengine / playwright not installed")
class DaySheetRelinkBrowserTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        cls.app = _make_app()
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
        from application.models.item import Item
        from application.services.timetracking import add_entry
        with self.app.app_context():
            user, support, project, self.task = _seed()
            self.other = Item(owner=user, kind="task", title="Other work",
                              parent=project).save()
            add_entry(project=support, user=user, minutes=30, note="meeting",
                      spent_on=datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
                      source_item=self.task)
        self.context = self.browser.new_context(
            viewport={"width": 1600, "height": 1000}, timezone_id="Europe/Berlin")
        self.context.add_cookies([{"name": "tz", "value": "Europe/Berlin",
                                   "url": self.base}])
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(f"{self.base}/auth/login")
        self.page.fill('input[name="email"]', "insp-time@example.com")
        self.page.fill('input[name="password"]', "pw-insp-time")
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")
        self.page.goto(f"{self.base}/time/day?d=2026-10-05")
        self.page.wait_for_load_state("networkidle")
        self.page.click(".tt-edit-toggle")

    def tearDown(self):
        self.context.close()

    def _source(self):
        with self.app.app_context():
            [entry] = _active_entries()
            return entry.source_item_id

    def test_unlink_then_save_removes_the_link(self):
        self.assertEqual(self._source(), str(self.task.pk))
        self.page.click(".tt-edit-unlink")
        self.page.click(".tt-edit-save")
        self.page.wait_for_timeout(1500)
        self.assertIsNone(self._source())
        self.assertEqual(self.page.locator(".tt-row-meta .bi-link-45deg").count(), 0)
        self.assertEqual(self.errors, [])

    def test_search_picks_another_task_and_enter_does_not_save_yet(self):
        self.page.fill(".tt-edit-link-search", "Other")
        self.page.wait_for_selector(".tt-edit-link-pick")
        self.page.press(".tt-edit-link-search", "Enter")
        self.page.wait_for_timeout(500)
        # Picked, not yet saved.
        self.assertEqual(self._source(), str(self.task.pk))
        self.assertEqual(self.page.locator(".tt-edit-link-text").inner_text(),
                         "Other work")
        self.page.click(".tt-edit-save")
        self.page.wait_for_timeout(1500)
        self.assertEqual(self._source(), str(self.other.pk))
        self.assertIn("Other work", self.page.locator(".tt-row-meta").inner_text())
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
