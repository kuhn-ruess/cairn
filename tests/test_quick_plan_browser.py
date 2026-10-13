"""Quick plan in Today, in a real browser (WebKit and Chromium).

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright (``.venv``); each engine that is not
downloaded is skipped, and so is the whole file without either.

A task with a quick plan step sits in Today's Next actions under the step's
name. Its check ends the step, not the task: the task stays open, the row
leaves the rail because the task left Next, and a comment logs the step.
"""
import threading
import unittest

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover (no playwright or mongoengine installed)
    sync_playwright = None

EMAIL = "quickplan-browser@example.com"
PASSWORD = "pw-quick-plan"


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class QuickPlanBrowserTests(unittest.TestCase):

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
            user = User(username="quickplanb", email=EMAIL, timezone="UTC",
                        today_view_mode="list")
            user.set_password(PASSWORD)
            user.save()
            task = Item(kind="task", title="Angebot Website", owner=user,
                        status="next", plan_note="Preis nachrechnen",
                        plan_prev_status="open").save()
            return str(task.pk)

    def _task(self, task_id):
        from application.models.item import Item
        with self.app.app_context():
            return Item.objects(pk=task_id).first()

    def test_ticking_the_step_in_todays_next_rail_keeps_the_task(self):
        for name, browser in self.browsers.items():
            with self.subTest(browser=name):
                task_id = self._fresh_task()
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(self.base + "/auth/login")
                page.fill("input[name=email]", EMAIL)
                page.fill("input[name=password]", PASSWORD)
                page.click("input[type=submit]")
                page.wait_for_load_state("networkidle")
                page.goto(self.base + "/tasks/today?view=list")
                page.wait_for_load_state("networkidle")
                item = page.locator(f'#today-next [data-task-id="{task_id}"]')
                self.assertIn("Preis nachrechnen", item.inner_text())
                with page.expect_response(lambda r: "/quick-plan" in r.url):
                    item.locator(".plan-rail-check button").click()
                page.wait_for_function(
                    "id => !document.querySelector("
                    "'#today-next [data-task-id=\"' + id + '\"]')", arg=task_id)
                task = self._task(task_id)
                self.assertEqual(task.status, "open")
                self.assertIsNone(task.completed_at)
                self.assertEqual(task.plan_note, "")
                self.assertEqual(task.comments[-1].body,
                                 "Quick plan done: Preis nachrechnen")
                self.assertEqual(errors, [])
                page.close()


if __name__ == "__main__":
    unittest.main()
