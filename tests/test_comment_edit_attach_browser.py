"""Editing a comment takes attachments, like writing a new one.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skips
without them.

The new comment box uploads a pasted or dropped file to the task and inserts
the Markdown link (note-paste.js). The box that opens on the pencil of an
existing comment was a bare textarea the upload code never looked at, so a
file dropped there did nothing and an image pasted there was lost. Both the
inspector and the task detail page are driven here.
"""
import threading
import unittest

try:
    from mongoengine import disconnect
except ImportError:  # pragma: no cover - no mongoengine installed
    disconnect = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - no playwright installed
    sync_playwright = None

if disconnect is not None:
    from tests.test_inspector_time_log_browser import _make_app, _seed, _wipe


# Drop a text file on the edit box, as the browser does for a Finder drag.
_DROP_JS = """(sel) => {
  const ta = document.querySelector(sel);
  const dt = new DataTransfer();
  dt.items.add(new File(["hello"], "offer.txt", {type: "text/plain"}));
  ta.dispatchEvent(new DragEvent("dragover", {dataTransfer: dt, bubbles: true, cancelable: true}));
  ta.dispatchEvent(new DragEvent("drop", {dataTransfer: dt, bubbles: true, cancelable: true}));
}"""

# Paste a small PNG from the clipboard into the edit box.
_PASTE_JS = """async (sel) => {
  const ta = document.querySelector(sel);
  ta.focus();
  const c = document.createElement("canvas");
  c.width = 8; c.height = 8;
  c.getContext("2d").fillRect(0, 0, 8, 8);
  const blob = await new Promise((r) => c.toBlob(r, "image/png"));
  const dt = new DataTransfer();
  dt.items.add(new File([blob], "shot.png", {type: "image/png"}));
  ta.dispatchEvent(new ClipboardEvent("paste", {clipboardData: dt, bubbles: true, cancelable: true}));
}"""


@unittest.skipIf(disconnect is None or sync_playwright is None,
                 "mongoengine / playwright not installed")
class CommentEditAttachBrowserTests(unittest.TestCase):

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
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        with cls.app.app_context():
            _wipe()
        disconnect(alias="default")

    def setUp(self):
        with self.app.app_context():
            self.user, _support, self.project, self.task = _seed()
            self.task.add_comment(self.user, "first draft")
        self.context = self.browser.new_context(viewport={"width": 1600, "height": 1000})
        self.page = self.context.new_page()
        self.errors, self.posts = [], []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.on("request", lambda r: r.method == "POST"
                     and self.posts.append(r.url.split(self.base, 1)[-1]))
        self.page.goto(f"{self.base}/auth/login")
        self.page.fill('input[name="email"]', "insp-time@example.com")
        self.page.fill('input[name="password"]', "pw-insp-time")
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")

    def tearDown(self):
        self.context.close()

    def _comment_body(self):
        with self.app.app_context():
            from application.models.item import Item
            return Item.objects(pk=self.task.pk).first().comments[0].body

    def _edit(self, scope):
        self.page.locator(f"{scope} .comment-edit-btn").first.click()
        sel = f"{scope} .comment-edit-form textarea"
        self.page.wait_for_selector(sel)
        return sel

    def _wait_for(self, sel, needle):
        self.page.wait_for_function(
            "([s, n]) => document.querySelector(s).value.includes(n)",
            arg=[sel, needle], timeout=5000)

    def _save(self, scope):
        self.page.locator(f"{scope} .comment-edit-save").click()
        self.page.wait_for_selector(f"{scope} .comment-edit-form", state="detached")

    def test_drop_a_file_on_the_edit_box_on_the_detail_page(self):
        self.page.goto(f"{self.base}/tasks/{self.task.pk}")
        self.page.wait_for_load_state("networkidle")
        scope = ".task-detail-comment"
        sel = self._edit(scope)
        self.page.evaluate(_DROP_JS, sel)
        self._wait_for(sel, f"/tasks/{self.task.pk}/note-file/")
        self.assertIn(f"/tasks/{self.task.pk}/note-file", self.posts)
        value = self.page.input_value(sel)
        self.assertTrue(value.startswith("first draft"), value)
        self.assertIn("[offer.txt](", value)
        self._save(scope)
        self.assertIn("[offer.txt](", self._comment_body())
        self.assertIn('href="', self.page.locator(f"{scope} .comment-text").inner_html())
        self.assertEqual(self.errors, [])

    def test_paste_an_image_into_the_edit_box_in_the_inspector(self):
        self.page.goto(f"{self.base}/projects/{self.project.pk}")
        self.page.wait_for_load_state("networkidle")
        self.page.evaluate(
            f"window.bktodoOpenInspector('/tasks/{self.task.pk}/edit', 'Do it')")
        self.page.wait_for_selector(".comments-list .comment-edit-btn")
        scope = ".comments-list"
        sel = self._edit(scope)
        self.page.evaluate(_PASTE_JS, sel)
        self._wait_for(sel, f"/tasks/{self.task.pk}/note-image/")
        self.assertIn(f"/tasks/{self.task.pk}/note-image", self.posts)
        self.assertNotIn("data:image", self.page.input_value(sel))
        self._save(scope)
        self.assertIn(f"/tasks/{self.task.pk}/note-image/", self._comment_body())
        self.assertIn("<img", self.page.locator(f"{scope} .comment-text").first.inner_html())
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
