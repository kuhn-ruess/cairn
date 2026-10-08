"""The inspector's "Refresh from Zammad" button, clicked in a real browser.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skipped
without either. Zammad itself is replaced by the fixture from
``test_zammad_refresh``, so nothing leaves the machine.

The report: after a refresh from the inspector panel on a list view, a whole
Cairn page loaded into the panel. The button re-fetched the panel from
``/tasks/<id>``, which is the full detail page; the fragment lives at
``/tasks/<id>/edit``. This pins down that the panel holds the inspector
fragment afterwards, with the new title, and no second app shell.
"""
import threading
import unittest

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover, no playwright / mongoengine installed
    sync_playwright = None

from tests.test_zammad_refresh import TICKET, _fixture_request

EMAIL = "zrefresh-browser@example.com"
PASSWORD = "pw-zammad-refresh"


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class ZammadRefreshBrowserTests(unittest.TestCase):

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
        from application.models.item import Item
        from application.models.user import User
        for user in User.objects(email=EMAIL):
            Item.all_objects(owner=user).delete()
            user.delete()

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        from application.services.crypto import encrypt_for_user
        from application.services import zammad_import as zi
        original = zi._request
        zi._request = _fixture_request()
        self.addCleanup(lambda: setattr(zi, "_request", original))
        with self.app.app_context():
            self._wipe()
            user = User(username="zrefreshbrowser", email=EMAIL,
                        level="premium", zammad_enabled=True,
                        zammad_url="https://zammad.test.invalid",
                        zammad_import_time=False)
            user.set_password(PASSWORD)
            user.save()
            user.zammad_token = encrypt_for_user(user, "tok-123")
            user.save()
            task = Item(owner=user, kind="task", title="Old title",
                        source="zammad", external_id="zammad:4711",
                        external_ref="70001", status="open").save()
            self.task_id = str(task.pk)

    def test_refresh_in_the_panel_keeps_the_inspector_fragment(self):
        page = self.browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(self.base + "/auth/login")
        page.fill("input[name=email]", EMAIL)
        page.fill("input[name=password]", PASSWORD)
        page.click("input[type=submit]")
        page.wait_for_load_state("networkidle")
        page.goto(self.base + "/tasks/inbox")
        page.wait_for_load_state("networkidle")

        page.locator(".task-inspector-btn").first.click()
        panel = page.locator("#inspector-panel .offcanvas-body")
        panel.locator("[data-zammad-refresh]").wait_for()

        with page.expect_response(lambda r: "/zammad-refresh" in r.url) as r:
            panel.locator("[data-zammad-refresh]").click()
        self.assertTrue(r.value.json()["ok"])
        # The panel is re-fetched after a short pause. Wait until it shows the
        # title Zammad has now, or until a page shell turns up in it.
        page.wait_for_function(
            """t => {
                const b = document.querySelector('#inspector-panel .offcanvas-body');
                if (!b) return false;
                if (b.querySelector('main')) return true;
                const i = b.querySelector('input[name=title]');
                return i && i.value === t;
            }""", arg=TICKET["title"], timeout=10000)
        page.wait_for_load_state("networkidle")

        self.assertEqual(panel.locator("main").count(), 0,
                         "a whole page landed inside the inspector panel")
        self.assertEqual(panel.locator("nav.navbar, #inspector-panel").count(), 0)
        self.assertEqual(page.locator("#inspector-panel").count(), 1)
        self.assertEqual(panel.locator("input[name=title]").input_value(),
                         TICKET["title"])
        self.assertEqual(errors, [])
        page.close()


if __name__ == "__main__":
    unittest.main()
