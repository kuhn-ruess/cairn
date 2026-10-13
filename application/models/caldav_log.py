"""What CalDAV clients asked of the server, one row per request.

A reminder that vanished from Cairn is only explainable from the request
sequence the device sent: which client, in which order, against which copy of
the task. The gunicorn access log has that, but it dies with the container on
every deploy, so the sequence behind a DELETE from days ago was never
recoverable. This journal keeps it next to the data.

Kept for **30 days** and then removed by MongoDB itself (TTL index on
``created_at``, the same mechanism as ``sync_runs``). Unauthenticated requests
are not recorded: they are noise, and the rate limiter deals with them.
"""
from datetime import datetime, timezone

from mongoengine import (
    DateTimeField,
    Document,
    IntField,
    ReferenceField,
    StringField,
)

RETENTION_DAYS = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CalDavRequest(Document):
    meta = {
        "collection": "caldav_log",
        "indexes": [
            ("user", "-created_at"),
            {"fields": ["created_at"],
             "expireAfterSeconds": RETENTION_DAYS * 24 * 3600},
        ],
        "ordering": ["-created_at"],
    }

    created_at = DateTimeField(default=_utcnow)
    user = ReferenceField("User", required=True)
    method = StringField(max_length=16)
    path = StringField(max_length=500)
    status = IntField()
    user_agent = StringField(max_length=300)
    depth = StringField(max_length=16)
    # The client's ``If-Match`` and the ETag the task had when the request
    # arrived: a DELETE or PUT made against an older copy shows up as the two
    # disagreeing.
    if_match = StringField(max_length=200)
    etag = StringField(max_length=100)
    item_id = StringField(max_length=40)
    # The task's status before the request, and for a PUT the STATUS the
    # client sent.
    item_status = StringField(max_length=20)
    sent_status = StringField(max_length=20)
    # What the server made of it: "trashed", "moved", "stale", ...
    outcome = StringField(max_length=40)
    # Resources answered (PROPFIND Depth 1, REPORT) and, for a multiget,
    # how many the client asked for.
    count = IntField()
    asked = IntField()
