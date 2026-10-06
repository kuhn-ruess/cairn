"""views/caldav: a stale reminder must not reopen a task closed in Cairn.

A task ticked off in Cairn came back open, again and again: Reminders still
held the open copy, wrote it back (an acknowledged alarm, any small edit) with
STATUS:NEEDS-ACTION before it had fetched the close, and ``_apply_ics`` read
every NEEDS-ACTION as "open". A client that did see the close and unticks the
reminder still reopens the task.

Run with ``python -m unittest discover tests``. Pure: no database, no network.
"""
import unittest
from datetime import datetime, timezone

from bson import ObjectId

from application.models.item import Item
from application.views.caldav import _apply_ics, _etag

PK = ObjectId("6abca673c356fbd2c694405c")
SEEN_OPEN = datetime(2026, 10, 6, 6, 40, tzinfo=timezone.utc)
CLOSED = datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 6, 7, 43, tzinfo=timezone.utc)


def _put(status="NEEDS-ACTION", modified=LATER):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//Apple Inc.//macOS 15.6//EN",
        "BEGIN:VTODO",
        f"UID:cairn-{PK}",
        "SUMMARY:FMG: Incident Erstellung Testen.",
        f"STATUS:{status}",
    ]
    if modified is not None:
        lines.append(f"LAST-MODIFIED:{modified:%Y%m%dT%H%M%SZ}")
    lines += ["END:VTODO", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


class _User:
    timezone = "Europe/Berlin"
    reminder_lead_min = 10


def _closed_task(**kw):
    item = Item(kind="task", status="done", title="x",
                completed_at=CLOSED, updated_at=CLOSED, **kw)
    item.pk = PK
    return item


def _etag_at(when):
    return _etag(Item(pk=PK, updated_at=when))


class StaleCopyTest(unittest.TestCase):

    def test_an_edit_of_the_open_copy_keeps_the_task_closed(self):
        item = _closed_task()
        _apply_ics(item, _put(), _User(), if_match=_etag_at(SEEN_OPEN))
        self.assertEqual(item.status, "done")

    def test_unticking_the_closed_copy_reopens(self):
        item = _closed_task()
        _apply_ics(item, _put(), _User(), if_match=_etag(item))
        self.assertEqual(item.status, "open")

    def test_a_later_cairn_edit_does_not_hide_that_the_client_saw_the_close(self):
        item = _closed_task()
        seen = _etag(item)
        item.updated_at = LATER
        _apply_ics(item, _put(), _User(), if_match=seen)
        self.assertEqual(item.status, "open")

    def test_without_if_match_an_older_last_modified_keeps_it_closed(self):
        item = _closed_task()
        _apply_ics(item, _put(modified=SEEN_OPEN), _User())
        self.assertEqual(item.status, "done")

    def test_without_if_match_a_newer_last_modified_reopens(self):
        item = _closed_task()
        _apply_ics(item, _put(modified=LATER), _User())
        self.assertEqual(item.status, "open")

    def test_a_dropped_task_is_guarded_too(self):
        item = _closed_task()
        item.status = "dropped"
        _apply_ics(item, _put(), _User(), if_match=_etag_at(SEEN_OPEN))
        self.assertEqual(item.status, "dropped")

    def test_completing_in_reminders_still_closes(self):
        item = Item(kind="task", status="open", title="x")
        item.pk = PK
        _apply_ics(item, _put(status="COMPLETED"), _User(),
                   if_match=_etag_at(SEEN_OPEN))
        self.assertEqual(item.status, "done")

    def test_an_open_task_stays_open(self):
        item = Item(kind="task", status="open", title="x")
        item.pk = PK
        _apply_ics(item, _put(modified=None), _User())
        self.assertEqual(item.status, "open")


if __name__ == "__main__":
    unittest.main()
