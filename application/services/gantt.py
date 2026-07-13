"""Gantt / timeline layout — turn a flat list of items into positioned bars.

Pure computation (no DB): the caller passes the already-filtered items (the
current view's task set), we work out a padded date range that spans every
``start_date``/``end_date`` plus today, and return each row with its bar's
``left``/``width`` as percentages of that range. The template just draws them.

An item may set only one of the two dates — the bar then collapses to a marker
on that day. Items with neither date still list (in the left table) with no bar.
"""
from datetime import datetime, timedelta, timezone


def _aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _day_floor(dt, tz):
    d = dt.astimezone(tz)
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_ticks(start, end, tz, pct):
    """First-of-month gridlines/labels within the range, as (label, pct)."""
    ticks = []
    cur = _day_floor(start, tz).replace(day=1)
    if cur < _day_floor(start, tz):
        cur = cur  # keep; may sit just left of range, pct clamps below
    guard = 0
    while cur <= end and guard < 240:
        guard += 1
        p = pct(cur)
        if 0 <= p <= 100:
            ticks.append({"label": cur.strftime("%b %Y"), "pct": p})
        # advance one month
        y, m = cur.year, cur.month
        cur = cur.replace(year=y + 1, month=1, day=1) if m == 12 \
            else cur.replace(month=m + 1, day=1)
    return ticks


def build_gantt(items, tz, now=None):
    """Return the template context for a timeline over ``items`` (an ordered
    iterable of Item). ``tz`` is the user's tzinfo; ``now`` overrides the clock
    (tests)."""
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
        range_start = min(bounds)
        range_end = max(bounds)
    else:
        range_start = now
        range_end = now + timedelta(days=30)
    # Always keep "today" on the canvas, then snap to whole days + pad.
    range_start = min(range_start, now)
    range_end = max(range_end, now)
    range_start = _day_floor(range_start, tz) - timedelta(days=2)
    range_end = _day_floor(range_end, tz) + timedelta(days=3)

    span = (range_end - range_start).total_seconds() or 1.0

    def pct(dt):
        return max(0.0, min(100.0, (dt - range_start).total_seconds() / span * 100.0))

    rows = []
    for it in items:
        s = _aware(getattr(it, "start_date", None))
        e = _aware(getattr(it, "end_date", None))
        bar = None
        if s or e:
            bs, be = (s or e), (e or s)
            if be < bs:
                bs, be = be, bs
            left = pct(bs)
            width = max(0.8, pct(be) - left)
            bar = {
                "left": round(left, 3),
                "width": round(width, 3),
                "point": s is None or e is None or s == e,
                "overdue": bool(e and e < now
                                and getattr(it, "status", "") not in ("done", "dropped", "completed")),
            }
        rows.append({
            "item": it,
            "start": s,
            "end": e,
            "bar": bar,
            "indent": int(getattr(it, "_gantt_indent", 0) or 0),
        })

    today_pct = pct(now) if range_start <= now <= range_end else None
    return {
        "rows": rows,
        "today_pct": None if today_pct is None else round(today_pct, 3),
        "range_start": range_start,
        "range_end": range_end,
        "ticks": _month_ticks(range_start, range_end, tz, pct),
        "has_any_dates": bool(bounds),
    }
