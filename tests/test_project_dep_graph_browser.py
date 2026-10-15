"""The project header's dependency graph panel, in a real browser.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skipped
without either.

The toggle in the header opens the panel, which fetches and draws the
project's graph with the same renderer as the full graph; a dependency on a
task outside ends at a dashed placeholder that links to the full graph. The
panel stays open across a background refresh of ``<main>``.
"""
import os
import threading
import unittest

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover (no playwright or mongoengine installed)
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class ProjectDepGraphBrowserTests(unittest.TestCase):

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
            cls._wipe()
        disconnect(alias="default")

    @staticmethod
    def _wipe():
        from application.models.item import Item
        from application.models.user import User
        for u in User.objects(username="pdg-user"):
            Item.all_objects(owner=u).delete()
            u.delete()

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        with self.app.app_context():
            self._wipe()
            user = User(username="pdg-user", email="pdg@example.com",
                        level="premium")
            user.set_password("pw-pdg-test")
            user.save()
            self.project = Item(owner=user, kind="project", title="Relaunch",
                                project_type="parallel").save()
            other = Item(owner=user, kind="project", title="Elsewhere",
                         project_type="parallel").save()
            outside = Item(owner=user, kind="task", title="Outside",
                           parent=other).save()
            base = Item(owner=user, kind="task", title="Base",
                        parent=self.project, depends_on=[outside]).save()
            Item(owner=user, kind="task", title="Top", parent=self.project,
                 depends_on=[base]).save()
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 1000})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(f"{self.base}/auth/login")
        self.page.fill('input[name="email"]', "pdg@example.com")
        self.page.fill('input[name="password"]', "pw-pdg-test")
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")

    def tearDown(self):
        self.context.close()

    def test_toggle_draws_the_graph_with_a_placeholder(self):
        page = self.page
        page.goto(f"{self.base}/projects/{self.project.pk}")
        page.wait_for_load_state("networkidle")
        panel = page.locator("#project-dep-graph")
        self.assertTrue(panel.is_hidden())
        page.click("[data-dep-graph-toggle]")
        panel.locator("svg .dep-node").first.wait_for()
        titles = panel.locator("svg .dep-node text").all_text_contents()
        self.assertIn("Base", titles)
        self.assertIn("Top", titles)
        self.assertNotIn("Outside", titles)
        placeholder = panel.locator('svg a.dep-node[href="/tasks/graph"]')
        self.assertEqual(placeholder.count(), 1)
        self.assertEqual(placeholder.locator("rect").get_attribute(
            "stroke-dasharray"), "5 4")
        if os.environ.get("PDG_SCREENSHOT"):
            page.screenshot(path=os.environ["PDG_SCREENSHOT"])

        # A background refresh replaces <main>; the panel comes back open.
        page.evaluate("window.bktodoRefreshMain()")
        page.wait_for_load_state("networkidle")
        panel.locator("svg .dep-node").first.wait_for()
        self.assertTrue(panel.is_visible())

        # Closing it is remembered on the next visit.
        page.click("[data-dep-graph-toggle]")
        self.assertTrue(panel.is_hidden())
        page.reload()
        page.wait_for_load_state("networkidle")
        self.assertTrue(page.locator("#project-dep-graph").is_hidden())
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
