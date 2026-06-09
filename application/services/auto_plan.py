"""Greedy day auto-planner for the Today "Plan" timeline.

Pure datetime logic, deliberately free of Flask/mongoengine so it can be
unit-tested in isolation. The view layer (``tasks.auto_plan``) gathers the
candidates + busy intervals and feeds plain values in here.

Strategy: place each candidate (in the order given — caller sorts by
priority/due/sort_order) into the earliest free slot at or after a moving
cursor, skipping ``busy`` intervals (calendar events + already-timed tasks)
and never spilling past ``window_end``. Non-destructive: nothing already
timed is touched; we only return where the *unplanned* candidates should go.
"""

from __future__ import annotations

from datetime import datetime, timedelta

SNAP_MIN = 15
DEFAULT_MIN = 30


def _snap_up(dt: datetime, snap: int = SNAP_MIN) -> datetime:
    """Round ``dt`` up to the next ``snap``-minute boundary."""
    if dt.second or dt.microsecond:
        dt = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    rem = dt.minute % snap
    if rem:
        dt = dt + timedelta(minutes=snap - rem)
    return dt


def _earliest(start: datetime, need: timedelta, busy_sorted, window_end, snap):
    """Earliest slot >= ``start`` of length ``need`` that clears ``busy_sorted``
    (a start-sorted list of (start, end) tuples) and fits before window_end.
    Returns the slot start, or None if it doesn't fit."""
    t = _snap_up(start, snap)
    while t + need <= window_end:
        bumped_to = None
        for bs, be in busy_sorted:
            if t < be and bs < t + need:        # overlap with this busy block
                bumped_to = be
                break
        if bumped_to is None:
            return t
        t = _snap_up(bumped_to, snap)
    return None


def plan_day(durations, busy, window_start, window_end, *, snap: int = SNAP_MIN):
    """Assign start datetimes to a list of task durations.

    Args:
        durations: minutes per candidate, in the order they should be tried.
        busy: iterable of (start_dt, end_dt) tz-aware intervals to avoid.
        window_start / window_end: tz-aware bounds of the working day.
        snap: slot granularity in minutes.

    Returns a list aligned with ``durations``: each entry is the assigned
    start datetime, or None when the candidate didn't fit before window_end.
    """
    busy_sorted = sorted(
        (s, e) for (s, e) in busy if e > window_start and s < window_end
    )
    cursor = window_start
    out = []
    for d in durations:
        need = timedelta(minutes=max(5, int(d or DEFAULT_MIN)))
        start = _earliest(cursor, need, busy_sorted, window_end, snap)
        if start is None:
            out.append(None)
            continue
        out.append(start)
        cursor = start + need          # next candidate starts after this one
    return out
