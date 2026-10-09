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


def planned(tactic, week: int) -> bool:
    if not 1 <= week <= WEEKS:
        return False
    return tactic.recurring or week in (tactic.weeks or [])


def key(tid: str, week: int) -> str:
    return f"{tid}:{week}"


def is_done(cycle, tactic, week: int) -> bool:
    """Ticked by hand, or Habitify reports enough completed days that week."""
    k = key(tactic.tid, week)
    if k in (cycle.done or []):
        return True
    if tactic.habitify_id:
        return int((cycle.habitify_counts or {}).get(k, 0)) >= (tactic.per_week or 1)
    return False


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


def parse_weeks(raw: str) -> list[int]:
    """``"1-4, 6, 9"`` -> ``[1, 2, 3, 4, 6, 9]``; anything outside 1..12 or
    unreadable is dropped."""
    out: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            out.update(range(max(1, lo), min(WEEKS, hi) + 1))
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= WEEKS:
                out.add(n)
    return sorted(out)


def format_weeks(weeks) -> str:
    """The inverse of :func:`parse_weeks`, collapsing runs: ``1-4, 6``."""
    weeks = sorted(set(weeks or []))
    parts, i = [], 0
    while i < len(weeks):
        j = i
        while j + 1 < len(weeks) and weeks[j + 1] == weeks[j] + 1:
            j += 1
        parts.append(str(weeks[i]) if i == j else f"{weeks[i]}-{weeks[j]}")
        i = j + 1
    return ", ".join(parts)
