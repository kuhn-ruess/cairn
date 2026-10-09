"""The 12 Week Year (Brian Moran / Michael Lennington), as one document per cycle.

A cycle is 12 execution weeks plus a 13th week for review and the next plan.
It carries a vision, one to three goals (each with a lag measure: the result
you want to see) and the tactics that are supposed to get you there (the lead
measures: actions you control). A tactic is planned for some weeks, or for
every week when it is ``recurring``. The weekly execution score is the share
of the tactics planned for that week that were done.

Everything lives in ONE document per cycle: goals, tactics, the ticks and the
counts pulled from Habitify are small embedded lists, read together on every
page of the feature. Nothing per week is stored apart from what was ticked,
the Habitify counts and the review note; the week a day falls in is computed
from ``start`` (see ``application/services/twelve_week.py``).

It is also where the habits live: the rolling habits of the old Habits area
(``models/habit.py``, now read only) moved in as tactics without a goal.
"""
import secrets
from datetime import datetime, timezone

from mongoengine import (
    BooleanField,
    DateTimeField,
    DictField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentListField,
    FloatField,
    IntField,
    ListField,
    ReferenceField,
    StringField,
)

MAX_GOALS = 3
WEEKS = 12


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _short_id() -> str:
    return secrets.token_hex(4)


class TwyGoal(EmbeddedDocument):
    """A 12 week goal with its lag measure ("10 kg down", "20 new clients")."""

    gid = StringField(required=True, default=_short_id)
    title = StringField(required=True, max_length=200)
    # The lag measure: where you want to be at the end of week 12, and where
    # you are now. Both optional, a goal without a number is still a goal.
    lag_target = FloatField()
    lag_current = FloatField()
    lag_unit = StringField(max_length=40, default="")


class TwyTactic(EmbeddedDocument):
    """An action serving zero or more goals, planned for the weeks picked.

    A tactic without a goal is a plain habit: the rolling habits of the old
    Habits area live on as these (see ``services/habit_migration.py``). One
    habit can serve several goals (``gids``); it is still ONE action, so the
    weekly execution score counts it once, while each linked goal counts it
    in its own progress."""

    tid = StringField(required=True, default=_short_id)
    gids = ListField(StringField(max_length=20))    # -> TwyGoal.gid, 0..n
    # The single goal link of the first version. Read only: never written
    # any more, read by ``services.twelve_week.goal_ids`` while ``goals_listed`` is
    # unset and copied into ``gids`` on the next read of the cycle.
    gid = StringField(default="")
    # Set once ``gids`` is the answer (written by the form, or filled from
    # ``gid``); from then on an empty ``gids`` means "no goal".
    goals_listed = BooleanField(default=False)
    title = StringField(required=True, max_length=200)
    recurring = BooleanField(default=False)         # all weeks 1..12 picked
    weeks = ListField(IntField(min_value=1, max_value=WEEKS))
    # Done in a week when it is ticked, or counted ``per_week`` times there:
    # by "+1" (``log``, one ISO date per time) or by Habitify (completed days).
    per_week = IntField(min_value=1, max_value=7, default=1)
    log = ListField(StringField(max_length=10))
    # Mapped to a Habitify habit.
    habitify_id = StringField(max_length=100, default="")
    habitify_name = StringField(max_length=200, default="")
    # Cairn keeps the habit's start and end date in Habitify in line with the
    # weeks picked here (on the user's confirmation); the dates last written.
    habitify_manage = BooleanField(default=False)
    habitify_start = StringField(max_length=10, default="")
    habitify_end = StringField(max_length=10, default="")
    # Where a migrated tactic came from: "habit:<habit id>:<target tid>".
    source = StringField(max_length=80, default="")


class TwelveWeekYear(Document):
    """One 12 week cycle owned by one user."""

    meta = {
        "collection": "twelve_week_years",
        "indexes": [("owner", "start")],
        "ordering": ["-start"],
    }

    owner = ReferenceField("User", required=True)
    title = StringField(required=True, max_length=200)
    vision = StringField(default="")
    # Day one of week 1, stored as midnight UTC of that calendar date.
    start = DateTimeField(required=True)

    goals = EmbeddedDocumentListField(TwyGoal)
    tactics = EmbeddedDocumentListField(TwyTactic)

    # Ticked by hand: "<tid>:<week>".
    done = ListField(StringField())
    # Completed days per tactic and week, as last read from Habitify:
    # {"<tid>:<week>": n}.
    habitify_counts = DictField()
    habitify_synced_at = DateTimeField()
    habitify_error = StringField(default="")
    # The weekly accountability note, {"<week>": text}; "13" is the review.
    week_notes = DictField()

    created_at = DateTimeField(default=_utcnow)

    def goal(self, gid):
        return next((g for g in self.goals if g.gid == gid), None)

    def tactic(self, tid):
        return next((t for t in self.tactics if t.tid == tid), None)

    def __str__(self) -> str:
        return self.title
