"""The Plan view's work list, driven in a real browser.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skipped
without either.

``test_plan_worklist.py`` checks what the page carries and that the endpoints
answer; this checks the part only a browser has: that the edits on a card
(done, note, comment, label, plan time) are made in place by
``static/js/plan-worklist.js`` and land in the database, and that the list fits
a phone without a horizontal scroll.
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


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class PlanWorklistBrowserTests(unittest.TestCase):

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
        from application.models.item import Comment, Item
        from application.models.user import User
        self.Item = Item
        today = datetime.now(BERLIN).date()
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            user = User(username="wl-browser", email="wl@example.com",
                        level="premium", timezone="Europe/Berlin",
                        plan_layout="list")
            user.set_password("pw-wl-test")
            user.save()
            self.user = user
            project = Item(owner=user, kind="project", title="Webshop",
                           project_type="parallel").save()
            self.task = Item(
                owner=user, kind="task", title="Rechnung schreiben",
                parent=project,
                plan_date=datetime.combine(today, time(0, 0)).replace(tzinfo=BERLIN),
                note="Erste Zeile der Notiz.",
                comments=[Comment(author=user, body="alte Rueckfrage")]).save()
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 1400}, timezone_id="Europe/Berlin")
        self.context.add_cookies([{"name": "tz", "value": "Europe/Berlin",
                                   "url": self.base}])
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(f"{self.base}/auth/login")
        self.page.fill('input[name="email"]', "wl@example.com")
        self.page.fill('input[name="password"]', "pw-wl-test")
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")
        self.page.goto(f"{self.base}/tasks/today?view=plan")
        self.page.wait_for_load_state("networkidle")

    def tearDown(self):
        self.context.close()

    def _db(self):
        with self.app.app_context():
            return self.Item.objects(pk=self.task.pk).first()

    def _card(self):
        return self.page.locator(f'.wl-card[data-task-id="{self.task.pk}"]')

    def test_edits_on_the_card_are_made_in_place(self):
        page, card = self.page, self._card()
        self.assertEqual(card.count(), 1)

        # Note: edit, save, the rendered note comes back without a reload.
        card.locator(".wl-note-edit-btn").click()
        card.locator(".wl-note-form textarea").fill("Neu mit **fett**.")
        card.locator(".wl-note-form button[type=submit]").click()
        card.locator(".wl-note strong", has_text="fett").wait_for()
        self.assertEqual(self._db().note, "Neu mit **fett**.")

        # Comment: Enter posts it and it shows up under the old one.
        box = card.locator(".wl-comment-form textarea")
        box.fill("vom Arbeitsblatt")
        box.press("Enter")
        card.locator(".wl-comment", has_text="vom Arbeitsblatt").wait_for()
        self.assertEqual(self._db().comments[-1].body, "vom Arbeitsblatt")

        # Plan time: the day plus a time puts it on the timeline.
        card.locator(".wl-plan-time").fill("14:30")
        card.locator(".wl-plan-time").dispatch_event("change")
        page.wait_for_function(
            "() => !!document.querySelector('.wl-flash')")
        pd = self._db().plan_date.astimezone(BERLIN)
        self.assertEqual((pd.hour, pd.minute), (14, 30))

        # Label: typed and Enter; the list refreshes and shows the chip.
        page.reload()
        page.wait_for_load_state("networkidle")
        card = self._card()
        card.locator(".wl-tag-add").fill("kunde")
        card.locator(".wl-tag-add").press("Enter")
        card.locator(".tag-chip", has_text="kunde").wait_for()
        self.assertIn("kunde", self._db().tags)

        # Done: the card stays, marked; the task is done.
        card.locator(".wl-check").click()
        page.wait_for_function(
            f"() => document.querySelector('.wl-card[data-task-id=\"{self.task.pk}\"]')"
            ".classList.contains('is-done')")
        self.assertEqual(self._db().status, "done")
        self.assertEqual(self.errors, [])

    def test_fits_a_phone(self):
        self.page.set_viewport_size({"width": 375, "height": 800})
        self.page.reload()
        self.page.wait_for_load_state("networkidle")
        overflow = self.page.evaluate(
            "() => { const c = document.querySelector('.wl-card').getBoundingClientRect();"
            " return c.right - window.innerWidth; }")
        self.assertLessEqual(overflow, 0)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
