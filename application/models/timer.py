"""The running stopwatch — kept in its own module for import order only.

``User`` embeds a :class:`RunningTimer` (one per user, so the navbar can render
the running clock without a query), while
:mod:`application.models.timetracking` imports ``User`` for its billing
references. Defining the timer here breaks that cycle: this module imports
nothing from either. It is re-exported from ``application.models.timetracking``,
which is where the rest of the module lives.
"""
from datetime import datetime, timezone

from mongoengine import (
    BooleanField,
    DateTimeField,
    EmbeddedDocument,
    StringField,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunningTimer(EmbeddedDocument):
    """The one stopwatch a user may have running — mite's core capture mode.

    Lives embedded on :class:`~application.models.user.User` (one per user, so
    the navbar can render the running state without a query). It holds *no*
    elapsed value: only ``started_at``. Elapsed time is derived on read, which
    is why the clock keeps running when the browser is closed — exactly like
    mite's server-side stopwatch. Stopping converts it into a regular
    :class:`TimeEntry` in the monthly bucket; nothing is ever logged while it
    runs. Same soft snapshots as ``TimeEntry`` (project/source as plain ids)
    so a deleted project or task can never break the navbar.
    """

    project_id = StringField(required=True)
    project_name = StringField(default="")
    customer_name = StringField(default="")
    team_id = StringField()

    started_at = DateTimeField(default=_utcnow)
    note = StringField(default="")
    billable = BooleanField(default=True)

    source_item_id = StringField()
    source_kind = StringField()
    source_label = StringField(default="")

    def elapsed_seconds(self) -> int:
        started = self.started_at
        if started is None:
            return 0
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0, int((_utcnow() - started).total_seconds()))

    def elapsed_minutes(self) -> int:
        """Whole minutes, rounded — what stopping the timer would log."""
        return max(1, round(self.elapsed_seconds() / 60))
