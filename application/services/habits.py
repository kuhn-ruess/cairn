"""Cycle math + progress for the habit-goals feature.

Everything cycle-related is *derived* from ``Habit.anchor`` + ``period_days``;
nothing is stored per cycle. Keep this the single source of truth so the view
and any future API agree on where a cycle begins and ends.
"""
from datetime import datetime, timedelta, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    """mongoengine returns tz-aware datetimes (tz_aware=True), but be defensive
    against naive values slipping in."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def day_anchor(dt: datetime | None = None) -> datetime:
    """Midnight (UTC) of the given day — the canonical cycle start point."""
    dt = _aware(dt or _utcnow())
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def current_cycle(habit, now: datetime | None = None):
    """``(index, start, end)`` of the cycle containing ``now``.

    ``index`` is 0 for the first cycle. ``start`` is inclusive, ``end`` is
    exclusive. A pre-anchor ``now`` (shouldn't happen) clamps to cycle 0.
    """
    now = _aware(now or _utcnow())
    anchor = _aware(habit.anchor)
    period = timedelta(days=habit.period_days)
    elapsed = now - anchor
    index = max(0, elapsed // period)
    start = anchor + index * period
    return index, start, start + period


def cycle_progress(habit, now: datetime | None = None) -> dict:
    """Everything the UI needs for one habit's current cycle.

    Returns a dict with the cycle window, days left, and a per-target breakdown
    (done / count / remaining / pct). Counts are computed in a single pass over
    the embedded check-ins — no extra queries.
    """
    now = _aware(now or _utcnow())
    index, start, end = current_cycle(habit, now)

    done: dict[str, int] = {}
    for c in habit.checkins:
        at = _aware(c.at)
        if start <= at < end:
            done[c.tid] = done.get(c.tid, 0) + 1

    targets = []
    total_done = total_goal = 0
    for t in habit.targets:
        d = done.get(t.tid, 0)
        goal = t.count or 1
        total_done += min(d, goal)
        total_goal += goal
        targets.append({
            "tid": t.tid,
            "name": t.name,
            "count": goal,
            "done": d,
            "remaining": max(0, goal - d),
            "met": d >= goal,
            "pct": min(100, round(d / goal * 100)) if goal else 0,
        })

    # Days left: whole days until the cycle rolls over (>=1 while in-cycle).
    days_left = max(0, (end - now).days + (1 if (end - now).seconds else 0))

    return {
        "index": index,
        "start": start,
        "end": end,
        "days_left": days_left,
        "targets": targets,
        "total_done": total_done,
        "total_goal": total_goal,
        "overall_pct": min(100, round(total_done / total_goal * 100)) if total_goal else 0,
        "all_met": total_goal > 0 and total_done >= total_goal,
    }


def add_checkin(habit, tid: str, at: datetime | None = None) -> bool:
    """Log one completion of target ``tid``. Returns False if ``tid`` unknown."""
    from application.models.habit import HabitCheckin

    if not any(t.tid == tid for t in habit.targets):
        return False
    habit.checkins.append(HabitCheckin(tid=tid, at=_aware(at or _utcnow())))
    habit.save()
    return True


def undo_checkin(habit, tid: str, now: datetime | None = None) -> bool:
    """Remove the most recent check-in of ``tid`` **in the current cycle**.

    Returns False if there's nothing to undo (never touches other cycles, so a
    mis-tap can be corrected without rewriting history).
    """
    now = _aware(now or _utcnow())
    _, start, end = current_cycle(habit, now)
    latest_i = latest_at = None
    for i, c in enumerate(habit.checkins):
        at = _aware(c.at)
        if c.tid == tid and start <= at < end:
            if latest_at is None or at >= latest_at:
                latest_at, latest_i = at, i
    if latest_i is None:
        return False
    del habit.checkins[latest_i]
    habit.save()
    return True
