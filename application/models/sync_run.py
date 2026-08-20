"""What the importers did, one row per run.

A sync is the part of this app that acts on its own: it runs in a worker every
few minutes, talks to a system nobody here controls, and writes into billing
data. "It seems to have imported nothing last night" has to be answerable
afterwards, and by the person responsible for the team rather than by whoever
can read a container's logs.

Kept for **90 days** and then removed by MongoDB itself: the ``created_at``
index carries ``expireAfterSeconds``, so there is no sweep to schedule and no
job that can quietly stop running. Ninety days is long enough to explain a
month-end invoice and short enough that this never becomes an archive.
"""
from datetime import datetime, timezone

from mongoengine import (
    BooleanField,
    DateTimeField,
    DictField,
    Document,
    FloatField,
    ReferenceField,
    StringField,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SyncRun(Document):
    meta = {
        "collection": "sync_runs",
        "indexes": [
            ("team", "-created_at"),
            ("user", "-created_at"),
            # MongoDB's own TTL sweep — see the module docstring.
            {"fields": ["created_at"], "expireAfterSeconds": 90 * 24 * 3600},
        ],
        "ordering": ["-created_at"],
    }

    # What ran: "zammad-team", "zammad-user", "mite".
    kind = StringField(required=True, max_length=32)
    # Who it ran for. A team run has no user; a personal one has both when the
    # user is in a team, because a manager looking at the team wants to see it.
    team = ReferenceField("Team")
    user = ReferenceField("User")
    # How it was started: "manual" (someone pressed a button), "background"
    # (the worker), "cli".
    trigger = StringField(default="manual", max_length=16)

    ok = BooleanField(default=True)
    error = StringField(default="")
    seconds = FloatField(default=0.0)
    # The run's own counters, trimmed to what a human reads — never the
    # per-ticket debug trace, which exists for one page and would dwarf this.
    stats = DictField()

    created_at = DateTimeField(default=_utcnow)

    def __str__(self) -> str:
        return f"{self.kind} {self.created_at:%Y-%m-%d %H:%M}"
