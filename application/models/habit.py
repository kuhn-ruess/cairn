"""Habit goals — recurring, rolling-window personal goals (premium).

A :class:`Habit` is a named goal ("Training") that runs in **rolling cycles of a
fixed length** (``period_days``, e.g. 14). Inside each cycle the user wants to
hit a set of sub-targets a certain number of times ("run 4×", "strength 6×").
When a cycle ends the next one starts automatically and every counter resets —
so the goal is a perpetual habit, not a one-off.

Design (matching the codebase conventions):

* **Personal & few documents.** One document per habit. Sub-targets are an
  embedded list; individual check-ins are embedded too (a habit does a handful
  of check-ins per cycle, so the list stays tiny). No per-event collection.
* **No stored cycle state.** The current cycle is *computed* from ``anchor`` +
  ``period_days`` on read (see ``application/services/habits.py``); nothing to
  migrate or reset on a schedule. A check-in counts toward whichever cycle its
  timestamp falls in, so history is preserved and the reset is implicit.
"""
import secrets
from datetime import datetime, timezone

from mongoengine import (
    DateTimeField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentListField,
    IntField,
    ReferenceField,
    StringField,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _short_id() -> str:
    return secrets.token_hex(4)


class HabitTarget(EmbeddedDocument):
    """One sub-goal inside a habit, e.g. "run" with a per-cycle count of 4."""

    tid = StringField(required=True, default=_short_id)
    name = StringField(required=True, max_length=120)
    count = IntField(required=True, min_value=1, default=1)  # times per cycle


class HabitCheckin(EmbeddedDocument):
    """A single logged completion of a target. ``at`` decides its cycle."""

    tid = StringField(required=True)          # -> HabitTarget.tid
    at = DateTimeField(required=True, default=_utcnow)


class Habit(Document):
    """A rolling-cycle goal owned by one user."""

    meta = {
        "collection": "habits",
        "indexes": ["owner", ("owner", "archived")],
        "ordering": ["order", "created_at"],
    }

    owner = ReferenceField("User", required=True)
    name = StringField(required=True, max_length=200)
    period_days = IntField(required=True, min_value=1, max_value=365, default=14)

    # First cycle starts at ``anchor`` (midnight of the creation day, UTC).
    # cycle N spans [anchor + N*period, anchor + (N+1)*period).
    anchor = DateTimeField(required=True, default=_utcnow)

    targets = EmbeddedDocumentListField(HabitTarget)
    checkins = EmbeddedDocumentListField(HabitCheckin)

    archived = DateTimeField()  # set == archived (hidden from the active list)
    order = IntField(default=0)
    created_at = DateTimeField(default=_utcnow)

    def __str__(self) -> str:
        return self.name
