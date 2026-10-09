"""Move the rolling habits of the old Habits area into the 12 Week Year.

Every target of an old :class:`~application.models.habit.Habit` ("Training:
run 4x in 14 days") becomes one habit tactic (a tactic without a goal) in the
user's running cycle, or in a new cycle called "Habits" starting this week
when none is running:

* planned for all twelve weeks, counted with "+1" like before;
* ``per_week`` is the old count spread over a week, rounded up and kept in
  1..7 (4 in 14 days -> 2 a week), because the 12 Week Year counts by week;
* every old check-in is kept, as the date it happened on in the user's
  timezone (``TwyTactic.log``), so the history counts in the weeks it falls
  in;
* ``source = "habit:<habit id>:<target tid>"`` names where it came from.

Nothing old is changed or deleted. Before anything is written the user's
habit documents are copied as they are into ``habit_backups`` (upsert by
their id), and the ``habits`` collection stays where it is, no longer
written by anything, until it is dropped by hand.

Idempotent twice over: ``User.habits_migrated`` stops the check after the
first run, and a target whose ``source`` is already on one of the user's
tactics is never copied again.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from application.models.habit import Habit
from application.models.twelve_week import TwelveWeekYear, TwyTactic, WEEKS
from application.services import twelve_week as tw
from application.services.tz import user_zone

BACKUP_COLLECTION = "habit_backups"


def per_week(count: int, period_days: int) -> int:
    period = max(1, period_days or 1)
    return max(1, min(7, math.ceil((count or 1) * 7 / period)))


def _source(habit, target) -> str:
    return f"habit:{habit.pk}:{target.tid}"


def _backup(habits) -> int:
    """Copy the raw documents into ``habit_backups``; returns how many."""
    coll = Habit._get_collection()
    db = coll.database
    ids = [h.pk for h in habits]
    now = datetime.now(timezone.utc)
    n = 0
    for raw in coll.find({"_id": {"$in": ids}}):
        db[BACKUP_COLLECTION].replace_one(
            {"_id": raw["_id"]}, {**raw, "backed_up_at": now}, upsert=True)
        n += 1
    return n


def _running_cycle(user, today):
    for c in TwelveWeekYear.objects(owner=user):
        if 1 <= tw.week_of(c, today) <= tw.REVIEW_WEEK:
            return c
    return None


def migrate_user(user, *, dry_run: bool = False, now: datetime | None = None) -> dict:
    """Move ``user``'s habits. Returns counters for the caller to report."""
    stats = {"habits": 0, "tactics": 0, "checkins": 0, "skipped": 0,
             "backed_up": 0, "cycle": None, "created_cycle": False}
    habits = list(Habit.objects(owner=user))
    stats["habits"] = len(habits)
    zone = user_zone(user)
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()

    cycles = list(TwelveWeekYear.objects(owner=user))
    known = {t.source for c in cycles for t in c.tactics if t.source}
    todo = [(h, t) for h in habits for t in h.targets
            if _source(h, t) not in known]
    stats["skipped"] = sum(len(h.targets) for h in habits) - len(todo)

    if todo and not dry_run:
        stats["backed_up"] = _backup(habits)
        cycle = _running_cycle(user, today)
        if cycle is None:
            cycle = TwelveWeekYear(owner=user, title="Habits",
                                   start=tw.to_start(tw.monday_of(today)))
            stats["created_cycle"] = True
        for h, t in todo:
            log = []
            for c in h.checkins:
                if c.tid != t.tid:
                    continue
                at = c.at if c.at.tzinfo else c.at.replace(tzinfo=timezone.utc)
                log.append(at.astimezone(zone).date().isoformat())
            title = h.name if len(h.targets) == 1 else f"{h.name}: {t.name}"
            cycle.tactics.append(TwyTactic(
                gid="", title=title[:200], recurring=True,
                weeks=list(range(1, WEEKS + 1)),
                per_week=per_week(t.count, h.period_days),
                log=sorted(log), source=_source(h, t)))
            stats["tactics"] += 1
            stats["checkins"] += len(log)
        cycle.save()
        stats["cycle"] = str(cycle.pk)
    elif todo:
        stats["tactics"] = len(todo)
        stats["checkins"] = sum(1 for h, t in todo for c in h.checkins if c.tid == t.tid)

    if not dry_run and not user.habits_migrated:
        user.habits_migrated = True
        user.save()
    return stats


def ensure_migrated(user) -> dict | None:
    """The page hook: run once per user, then never ask again."""
    if user.habits_migrated:
        return None
    return migrate_user(user)
