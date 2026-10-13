"""Opening a bell row marks it read, however it is opened.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); the browser half also needs Playwright with
Chromium (``.venv``). Each half skips without what it needs.

The bug: the bell marked a row read from its click handler, and a click
handler never hears of a middle click or of "Open in new tab" from the context
menu, so a notification opened that way stayed unread. A row now links to
``/notifications/<id>/open``, which marks it read on the server and redirects
to where it points.
"""
import threading
import unittest

from bson import ObjectId

from tests.test_inspector_time_log_browser import (
    _make_app, disconnect, sync_playwright)


def _seed():
    from application.models.user import User
    User.objects.delete()
    user = User(username="notif-open", email="notif-open@example.com",
                level="premium")
    user.set_password("pw-notif-open")
    item = ObjectId()
    user.add_notification(text="Assigned to you", kind="assigned",
                          link="/tags/", item_id=item)
    user.add_notification(text="New comment", kind="comment",
                          link="/tags/", item_id=item)
    user.add_notification(text="Other task", kind="comment",
                          link="/tags/", item_id=ObjectId())
    user.save()
    return user


def _read_state(app, user):
    from application.models.user import User
    with app.app_context():
        fresh = User.objects(pk=user.pk).first()
        return {n.text: n.read for n in fresh.notifications}


@unittest.skipIf(disconnect is None, "mongoengine not installed")
class OpenEndpointTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _make_app()

    @classmethod
    def tearDownClass(cls):
        from application.models.user import User
        with cls.app.app_context():
            User.objects.delete()
        disconnect(alias="default")

    def setUp(self):
        with self.app.app_context():
            self.user = _seed()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _ref(self, text):
        return next(str(n.pk_ref) for n in self.user.notifications
                    if n.text == text)

    def test_open_marks_the_row_read_and_redirects(self):
        resp = self.client.get(f"/notifications/{self._ref('New comment')}/open")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/tags/"))
        state = _read_state(self.app, self.user)
        # The older entry about the same task is part of the row.
        self.assertTrue(state["New comment"])
        self.assertTrue(state["Assigned to you"])
        self.assertFalse(state["Other task"])

    def test_unknown_entry_goes_to_the_dashboard(self):
        resp = self.client.get(f"/notifications/{ObjectId()}/open")
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("/tags/", resp.headers["Location"])
        self.assertFalse(any(_read_state(self.app, self.user).values()))

    def test_off_site_link_is_not_followed(self):
        from application.models.user import User
        with self.app.app_context():
            user = User.objects(pk=self.user.pk).first()
            user.notifications[2].link = "//evil.example/x"
            user.save()
        resp = self.client.get(f"/notifications/{self._ref('Other task')}/open")
        self.assertNotIn("evil", resp.headers["Location"])

    def test_bell_rows_link_through_open(self):
        html = self.client.get("/notifications/bell").get_json()["html"]
        self.assertIn(f"/notifications/{self._ref('New comment')}/open", html)
        self.assertNotIn('href="/tags/"', html)


@unittest.skipIf(disconnect is None or sync_playwright is None,
                 "mongoengine / playwright not installed")
class OpenInNewTabBrowserTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        cls.app = _make_app()
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
        from application.models.user import User
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        with cls.app.app_context():
            User.objects.delete()
        disconnect(alias="default")

    def setUp(self):
        with self.app.app_context():
            self.user = _seed()
        self.context = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(f"{self.base}/auth/login")
        self.page.fill('input[name="email"]', "notif-open@example.com")
        self.page.fill('input[name="password"]', "pw-notif-open")
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")
        self.home = self.page.url
        self.page.click("#notifBell [data-bs-toggle=dropdown]")
        self.row = self.page.locator(".notif-item", has_text="New comment")
        self.row.wait_for()

    def tearDown(self):
        self.context.close()

    def test_middle_click_opens_a_tab_and_marks_read(self):
        with self.context.expect_page() as new:
            self.row.click(button="middle")
        new.value.wait_for_url("**/tags/")
        state = _read_state(self.app, self.user)
        self.assertTrue(state["New comment"] and state["Assigned to you"])
        self.assertFalse(state["Other task"])
        # This page stays where it is, and its bell shows the row read.
        self.assertEqual(self.page.url, self.home)
        self.assertNotIn("notif-unread", self.row.get_attribute("class"))
        self.assertEqual(self.page.locator("#notifBadge").inner_text(), "1")
        self.assertEqual(self.errors, [])

    def test_link_opened_elsewhere_marks_read(self):
        """What "Open in new tab" from the context menu does: load the row's
        address without any event reaching this page."""
        href = self.row.get_attribute("href")
        other = self.context.new_page()
        other.goto(self.base + href)
        self.assertTrue(other.url.endswith("/tags/"))
        self.assertTrue(_read_state(self.app, self.user)["New comment"])

    def test_plain_click_navigates_and_the_target_shows_it_read(self):
        self.row.click()
        self.page.wait_for_url("**/tags/")
        self.page.wait_for_load_state("networkidle")
        self.assertEqual(self.page.locator("#notifBadge").inner_text(), "1")
        self.assertTrue(_read_state(self.app, self.user)["New comment"])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
