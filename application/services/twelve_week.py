"""Week math and scoring for the 12 Week Year.

Pure functions over a :class:`~application.models.twelve_week.TwelveWeekYear`
and plain ``date`` values: no clock, no database, no network. The view passes
"today" in the user's own timezone, so the week a tick lands in is the week
the user is living in, not the server's.

Week ``n`` (1 based) covers ``start + 7*(n-1)`` up to the day before
``start + 7*n``. Weeks 1 to 12 are the execution weeks, week 13 is the review
and planning week, anything later means the cycle is over.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from application.models.twelve_week import WEEKS

TARGET_SCORE = 85       # the book's line: 85 % of the plan executed is a win
REVIEW_WEEK = WEEKS + 1


def as_date(value) -> date:
    """``start`` is stored as midnight UTC; read the calendar date back."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.date()
    return value


def to_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_of(cycle, day: date) -> int:
    """The week ``day`` falls in: 0 before the start, 1..13 inside, 14 after."""
    delta = (day - as_date(cycle.start)).days
    if delta < 0:
        return 0
    return min(REVIEW_WEEK + 1, delta // 7 + 1)


def week_range(cycle, week: int) -> tuple[date, date]:
    """First and last day (both inclusive) of ``week``."""
    first = as_date(cycle.start) + timedelta(days=7 * (week - 1))
    return first, first + timedelta(days=6)


def end_date(cycle) -> date:
    """Last day of the review week."""
    return week_range(cycle, REVIEW_WEEK)[1]


def goal_ids(tactic) -> list[str]:
    """The goals a tactic serves: ``gids``, or the old single ``gid``."""
    if tactic.goals_listed or tactic.gids:
        return list(tactic.gids or [])
    return [tactic.gid] if tactic.gid else []


def backfill_goal_ids(cycle) -> bool:
    """Copy the old single goal link into ``gids`` where that list is still
    empty. ``gid`` itself is left as it is. Returns whether anything changed
    (the caller saves)."""
    changed = False
    for t in cycle.tactics:
        if not t.goals_listed:
            t.gids = goal_ids(t)
            t.goals_listed = True
            changed = True
    return changed


def serves(tactic, gid: str) -> bool:
    return gid in goal_ids(tactic)


def planned(tactic, week: int) -> bool:
    if not 1 <= week <= WEEKS:
        return False
    return tactic.recurring or week in (tactic.weeks or [])


def key(tid: str, week: int) -> str:
    return f"{tid}:{week}"


def logged(cycle, tactic, week: int) -> int:
    """How often "+1" was logged for ``tactic`` in ``week``."""
    n = 0
    for raw in tactic.log or []:
        try:
            if week_of(cycle, date.fromisoformat(raw)) == week:
                n += 1
        except ValueError:
            continue
    return n


def count(cycle, tactic, week: int) -> int:
    """The times counted in ``week``: "+1" logs plus Habitify's days."""
    k = key(tactic.tid, week)
    return logged(cycle, tactic, week) + int((cycle.habitify_counts or {}).get(k, 0))


def is_done(cycle, tactic, week: int) -> bool:
    """Ticked by hand, or counted ``per_week`` times that week ("+1" logs
    and Habitify's completed days together)."""
    if key(tactic.tid, week) in (cycle.done or []):
        return True
    if tactic.habitify_id or tactic.log:
        return count(cycle, tactic, week) >= (tactic.per_week or 1)
    return False


def counted(tactic) -> bool:
    """Shown as a "+1" counter rather than a checkbox."""
    return (tactic.per_week or 1) > 1


def add_log(cycle, tactic, week: int, today: date) -> bool:
    """One more time in ``week``: dated today when today is in that week,
    else the week's first day."""
    if not planned(tactic, week):
        return False
    first, last = week_range(cycle, week)
    day = today if first <= today <= last else first
    tactic.log = list(tactic.log or []) + [day.isoformat()]
    return True


def undo_log(cycle, tactic, week: int) -> bool:
    """Take back the latest "+1" of ``week``; other weeks stay untouched."""
    entries = list(tactic.log or [])
    for i in range(len(entries) - 1, -1, -1):
        try:
            if week_of(cycle, date.fromisoformat(entries[i])) == week:
                del entries[i]
                tactic.log = entries
                return True
        except ValueError:
            continue
    return False


ALL_WEEKS = list(range(1, WEEKS + 1))


def habitify_window(tactic, cycle) -> tuple[str, str]:
    """First day of the first picked week and last day of the last one, as
    Habitify wants them (``YYYY-MM-DD``)."""
    weeks = ALL_WEEKS if tactic.recurring else sorted(tactic.weeks or [])
    if not weeks:
        return "", ""
    return (week_range(cycle, weeks[0])[0].isoformat(),
            week_range(cycle, weeks[-1])[1].isoformat())


def gaps(tactic) -> list[int]:
    """Weeks inside the tactic's span that are NOT picked. Habitify has no
    schedule by calendar week, so these cannot be paused there."""
    weeks = sorted(set(ALL_WEEKS if tactic.recurring else (tactic.weeks or [])))
    if not weeks:
        return []
    return [w for w in range(weeks[0], weeks[-1] + 1) if w not in weeks]


def week_score(cycle, week: int) -> dict:
    """``{"planned", "done", "score"}`` for one week; ``score`` is None when
    nothing was planned, because 0 of 0 is not a failed week."""
    total = done = 0
    for t in cycle.tactics:
        if planned(t, week):
            total += 1
            if is_done(cycle, t, week):
                done += 1
    score = round(done / total * 100) if total else None
    return {"week": week, "planned": total, "done": done, "score": score}


def goal_week_score(cycle, gid: str, week: int) -> dict:
    """The execution score of ONE goal's tactics in ``week``: a habit that
    serves several goals counts in each of them here, and once in
    :func:`week_score`."""
    total = done = 0
    for t in cycle.tactics:
        if serves(t, gid) and planned(t, week):
            total += 1
            if is_done(cycle, t, week):
                done += 1
    return {"week": week, "planned": total, "done": done,
            "score": round(done / total * 100) if total else None}


def goal_average(cycle, gid: str, today: date):
    """A goal's average execution over the finished weeks, or None."""
    current = week_of(cycle, today)
    scores = [goal_week_score(cycle, gid, w)["score"]
              for w in range(1, min(current, REVIEW_WEEK))]
    scores = [s for s in scores if s is not None]
    return round(sum(scores) / len(scores)) if scores else None


def scoreboard(cycle, today: date) -> dict:
    """Everything the overview needs: the score of every week, the average of
    the weeks already finished, and where we are now."""
    current = week_of(cycle, today)
    weeks = [week_score(cycle, w) for w in range(1, WEEKS + 1)]
    closed = [w["score"] for w in weeks
              if w["score"] is not None and w["week"] < min(current, REVIEW_WEEK)]
    average = round(sum(closed) / len(closed)) if closed else None
    return {
        "current": current,
        "weeks": weeks,
        "average": average,
        "target": TARGET_SCORE,
        "on_track": average is not None and average >= TARGET_SCORE,
    }


def goal_progress(goal) -> int | None:
    """Lag measure progress in percent, or None when the goal has no number."""
    if not goal.lag_target:
        return None
    return max(0, min(100, round((goal.lag_current or 0) / goal.lag_target * 100)))


def toggle_done(cycle, tid: str, week: int) -> bool | None:
    """Flip the manual tick. Returns the new state, None when the tactic is
    unknown or not planned for that week (nothing is written then)."""
    tactic = cycle.tactic(tid)
    if tactic is None or not planned(tactic, week):
        return None
    k = key(tid, week)
    done = list(cycle.done or [])
    if k in done:
        done.remove(k)
        state = False
    else:
        done.append(k)
        state = True
    cycle.done = done
    return state
