"""The inspector's date clear buttons in a real browser (WebKit and Chromium).

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright (``.venv``); each engine that is not
downloaded is skipped, and so is the whole file without either.

The report: "Auf Safari: Inspektor das kreuz neben Plandate loescht das
Plandate nicht." The value was cleared and saved as cleared, but Safari draws
an EMPTY datetime-local field with today's date and the current time, in the
same colour as a real value. So after the cross the plan date looked as if it
had only moved to today, and after a reload a task without a plan date looked
planned for today. An empty field in the inspector now carries ``dt-empty``,
and app.css draws its segments transparent.
"""
import threading
import unittest
from datetime import datetime, timezone

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover (no playwright or mongoengine installed)
    sync_playwright = None

EMAIL = "dateclear@example.com"
PASSWORD = "pw-date-clear"
WHEN = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class InspectorDateClearBrowserTests(unittest.TestCase):

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
        from application.models.item import Item
        from application.models.user import User
        for user in User.objects(email=EMAIL):
            Item.all_objects(owner=user).delete()
            user.delete()

    def _fresh_task(self):
        from application.models.item import Item
        from application.models.user import User
        with self.app.app_context():
            self._wipe()
            user = User(username="dateclear", email=EMAIL,
                        timezone="Europe/Berlin")
            user.set_password(PASSWORD)
            user.save()
            task = Item(kind="task", title="Plan me", owner=user,
                        plan_date=WHEN, due_date=WHEN, defer_date=WHEN).save()
            return str(task.pk)

    def _task(self, task_id):
        from application.models.item import Item
        with self.app.app_context():
            return Item.objects(pk=task_id).first()

    def _detail(self, browser, task_id):
        page = browser.new_page(locale="de-DE")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(self.base + "/auth/login")
        page.fill("input[name=email]", EMAIL)
        page.fill("input[name=password]", PASSWORD)
        page.click("input[type=submit]")
        page.wait_for_load_state("networkidle")
        page.goto(self.base + f"/tasks/{task_id}")
        page.wait_for_load_state("networkidle")
        return page, errors

    def _save(self, page):
        with page.expect_response(lambda r: r.request.method == "POST"):
            page.locator("button", has_text="Save task").first.click()
        page.wait_for_load_state("networkidle")

    def _blank(self, page, field):
        """Is the field drawn empty, i.e. does it carry ``dt-empty``?"""
        return page.evaluate(
            "id => document.getElementById(id).classList.contains('dt-empty')",
            field)

    def test_cross_clears_the_date_and_save_keeps_it_cleared(self):
        for name, browser in self.browsers.items():
            for field in ("plan_date", "due_date", "defer_date"):
                with self.subTest(browser=name, field=field):
                    task_id = self._fresh_task()
                    page, errors = self._detail(browser, task_id)
                    page.locator(
                        f'[data-quick-date="{field}"][data-set="clear"]').click()
                    self.assertEqual(page.locator(f"#{field}").input_value(), "")
                    self.assertTrue(self._blank(page, field))
                    self._save(page)
                    self.assertIsNone(getattr(self._task(task_id), field))
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    self.assertEqual(page.locator(f"#{field}").input_value(), "")
                    self.assertTrue(self._blank(page, field))
                    # A value set again is drawn again.
                    page.locator(
                        f'[data-quick-date="{field}"][data-set="tomorrow"]').click()
                    self.assertNotEqual(page.locator(f"#{field}").input_value(), "")
                    self.assertFalse(self._blank(page, field))
                    self.assertEqual(errors, [])
                    page.close()

    def test_quick_plan_sets_next_and_its_done_ends_only_the_step(self):
        """Quick plan puts the task into Next (no date), and a full Save after
        it keeps that; Done ticks off the step with a log comment."""
        for name, browser in self.browsers.items():
            with self.subTest(browser=name):
                task_id = self._fresh_task()
                page, errors = self._detail(browser, task_id)
                done_btn = page.locator('[data-quick-plan="done"]')
                self.assertTrue(done_btn.is_hidden())
                page.fill("[data-quick-plan-note]", "Preis nachrechnen")
                with page.expect_response(lambda r: "/quick-plan" in r.url):
                    page.press("[data-quick-plan-note]", "Enter")
                page.wait_for_function(
                    "document.querySelector('select[name=status]').value === 'next'")
                task = self._task(task_id)
                self.assertEqual(task.status, "next")
                self.assertEqual(task.plan_note, "Preis nachrechnen")
                self.assertEqual(task.plan_date.replace(tzinfo=timezone.utc), WHEN)
                self.assertTrue(done_btn.is_visible())
                self._save(page)
                self.assertEqual(self._task(task_id).status, "next")
                page.goto(self.base + f"/tasks/{task_id}")
                page.wait_for_load_state("networkidle")
                with page.expect_response(lambda r: "/quick-plan" in r.url):
                    page.locator('[data-quick-plan="done"]').click()
                page.wait_for_function(
                    "document.querySelector('[data-quick-plan-note]').value === ''")
                task = self._task(task_id)
                self.assertEqual(task.plan_note, "")
                self.assertEqual(task.status, "open")
                self.assertIsNone(task.completed_at)
                self.assertEqual(task.comments[-1].body,
                                 "Quick plan done: Preis nachrechnen")
                self.assertEqual(errors, [])
                page.close()

if __name__ == "__main__":
    unittest.main()
