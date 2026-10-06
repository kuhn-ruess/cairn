"""The `@person` and `+tag` pick list in the project sketch, in a real browser.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skipped
without either.

"Kann ich mit att auch personen direkt zuweisen, mit dropdown auswahl sowie
tags auch mit dropdown auswahl, vergleichbar quick add?" The sketch textarea
gets the Quick Add list (``token-complete.js``): `@` lists teammates, `+`
lists tags, ↓ and Enter take an entry, and an Enter with nothing highlighted
is still the sketch's own Enter (a new list line). Quick Add uses the same
module, so it is checked here as well.
"""
import threading
import unittest

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover (no playwright or mongoengine installed)
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class SketchPickListBrowserTests(unittest.TestCase):

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
        from application.models.team import Team
        from application.models.user import User
        for t in Team.objects(name="skb-team"):
            t.delete()
        for u in User.objects(username__in=("skb-bastian", "skb-sven")):
            Item.all_objects(owner=u).delete()
            u.delete()

    def setUp(self):
        from application.models.item import Item
        from application.models.tag import Tag
        from application.models.team import Team
        from application.models.user import User
        self.Item = Item
        with self.app.app_context():
            self._wipe()
            user = User(username="skb-bastian", email="skb@example.com",
                        level="premium",
                        tags=[Tag(name="Buchhaltung", color="#6c757d")])
            user.set_password("pw-skb-test")
            user.save()
            self.mate = User(username="skb-sven", email="skb-sven@example.com",
                             display_name="Sven", password_hash="x").save()
            self.mate.team = Team(name="skb-team", admin=user).save()
            self.mate.save()
            self.project = Item(owner=user, kind="project", title="Relaunch",
                                project_type="parallel").save()
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 1000}, timezone_id="Europe/Berlin")
        self.context.add_cookies([{"name": "tz", "value": "Europe/Berlin",
                                   "url": self.base}])
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(f"{self.base}/auth/login")
        self.page.fill('input[name="email"]', "skb@example.com")
        self.page.fill('input[name="password"]', "pw-skb-test")
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")

    def tearDown(self):
        self.context.close()

    def _pick(self, field):
        """↓ then Enter on the open list, once it has an entry."""
        self.page.locator(".qa-assignee-ac:not(.d-none) .qa-ac-item").first.wait_for()
        field.press("ArrowDown")
        field.press("Enter")

    def test_sketch_picks_a_person_and_a_tag(self):
        page = self.page
        page.goto(f"{self.base}/projects/{self.project.pk}?view=sketch")
        page.wait_for_load_state("networkidle")
        ta = page.locator("#project-sketch")
        ta.click()
        ta.press_sequentially("- Angebot @skb-s")
        box = page.locator(".qa-assignee-ac:not(.d-none)")
        box.locator(".qa-ac-item").first.wait_for()
        # Under the line being typed, not under the whole twenty-line field.
        tb, bb = ta.bounding_box(), box.bounding_box()
        self.assertLess(bb["y"], tb["y"] + 80)
        self.assertGreater(bb["y"], tb["y"])
        self._pick(ta)
        ta.press_sequentially("+Buch")
        self._pick(ta)
        self.assertEqual(ta.input_value(), "- Angebot @skb-sven +Buchhaltung ")
        # Nothing highlighted: Enter is the sketch's own again (a new line).
        ta.press_sequentially("@skb")
        page.locator(".qa-assignee-ac:not(.d-none) .qa-ac-item").first.wait_for()
        ta.press("Enter")
        self.assertEqual(ta.input_value(),
                         "- Angebot @skb-sven +Buchhaltung @skb\n- ")
        ta.fill("- Angebot @skb-sven +Buchhaltung")
        ta.press("Control+Enter")
        page.wait_for_url(f"**/projects/{self.project.pk}")
        with self.app.app_context():
            task = self.Item.objects(parent=self.project, title="Angebot").first()
            self.assertIsNotNone(task)
            self.assertEqual(task.assigned_to.pk, self.mate.pk)
            self.assertEqual(task.tags, ["Buchhaltung"])
        self.assertEqual(self.errors, [])

    def test_quick_add_still_picks_a_person(self):
        page = self.page
        page.goto(f"{self.base}/projects/{self.project.pk}")
        page.wait_for_load_state("networkidle")
        page.keyboard.press("q")
        field = page.locator("#quickAddText")
        field.wait_for(state="visible")
        field.press_sequentially("Rechnung @skb-s")
        self._pick(field)
        self.assertEqual(field.input_value(), "Rechnung @skb-sven ")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
