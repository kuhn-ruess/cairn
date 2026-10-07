"""The list views' refresh button, clicked in a real browser.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skipped
without either.

``test_list_refresh_button.py`` checks that the button is on the views and
what the refresh request answers; this checks the part only a browser has:
that a click swaps the list in place (no navigation, the page's own state
survives), that a task completed and one taken out of the plan elsewhere are
gone afterwards, and that it costs exactly ONE request for the page (the
project sidebar, which refetches the page after a save, sits this one out).
"""
import threading
import unittest
from datetime import datetime, time
from zoneinfo import ZoneInfo

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover, no playwright / mongoengine installed
    sync_playwright = None

BERLIN = ZoneInfo("Europe/Berlin")
URL = "/tasks/today?view=list&days=1&group=none"


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class ListRefreshBrowserTests(unittest.TestCase):

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
        with cls.app.app_context():
            from application.models.item import Item
            from application.models.user import User
            Item.all_objects.delete()
            User.objects.delete()
        disconnect(alias="default")

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        self.Item = Item
        planned = datetime.combine(datetime.now(BERLIN).date(),
                                   time(0, 0)).replace(tzinfo=BERLIN)
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            user = User(username="rf-browser", email="rf@example.com",
                        level="premium", timezone="Europe/Berlin")
            user.set_password("pw-rf-test")
            user.save()
            self.user = user
            project = Item(owner=user, kind="project", title="Webshop",
                           project_type="parallel").save()
            self.keep, self.unplan, self.done = (
                Item(owner=user, kind="task", title=title, parent=project,
                     plan_date=planned).save()
                for title in ("bleibt", "aus dem Plan", "erledigt"))
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 900}, timezone_id="Europe/Berlin")
        self.context.add_cookies([{"name": "tz", "value": "Europe/Berlin",
                                   "url": self.base}])
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(f"{self.base}/auth/login")
        self.page.fill('input[name="email"]', "rf@example.com")
        self.page.fill('input[name="password"]', "pw-rf-test")
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")
        self.page.goto(self.base + URL)
        self.page.wait_for_load_state("networkidle")

    def tearDown(self):
        self.context.close()

    def _row(self, task):
        return self.page.locator(f'main [data-task-id="{task.pk}"]')

    def test_the_button_swaps_the_list_without_a_reload(self):
        page = self.page
        for t in (self.keep, self.unplan, self.done):
            self.assertGreater(self._row(t).count(), 0, t.title)

        # Somebody else (the API, another tab) completes one and unplans one.
        with self.app.app_context():
            t = self.Item.objects(pk=self.unplan.pk).first()
            t.plan_date = None
            t.save()
            self.Item.objects(pk=self.done.pk).first().complete(by_user=self.user)

        # A marker on window survives an in-place swap but not a navigation.
        page.evaluate("window.__noReload = true")
        page_gets = []
        page.on("request", lambda r: page_gets.append(r.url)
                if r.method == "GET" and "/tasks/today" in r.url else None)

        page.locator("main [data-refresh-list]").click()
        self._row(self.done).first.wait_for(state="detached")
        self._row(self.unplan).first.wait_for(state="detached")
        page.wait_for_load_state("networkidle")

        self.assertGreater(self._row(self.keep).count(), 0)
        self.assertTrue(page.evaluate("window.__noReload === true"))
        self.assertEqual(len(page_gets), 1, page_gets)
        # The fresh button is not stuck spinning.
        btn = page.locator("main [data-refresh-list]")
        self.assertFalse(btn.is_disabled())
        self.assertNotIn("spinning", btn.get_attribute("class"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
