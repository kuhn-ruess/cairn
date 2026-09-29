"""Save task in the inspector also posts the comment box — comment AND time.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skips
without them.

"Save task just should also save the comment and the time if entered." The
save used to post a typed comment first but drop the time beside it, and a
time with no comment was never booked at all. Now the save posts whatever the
box holds, waits for it, and only then saves (and re-renders) the task — the
comment endpoint is slowed down here so a save that didn't wait would lose.
A clock-mode billing project gets end = the save, start = end − duration.
"""
import threading
import time
import unittest
from datetime import datetime, timedelta

try:
    from mongoengine import disconnect
except ImportError:  # pragma: no cover - no mongoengine installed
    disconnect = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - no playwright installed
    sync_playwright = None

if disconnect is not None:
    from tests.test_inspector_time_log_browser import (
        _active_entries, _make_app, _seed, _wipe)


@unittest.skipIf(disconnect is None or sync_playwright is None,
                 "mongoengine / playwright not installed")
class InspectorSaveCommentTimeBrowserTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        cls.app = _make_app()

        @cls.app.before_request
        def _slow_comment():
            from flask import request
            if request.path.endswith("/comment"):
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

    def setUp(self, clock=False):
        with self.app.app_context():
            self.user, self.support, self.project, self.task = _seed()
            if clock:
                self.support.time_mode = "clock"
                self.support.save()
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
        self._open()
        self.posts.clear()

    def tearDown(self):
        self.context.close()

    def _open(self):
        self.page.evaluate(
            f"window.bktodoOpenInspector('/tasks/{self.task.pk}/edit', 'Do it')")
        self.page.wait_for_selector('[data-inspector-comment-form] input[name="log_duration"]')
        # The panel focuses the title 30ms after it renders; typing before that
        # would land half in the title.
        self.page.wait_for_timeout(300)

    def _box(self, sel):
        return self.page.locator(f"[data-inspector-comment-form] {sel}")

    def _save(self):
        self.page.get_by_role("button", name="Save task").click()

    def _settle(self, ms=2500):
        self.page.wait_for_timeout(ms)

    def _entries(self):
        with self.app.app_context():
            return _active_entries()

    def _comments(self):
        with self.app.app_context():
            from application.models.item import Item
            return [c.body for c in Item.objects(pk=self.task.pk).first().comments]

    def _saved_after_comment(self):
        """The task save went out only after the comment post (sequenced)."""
        comment = [i for i, p in enumerate(self.posts) if p.endswith("/comment")]
        save = [i for i, p in enumerate(self.posts) if p.endswith("/edit")]
        self.assertTrue(comment and save, self.posts)
        self.assertLess(comment[-1], save[0], self.posts)

    def test_save_posts_the_comment_and_books_the_time(self):
        self._box("textarea[name=comment_body]").fill("called them")
        self._box('input[name="log_duration"]').fill("40m")
        self._save()
        self._settle()
        self.assertEqual(self._comments(), ["called them"])
        [entry] = self._entries()
        self.assertEqual((entry.minutes, entry.note), (40, "called them"))
        self._saved_after_comment()
        # The re-rendered panel shows both and has empty fields.
        self.assertEqual(self.page.locator(".time-block .time-row").count(), 1)
        self.assertIn("called them", self.page.locator(".comments-list").inner_text())
        self.assertEqual(self._box('input[name="log_duration"]').input_value(), "")
        self.assertEqual(self.errors, [])

    def test_save_books_a_time_without_a_comment(self):
        self._box('input[name="log_duration"]').fill("25m")
        self._save()
        self._settle()
        self.assertEqual([(e.minutes, e.note) for e in self._entries()], [(25, "")])
        self.assertEqual(self._comments(), [])
        self._saved_after_comment()

    def test_enter_in_the_time_field_books_a_time_without_a_comment(self):
        self._box('input[name="log_duration"]').fill("10m")
        self._box('input[name="log_duration"]').press("Enter")
        self._settle()
        self.assertEqual([e.minutes for e in self._entries()], [10])
        self.assertEqual(self._comments(), [])
        self.assertEqual(self._box('input[name="log_duration"]').input_value(), "")

    def test_duration_project_gets_no_clock_times(self):
        self._box('input[name="log_duration"]').fill("30m")
        self._save()
        self._settle()
        [entry] = self._entries()
        self.assertIsNone(entry.start_time)
        self.assertIsNone(entry.end_time)

    def test_post_then_save_and_double_save_book_once(self):
        self._box("textarea[name=comment_body]").fill("once")
        self._box('input[name="log_duration"]').fill("20m")
        # Post, and while it is still in flight, Save twice.
        self._box(".comment-add-btn").click()
        self._save()
        self.page.evaluate(
            "document.querySelector('form[data-inspector-form]').requestSubmit()")
        self._settle(3500)
        self.assertEqual([e.minutes for e in self._entries()], [20])
        self.assertEqual(self._comments(), ["once"])
        self.assertEqual(self.errors, [])

    def test_a_refused_time_keeps_the_input_and_does_not_save(self):
        self._box("textarea[name=comment_body]").fill("keep me")
        self._box('input[name="log_duration"]').fill("a while")
        self._save()
        self._settle()
        self.assertEqual(self._entries(), [])
        self.assertEqual(self._comments(), [])
        self.assertFalse(any(p.endswith("/edit") for p in self.posts), self.posts)
        self.assertEqual(self._box("textarea[name=comment_body]").input_value(), "keep me")
        self.assertEqual(self._box('input[name="log_duration"]').input_value(), "a while")
        self.assertTrue(self._box(".comment-post-error").is_visible())
        self.assertIn("Task not saved", self.page.locator(".alert-danger").first.inner_text())


class InspectorSaveClockProjectBrowserTests(InspectorSaveCommentTimeBrowserTests):
    """Same page, the billing project books by the clock."""

    def setUp(self):
        super().setUp(clock=True)

    # Not a duration project here — its own test below says what happens.
    test_duration_project_gets_no_clock_times = None

    def test_clock_project_ends_at_the_save(self):
        from zoneinfo import ZoneInfo
        self._box('input[name="log_duration"]').fill("1h30m")
        zone = ZoneInfo("Europe/Berlin")
        before = datetime.now(zone).replace(second=0, microsecond=0)
        self._save()
        self._settle()
        after = datetime.now(zone)
        [entry] = self._entries()
        self.assertEqual(entry.minutes, 90)
        ends, t = set(), before
        while t <= after:
            ends.add(t.strftime("%H:%M"))
            t += timedelta(minutes=1)
        self.assertIn(entry.end_time, ends)
        end = datetime.strptime(entry.end_time, "%H:%M")
        self.assertEqual((end - timedelta(minutes=90)).strftime("%H:%M"),
                         entry.start_time)


if __name__ == "__main__":
    unittest.main()
