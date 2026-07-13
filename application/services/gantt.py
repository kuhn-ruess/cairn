"""Gantt / timeline layout — turn a flat list of items into positioned bars.

Pure computation (no DB): the caller passes the already-filtered items (the
current view's task set) and we work out the date range that spans every
``start_date``/``end_date`` plus today. Positions are returned in **days from
the range start** — the template multiplies by a fixed pixels-per-day so the
canvas is wider than the viewport and scrolls horizontally (pan across months),
and the page scrolls it to "today" on load. An item may set only one of the two
dates (bar collapses to a marker); items with neither still list, without a bar.
"""
from datetime import datetime, timedelta, timezone

PX_PER_DAY = 26  # kept in sync with the template's --gantt-ppd


def _aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _day_floor(dt, tz):
    return dt.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)


def _days(dt, origin):
    """Fractional days from ``origin`` to ``dt``."""
    return (dt - origin).total_seconds() / 86400.0


def _month_ticks(start, end, tz, origin):
    """First-of-month gridlines/labels within the range, as (label, days)."""
    ticks = []
    cur = _day_floor(start, tz).replace(day=1)
    guard = 0
    while cur <= end and guard < 240:
        guard += 1
        if cur >= start:
            ticks.append({"label": cur.strftime("%b %Y"), "left": round(_days(cur, origin), 3)})
        y, m = cur.year, cur.month
        cur = cur.replace(year=y + 1, month=1, day=1) if m == 12 \
            else cur.replace(month=m + 1, day=1)
    return ticks


def build_gantt(items, tz, now=None):
    """Return the template context for a timeline over ``items`` (ordered).
    ``tz`` is the user's tzinfo; ``now`` overrides the clock (tests)."""
    now = _aware(now) or datetime.now(timezone.utc)
    items = list(items)

    bounds = []
    for it in items:
        s = _aware(getattr(it, "start_date", None))
        e = _aware(getattr(it, "end_date", None))
        if s:
            bounds.append(s)
        if e:
            bounds.append(e)

    if bounds:
        range_start, range_end = min(bounds), max(bounds)
    else:
        range_start, range_end = now, now + timedelta(days=30)
    # Keep today on the canvas, snap to whole days, and pad generously so there
    # are months to scroll on either side (a month before the earliest bar /
    # today, three months past the latest to plan ahead).
    range_start = _day_floor(min(range_start, now), tz) - timedelta(days=31)
    range_end = _day_floor(max(range_end, now), tz) + timedelta(days=92)

    total_days = max(1.0, _days(range_end, range_start))

    rows = []
    for it in items:
        s = _aware(getattr(it, "start_date", None))
        e = _aware(getattr(it, "end_date", None))
        bar = None
        if s or e:
            bs, be = (s or e), (e or s)
            if be < bs:
                bs, be = be, bs
            left = _days(bs, range_start)
            width = max(0.6, _days(be, range_start) - left)
            bar = {
                "left": round(left, 3),
                "width": round(width, 3),
                "point": s is None or e is None or s == e,
                "overdue": bool(e and e < now and getattr(it, "status", "")
                                not in ("done", "dropped", "completed")),
            }
        rows.append({
            "item": it,
            "start": s,
            "end": e,
            "bar": bar,
            "indent": int(getattr(it, "_gantt_indent", 0) or 0),
        })

    today = round(_days(now, range_start), 3) if range_start <= now <= range_end else None
    return {
        "rows": rows,
        "today": today,
        "total_days": round(total_days, 3),
        "px_per_day": PX_PER_DAY,
        "range_start": range_start,
        "range_end": range_end,
        "ticks": _month_ticks(range_start, range_end, tz, range_start),
        "has_any_dates": bool(bounds),
    }
