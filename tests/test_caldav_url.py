"""views/caldav: the link a reminder carries must survive Cairn.

A reminder made from Apple Mail (Share, or Siri's "remind me about this")
keeps the mail in the VTODO's ``URL`` property as
``message://%3C<message-id>%3E``. Cairn used to read nothing but the title and
the notes, and wrote its own task link into ``URL`` on every export, so the mail
was gone twice: never stored in Cairn, and replaced on the device the next time
the client fetched the task.

Run with ``python -m unittest discover tests``. Pure: no database, no network.
"""
import unittest

from bson import ObjectId
from icalendar import Calendar

from application.models.item import Item
from application.views.caldav import _apply_ics
from application.views.filters import safe_link

PK = ObjectId("6abcea0388b85cc53a6fb448")
MAIL = ("message://%3C0f9a2b1c-4d5e-6f70-8192-a3b4c5d6e7f8"
        "@mail.example.com%3E")

# What Reminders on macOS sends for a reminder created from a mail (modelled
# on its PUTs: Apple's PRODID, X-APPLE-SORT-ORDER, the URL typed as a URI; not
# yet checked against a wire dump, see BKTODO_CALDAV_DEBUG).
APPLE_PUT = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Apple Inc.//macOS 15.6//EN\r\n"
    "CALSCALE:GREGORIAN\r\n"
    "BEGIN:VTODO\r\n"
    "CREATED:20260929T081512Z\r\n"
    "DTSTAMP:20260929T081514Z\r\n"
    "LAST-MODIFIED:20260929T081514Z\r\n"
    "SEQUENCE:0\r\n"
    "STATUS:NEEDS-ACTION\r\n"
    "SUMMARY:Angebot Firma Muster beantworten\r\n"
    "UID:4B0E6A2C-7F31-4E0B-9C55-2D1A9B7E3F10\r\n"
    f"URL;VALUE=URI:{MAIL}\r\n"
    "X-APPLE-SORT-ORDER:780480914\r\n"
    "END:VTODO\r\n"
    "END:VCALENDAR\r\n"
)


def _without_url(ics):
    return "".join(ln + "\r\n" for ln in ics.split("\r\n")
                   if ln and not ln.startswith("URL"))


class _User:
    timezone = "Europe/Berlin"
    reminder_lead_min = 10


class InboundTest(unittest.TestCase):

    def _item(self, **kw):
        item = Item(kind="task", status="open", title="x", **kw)
        item.pk = PK
        return item

    def test_the_mail_link_is_kept(self):
        item = self._item()
        self.assertTrue(_apply_ics(item, APPLE_PUT, _User()))
        self.assertEqual(item.url, MAIL)
        self.assertEqual(item.title, "Angebot Firma Muster beantworten")

    def test_a_new_reminder_without_pk_keeps_it_too(self):
        item = Item(kind="task", status="open", title="x")
        _apply_ics(item, APPLE_PUT, _User())
        self.assertEqual(item.url, MAIL)

    def test_our_own_backlink_coming_back_is_no_link(self):
        ics = APPLE_PUT.replace(
            MAIL, f"https://app.cairn-app.de/tasks/{PK}")
        item = self._item()
        _apply_ics(item, ics, _User())
        self.assertFalse(item.url)

    def test_a_client_without_url_does_not_cost_the_task_its_mail(self):
        item = self._item(url=MAIL)
        _apply_ics(item, _without_url(APPLE_PUT), _User())
        self.assertEqual(item.url, MAIL)

    def test_a_link_in_attach_counts_when_there_is_no_url(self):
        ics = _without_url(APPLE_PUT).replace(
            "X-APPLE-SORT-ORDER", f"ATTACH:{MAIL}\r\nX-APPLE-SORT-ORDER")
        item = self._item()
        _apply_ics(item, ics, _User())
        self.assertEqual(item.url, MAIL)

    def test_a_binary_attachment_is_not_a_link(self):
        ics = _without_url(APPLE_PUT).replace(
            "X-APPLE-SORT-ORDER",
            "ATTACH;ENCODING=BASE64;VALUE=BINARY:aGFsbG8=\r\nX-APPLE-SORT-ORDER")
        item = self._item()
        _apply_ics(item, ics, _User())
        self.assertFalse(item.url)


class RoundTripTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from application import create_app
        cls.app = create_app("application.config.TestConfig")

    def _export(self, item):
        from application.views.caldav import _todo_ics
        with self.app.test_request_context("/caldav/"):
            todo = Calendar.from_ical(_todo_ics(item, _User())).walk("VTODO")[0]
        return todo

    def _item(self, **kw):
        item = Item(kind="task", status="open", title="Angebot", **kw)
        item.pk = PK
        return item

    def test_the_export_hands_the_mail_back(self):
        """Overwriting it with the task link is what emptied it on the device."""
        todo = self._export(self._item(url=MAIL))
        self.assertEqual(str(todo.get("url")), MAIL)

    def test_the_way_back_to_cairn_stays_in_the_description(self):
        todo = self._export(self._item(url=MAIL))
        self.assertIn(f"/tasks/{PK}", str(todo.get("description")))

    def test_without_a_link_the_url_is_the_task_as_before(self):
        todo = self._export(self._item())
        self.assertTrue(str(todo.get("url")).endswith(f"/tasks/{PK}"))

    def test_put_export_put_changes_nothing(self):
        item = self._item()
        _apply_ics(item, APPLE_PUT, _User())
        with self.app.test_request_context("/caldav/"):
            from application.views.caldav import _todo_ics
            echoed = _todo_ics(item, _User())
        _apply_ics(item, echoed, _User())
        self.assertEqual(item.url, MAIL)
        self.assertEqual(item.note, "")


class SafeLinkTest(unittest.TestCase):

    def test_mail_and_web_links_render(self):
        for url in (MAIL, "message:%3Cabc@x%3E", "https://example.com/a",
                    "mailto:a@b.de", "x-devonthink-item://1234"):
            self.assertEqual(safe_link(url), url)

    def test_script_schemes_do_not(self):
        for url in ("javascript:alert(1)", " JavaScript:alert(1)",
                    "data:text/html,<script>", "vbscript:x", "", None):
            self.assertEqual(safe_link(url), "")


if __name__ == "__main__":
    unittest.main()
