"""Moving tasks into a section by dragging, in a real browser.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skipped
without either.

The bug: "eine task mit subtask lies sich nicht verschieben, sagte aber trozem
die sektion ist schon parent. Sobald ich ein single task verschoben habe, kann
ich keinen weiteren mehr verschieben." The message on screen was the merge
endpoint's "can't nest a task under its own subtask". A drag down to another
section drifts right on the way, and 48px of drift turns a drag into a NEST
(see test_nest_drag_browser.py): the list froze, the drop never reached the
section, and the task was nested under the row the pointer was on, which for
a task with open sub-tasks is its own sub-task. Over another list a drag is a
move; nesting stays a gesture inside the list the drag started in.
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
class SectionDragBrowserTests(unittest.TestCase):

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
            user = User(username="section-drag", email="section@example.com",
                        level="premium")
            user.set_password("pw-section-drag")
            user.save()
            P = Item(owner=user, kind="project", title="Sections",
                     project_type="parallel").save()
            self.project = P
            self.sec_a = Item(owner=user, kind="section", title="Alpha",
                              parent=P, status="active").save()
            self.sec_b = Item(owner=user, kind="section", title="Beta",
                              parent=P, status="active").save()
            self.parent_task = Item(owner=user, kind="task", title="Has subtasks",
                                    parent=P, sort_order=0).save()
            self.subs = [
                Item(owner=user, kind="task", title=f"Sub {i}",
                     parent=self.parent_task, sort_order=i).save()
                for i in range(2)
            ]
            self.single = [
                Item(owner=user, kind="task", title=f"Single {i}",
                     parent=P, sort_order=1 + i).save()
                for i in range(2)
            ]
            self.in_a = Item(owner=user, kind="task", title="In alpha",
                             parent=self.sec_a, sort_order=0).save()
            self.in_b = Item(owner=user, kind="task", title="In beta",
                             parent=self.sec_b, sort_order=0).save()
            # A pk in collapsed_items marks an EXPANDED parent in the project
            # list: the sub-task rows are on screen, as they were in the report.
            user.collapsed_items = [str(self.parent_task.pk)]
            user.save()
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 1400}, timezone_id="Europe/Berlin")
        self.context.add_cookies([{"name": "tz", "value": "Europe/Berlin",
                                   "url": self.base}])
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.merges = []
        self.page.on("request", lambda r: self.merges.append(r.url)
                     if r.url.endswith("/tasks/merge") else None)
        self.page.goto(f"{self.base}/auth/login")
        self.page.fill('input[name="email"]', "section@example.com")
        self.page.fill('input[name="password"]', "pw-section-drag")
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")
        self.page.goto(f"{self.base}/projects/{self.project.pk}")
        self.page.wait_for_load_state("networkidle")

    def tearDown(self):
        self.context.close()

    def _row(self, item):
        return self.page.locator(f'.task-row[data-task-id="{item.pk}"]').first

    def _parent_of(self, item):
        with self.app.app_context():
            return self.Item.all_objects.get(pk=item.pk).parent.pk

    def _drag(self, source, onto, drift=120, steps=40):
        """Grab the handle and head for ``onto`` (a task row or any element)
        the way a hand does: down and to the right at once."""
        page = self.page
        src = self._row(source)
        src.hover()
        hb = src.locator(".task-handle").bounding_box()
        x0, y0 = hb["x"] + hb["width"] / 2, hb["y"] + hb["height"] / 2
        tb = onto.bounding_box()
        x1, y1 = x0 + drift, tb["y"] + tb["height"] / 2
        page.mouse.move(x0, y0)
        page.mouse.down()
        for i in range(1, steps + 1):
            page.mouse.move(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
            page.wait_for_timeout(15)
        for _ in range(5):
            page.mouse.move(x1, y1)
            page.wait_for_timeout(30)
        page.mouse.up()
        # The move, then the background refresh of <main>.
        page.wait_for_timeout(1200)
        page.wait_for_load_state("networkidle")

    def test_a_task_with_open_subtasks_moves_into_a_section(self):
        self._drag(self.parent_task, self._row(self.in_a))
        self.assertEqual(self._parent_of(self.parent_task), self.sec_a.pk)
        for sub in self.subs:
            self.assertEqual(self._parent_of(sub), self.parent_task.pk)
        self.assertEqual(self.merges, [])
        self.assertEqual(self.errors, [])

    def test_one_move_after_another_each_lands_in_its_section(self):
        first, second = self.single
        self._drag(first, self._row(self.in_a))
        self.assertEqual(self._parent_of(first), self.sec_a.pk)
        self._drag(second, self._row(self.in_b))
        self.assertEqual(self._parent_of(second), self.sec_b.pk)
        self._drag(self.parent_task, self._row(self.in_b))
        self.assertEqual(self._parent_of(self.parent_task), self.sec_b.pk)
        self.assertEqual(self.merges, [])
        self.assertEqual(self.errors, [])

    def test_nesting_inside_the_list_still_works(self):
        # The parent's own sub-task is never the nest target; the row aimed at
        # in the same list still is.
        first, second = self.single
        self._drag(second, self._row(first), drift=120)
        self.assertEqual(self._parent_of(second), first.pk)

    def test_drop_on_a_sub_project_row_works_twice(self):
        with self.app.app_context():
            sub = self.Item(owner=self.project.owner, kind="project",
                            title="Sub project", parent=self.project,
                            status="active", sort_order=0).save()
        self.page.reload()
        self.page.wait_for_load_state("networkidle")
        sp_row = lambda: self.page.locator(f'.sp-row[data-sp-id="{sub.pk}"]').first
        first, second = self.single
        self._drag(first, sp_row(), drift=0)
        self.assertEqual(self._parent_of(first), sub.pk)
        self._drag(second, sp_row(), drift=0)
        self.assertEqual(self._parent_of(second), sub.pk)


if __name__ == "__main__":
    unittest.main()
