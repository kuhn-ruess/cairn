"""Nesting a task by dragging it to the right, in a real browser.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skipped
without either.

The bug this pins down: "Der task bleibt bei einer langen Liste schon beim erst
besten hängen, der zwischen dem Quell- und dem Zieltask ist." A hand does not
drag straight down and THEN right — it drags diagonally towards the target. The
moment the pointer was 48px right of the grab the list froze with the dragged
row wherever the reorder had put it so far, and the target was read off the
DOM (the row above the dragged one), so it stayed on the first row it passed.
Once frozen, the list is still, which is exactly what makes the pointer
truthful again: the target now follows the row under the pointer.
"""
import threading
import unittest

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - no playwright / mongoengine installed
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class NestDragBrowserTests(unittest.TestCase):

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
            user = User(username="drag-test", email="drag@example.com",
                        level="premium")
            user.set_password("pw-drag-test")
            user.save()
            self.project = Item(owner=user, kind="project", title="Long list",
                                project_type="parallel").save()
            self.tasks = [
                Item(owner=user, kind="task", title=f"Task {i:02d}",
                     parent=self.project, sort_order=i).save()
                for i in range(12)
            ]
        # The base template reloads once to hand the server the browser's
        # timezone; with the cookie already set that reload cannot race the
        # login form.
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 1400}, timezone_id="Europe/Berlin")
        self.context.add_cookies([{"name": "tz", "value": "Europe/Berlin",
                                   "url": self.base}])
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(f"{self.base}/auth/login")
        self.page.fill('input[name="email"]', "drag@example.com")
        self.page.fill('input[name="password"]', "pw-drag-test")
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")
        self.page.goto(f"{self.base}/projects/{self.project.pk}")
        self.page.wait_for_load_state("networkidle")

    def tearDown(self):
        self.context.close()

    def _row(self, task):
        return self.page.locator(f'.task-row[data-task-id="{task.pk}"]').first

    def _parent_of(self, task):
        with self.app.app_context():
            return self.Item.all_objects.get(pk=task.pk).parent.pk

    def _drag_diagonally(self, source, target, steps=40):
        """Grab the handle and head for the target row the way a hand does:
        right and down at once, releasing while still over the target."""
        page = self.page
        src = self._row(source)
        src.hover()
        handle = src.locator(".task-handle")
        hb = handle.bounding_box()
        x0, y0 = hb["x"] + hb["width"] / 2, hb["y"] + hb["height"] / 2
        tb = self._row(target).bounding_box()
        x1, y1 = x0 + 120, tb["y"] + tb["height"] / 2
        page.mouse.move(x0, y0)
        page.mouse.down()
        for i in range(1, steps + 1):
            page.mouse.move(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
            page.wait_for_timeout(15)
        # Settle over the target, as a hand does before letting go.
        for _ in range(5):
            page.mouse.move(x1, y1)
            page.wait_for_timeout(30)
        highlighted = page.evaluate(
            "() => { const r = document.querySelector('.drop-merge-target');"
            " return r ? r.dataset.taskId : null; }")
        page.mouse.up()
        page.wait_for_timeout(600)
        return highlighted

    def test_a_diagonal_drag_nests_under_the_row_aimed_at_not_the_first_passed(self):
        source, target = self.tasks[1], self.tasks[9]
        highlighted = self._drag_diagonally(source, target)
        self.assertEqual(highlighted, str(target.pk))
        self.assertEqual(self._parent_of(source), target.pk)
        self.assertEqual(self.errors, [])

    def test_dragging_upwards_works_the_same_way(self):
        source, target = self.tasks[10], self.tasks[2]
        highlighted = self._drag_diagonally(source, target)
        self.assertEqual(highlighted, str(target.pk))
        self.assertEqual(self._parent_of(source), target.pk)

    def test_a_straight_vertical_drag_is_still_a_reorder(self):
        source = self.tasks[1]
        page = self.page
        src = self._row(source)
        src.hover()
        hb = src.locator(".task-handle").bounding_box()
        x0, y0 = hb["x"] + hb["width"] / 2, hb["y"] + hb["height"] / 2
        tb = self._row(self.tasks[6]).bounding_box()
        page.mouse.move(x0, y0)
        page.mouse.down()
        for i in range(1, 31):
            page.mouse.move(x0, y0 + (tb["y"] + tb["height"] - y0) * i / 30)
            page.wait_for_timeout(15)
        page.mouse.up()
        page.wait_for_timeout(600)
        self.assertEqual(self._parent_of(source), self.project.pk)


if __name__ == "__main__":
    unittest.main()
