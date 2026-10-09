"""The ``d`` key on a task list, pressed in a real browser.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skipped
without either.

``d`` used to trash the row the mouse had LAST passed over, however long ago
and wherever the pointer was now, without asking, and the same key press also
marked the row under the pointer done through the bulk shortcuts. One stray
``d`` anywhere outside a text field sent a task into the trash unseen. The key
now acts only on the keyboard cursor or the row under the pointer, asks before
it trashes, and does nothing else.
"""
import threading
import unittest

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover, no playwright / mongoengine installed
    sync_playwright = None

URL = "/tasks/inbox"


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class DeleteKeyBrowserTests(unittest.TestCase):

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
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            user = User(username="dk-browser", email="dk@example.com",
                        level="premium", timezone="Europe/Berlin")
            user.set_password("pw-dk-test")
            user.save()
            self.task, self.other = (
                Item(owner=user, kind="task", title=title).save()
                for title in ("erste Aufgabe", "zweite Aufgabe"))
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 900}, timezone_id="Europe/Berlin")
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(f"{self.base}/auth/login")
        self.page.fill('input[name="email"]', "dk@example.com")
        self.page.fill('input[name="password"]', "pw-dk-test")
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")
        self.page.goto(self.base + URL)
        self.page.wait_for_load_state("networkidle")

    def tearDown(self):
        self.context.close()

    def _row(self, task):
        return self.page.locator(f'main .task-row[data-task-id="{task.pk}"]').first

    def _state(self, task):
        with self.app.app_context():
            t = self.Item.all_objects(pk=task.pk).first()
            return t.status, t.trashed_at is not None

    def _settle(self):
        self.page.wait_for_timeout(400)
        self.page.wait_for_load_state("networkidle")

    def test_d_after_the_pointer_left_the_row_does_nothing(self):
        page = self.page
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        self._row(self.task).hover()
        # The pointer moves on, off every row, and a stray d follows.
        page.mouse.move(5, 5)
        page.mouse.move(640, 880)
        page.keyboard.press("d")
        self._settle()
        self.assertEqual(self._state(self.task), ("open", False))
        self.assertEqual(self._state(self.other), ("open", False))
        self.assertEqual(dialogs, [])
        self.assertEqual(self.errors, [])

    def test_d_on_a_row_asks_and_a_no_leaves_it_alone(self):
        page = self.page
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        self._row(self.task).hover()
        page.keyboard.press("d")
        self._settle()
        self.assertEqual(len(dialogs), 1, dialogs)
        # Not trashed, and not marked done on the side either.
        self.assertEqual(self._state(self.task), ("open", False))
        self.assertEqual(self.errors, [])

    def test_d_on_a_row_trashes_it_after_a_yes(self):
        page = self.page
        page.on("dialog", lambda d: d.accept())
        self._row(self.task).hover()
        page.keyboard.press("d")
        self._settle()
        self.assertEqual(self._state(self.task), ("open", True))
        self.assertEqual(self._state(self.other), ("open", False))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
