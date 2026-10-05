"""Pasting several lines into a note, then tidying them with Tab, in a real
browser.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and Playwright with Chromium (``.venv``); skipped
without either.

The bug: "habe das eingefuegt ... dann mit tab aufräumen wollen, dann ist das
passiert". Two things went wrong on the way:

* Laying the pasted blocks down moved the open block's element back into the
  place it already had. A focused element that is re-inserted loses its focus,
  the blur committed the block halfway through the paste, and the commit meant
  to render the new blocks found nothing open anymore. The blocks stayed
  EMPTY bullets until the page was reloaded.
* Zoomed in on a block, the pasted lines were filed as its SIBLINGS, which a
  zoom does not show, and the empty block they replaced lost its ``id::``, the
  very id the zoom in the address bar points at.
"""
import threading
import unittest

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - no playwright / mongoengine installed
    sync_playwright = None

ZOOM_ID = "1f281876-58d2-4b55-86bb-00dbd9117c85"
BODY = f"- Server\n\t-\n\t  id:: {ZOOM_ID}\n- other\n"
PASTE = ("MSSQL:\n"
         "ex-db010 --> Win 2019  hat zwei Instanzen SQL 2019\n"
         "ex-db103 --> Win 2022 mit einer SQL 2022\n"
         "Oracle:\n"
         "lx2-db057 --> RHEL8 + zwei Instanzen 19c\n"
         "lx2-db101 --> RHEL9 + eine Instanz 19c\n")

# The outline on screen, one "depth|text" entry per block, depth-first.
SHOWN = """() => {
  const out = [];
  const walk = (list, d) => list.querySelectorAll(":scope > .ol-block").forEach((b) => {
    out.push(d + "|" + b.querySelector(":scope > .ol-row > .ol-content").innerText.trim());
    const kids = b.querySelector(":scope > .ol-children > .ol-list");
    if (kids) walk(kids, d + 1);
  });
  walk(document.querySelector("#note-outline > .ol-list"), 0);
  return out;
}"""


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class OutlinerPasteBrowserTests(unittest.TestCase):

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
            user = User(username="paste-test", email="paste@example.com",
                        level="premium")
            user.set_password("pw-paste-test")
            user.save()
            self.note = Item(owner=user, kind="note", title="Server",
                             note=BODY).save()
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 1000}, timezone_id="Europe/Berlin")
        self.context.add_cookies([{"name": "tz", "value": "Europe/Berlin",
                                   "url": self.base}])
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(f"{self.base}/auth/login")
        self.page.fill('input[name="email"]', "paste@example.com")
        self.page.fill('input[name="password"]', "pw-paste-test")
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")

    def tearDown(self):
        self.context.close()

    def _open(self, zoom=False):
        url = f"{self.base}/notes/{self.note.pk}" + (f"?zoom={ZOOM_ID}" if zoom else "")
        self.page.goto(url)
        self.page.wait_for_load_state("networkidle")

    def _paste_into(self, index):
        page = self.page
        page.locator("#note-outline .ol-block .ol-content").nth(index).click()
        page.wait_for_selector("textarea.ol-edit")
        page.evaluate("""(text) => {
          const ta = document.querySelector("textarea.ol-edit");
          const dt = new DataTransfer();
          dt.setData("text/plain", text);
          ta.dispatchEvent(new ClipboardEvent("paste",
            {clipboardData: dt, bubbles: true, cancelable: true}));
        }""", PASTE)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(300)

    def _tab(self, text):
        self.page.locator("#note-outline .ol-content", has_text=text).first.click()
        self.page.wait_for_selector("textarea.ol-edit")
        self.page.keyboard.press("Tab")
        self.page.wait_for_timeout(300)

    def _stored(self):
        self.page.keyboard.press("Escape")
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(300)
        with self.app.app_context():
            return self.Item.objects.get(pk=self.note.pk).note

    def test_pasted_lines_show_their_text_and_tab_nests_them(self):
        self._open()
        self._paste_into(1)
        shown = self.page.evaluate(SHOWN)
        # The last one is open in its textarea; every other block has text.
        self.assertEqual(shown[:6], [
            "0|Server", "1|MSSQL:",
            "1|ex-db010 --> Win 2019 hat zwei Instanzen SQL 2019",
            "1|ex-db103 --> Win 2022 mit einer SQL 2022",
            "1|Oracle:", "1|lx2-db057 --> RHEL8 + zwei Instanzen 19c"])
        for host in ("ex-db010", "ex-db103", "lx2-db057", "lx2-db101"):
            self._tab(host)
        self.assertEqual(self._stored(), (
            "- Server\n"
            "\t- MSSQL:\n"
            f"\t  id:: {ZOOM_ID}\n"
            "\t\t- ex-db010 --> Win 2019  hat zwei Instanzen SQL 2019\n"
            "\t\t- ex-db103 --> Win 2022 mit einer SQL 2022\n"
            "\t- Oracle:\n"
            "\t\t- lx2-db057 --> RHEL8 + zwei Instanzen 19c\n"
            "\t\t- lx2-db101 --> RHEL9 + eine Instanz 19c\n"
            "- other\n"))
        self.assertEqual(self.page.evaluate(SHOWN), [
            "0|Server", "1|MSSQL:",
            "2|ex-db010 --> Win 2019 hat zwei Instanzen SQL 2019",
            "2|ex-db103 --> Win 2022 mit einer SQL 2022",
            "1|Oracle:",
            "2|lx2-db057 --> RHEL8 + zwei Instanzen 19c",
            "2|lx2-db101 --> RHEL9 + eine Instanz 19c",
            "0|other"])
        self.assertEqual(self.errors, [])

    def test_a_paste_while_zoomed_in_stays_in_the_zoom(self):
        self._open(zoom=True)
        self._paste_into(0)
        shown = self.page.evaluate(SHOWN)
        self.assertEqual(len(shown), 6, shown)
        self.assertEqual(shown[0], "0|MSSQL:")
        self.assertTrue(all(row.startswith("1|") for row in shown[1:]), shown)
        self.assertEqual(self._stored(), (
            "- Server\n"
            "\t- MSSQL:\n"
            f"\t  id:: {ZOOM_ID}\n"
            "\t\t- ex-db010 --> Win 2019  hat zwei Instanzen SQL 2019\n"
            "\t\t- ex-db103 --> Win 2022 mit einer SQL 2022\n"
            "\t\t- Oracle:\n"
            "\t\t- lx2-db057 --> RHEL8 + zwei Instanzen 19c\n"
            "\t\t- lx2-db101 --> RHEL9 + eine Instanz 19c\n"
            "- other\n"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
