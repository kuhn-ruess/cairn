"""Habitify (habitify.me) as a read only source for the 12 Week Year.

Built against Habitify's public API v2 (spec:
https://static.habitify.me/Habitify%20API%20v2.json): base
``https://api.habitify.me/v2``, the user's key in the ``X-API-Key`` header
(no "Bearer"), plain ``YYYY-MM-DD`` dates resolved in the account's own
timezone, 500 requests per minute per account. A key is made in the app under
Settings > API and needs a paid Habitify plan; a key from the older v1 API
gets 403 here.

Two calls are all this needs:

* ``GET /habits`` (paged, ``limit`` up to 100) for the picker on the plan page;
* ``GET /habits/{id}/statistics?startDate&endDate`` for the completions, whose
  ``data.dailyProgress`` lists ``{date, totalLog, status}`` per day. A day
  with ``status == "completed"`` counts; for a habit whose goal is weekly
  (``data.periodicity``), Habitify judges it by the week, not by the day:
  a goal in reps counts ``data.totalLogs``, every rep logged that week
  (:func:`week_count`), any other unit the days with a log. One request per
  mapped habit and Cairn week, so it is read week by week: a whole cycle
  costs at most 12 requests per mapped habit, a background round 2.

Writing happens only for tactics the user asked Cairn to manage
(``habitify_manage``), only on the user's confirmation after a plan save, and
only what the API offers: ``POST /habits`` creates a habit (name, type,
``startDate``, ``occurrence``, ``goal`` and an ``endCondition`` of type
``date``) and ``PUT /habits/{id}`` moves an existing one's ``startDate``, end
date and frequency. The frequency is the tactic's ``per_week``: 7 is a daily
habit, fewer is Habitify's own "N times per week" (repeat daily with a weekly
goal of N), see :func:`frequency`.
Habitify has no schedule by calendar week, so a tactic picked for weeks 1-4
and 6 runs in Habitify from the start of week 1 to the end of week 6; the
gap is said in the preview, not hidden. Archiving is never used: it cannot be
undone through the API.

The HTTP calls are :func:`_fetch` and :func:`_send`, two small functions,
so tests replace them and never reach Habitify.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from application.services import twelve_week as tw

log = logging.getLogger(__name__)

BASE_URL = "https://api.habitify.me/v2"
TIMEOUT = 15
PAGE = 100


class HabitifyError(Exception):
    """Anything that kept us from an answer; the message is for the user."""


def _fetch(path: str, api_key: str, params: dict | None = None):
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-API-Key": api_key, "Accept": "application/json",
        "User-Agent": "Cairn"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        raise HabitifyError(_http_message(exc)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HabitifyError(f"Habitify did not answer ({exc}).") from exc
    except ValueError as exc:
        raise HabitifyError("Habitify sent something that is not JSON.") from exc


def _send(method: str, path: str, api_key: str, body: dict):
    """A write. Same errors as :func:`_fetch`."""
    req = urllib.request.Request(
        BASE_URL + path, method=method,
        data=json.dumps(body).encode("utf-8"),
        headers={"X-API-Key": api_key, "Accept": "application/json",
                 "Content-Type": "application/json", "User-Agent": "Cairn"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as exc:
        raise HabitifyError(_http_message(exc)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HabitifyError(f"Habitify did not answer ({exc}).") from exc
    except ValueError as exc:
        raise HabitifyError("Habitify sent something that is not JSON.") from exc


def _http_message(exc) -> str:
    if exc.code in (401, 403):
        return ("Habitify refused the API key (HTTP %d). Make a new one in "
                "Habitify under Settings > API; it needs a paid plan." % exc.code)
    if exc.code == 429:
        return "Habitify's rate limit was hit, try again in a minute."
    try:
        body = json.loads(exc.read().decode("utf-8"))
        detail = body.get("message") or body.get("error") or ""
    except Exception:  # noqa: BLE001, the status code is enough then
        detail = ""
    return f"Habitify answered HTTP {exc.code}" + (f": {detail}" if detail else ".")


def _data(payload):
    """Lists and statistics come as ``{"data": ...}``; tolerate a bare body."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def list_habits(api_key: str, *, archived: bool = False) -> list[dict]:
    """``[{"id", "name"}]`` of the account's habits, all pages."""
    habits: list[dict] = []
    offset = 0
    while True:
        payload = _fetch("/habits", api_key, {
            "archived": "true" if archived else "false",
            "limit": PAGE, "offset": offset})
        rows = _data(payload) or []
        if not isinstance(rows, list):
            raise HabitifyError("Habitify's habit list has an unexpected shape.")
        for h in rows:
            if isinstance(h, dict) and h.get("id"):
                habits.append({"id": str(h["id"]), "name": str(h.get("name") or h["id"])})
        total = ((payload or {}).get("pagination") or {}).get("total") \
            if isinstance(payload, dict) else None
        offset += len(rows)
        if not rows or len(rows) < PAGE or (total is not None and offset >= total):
            break
    return habits


def week_stats(api_key: str, habit_id: str, first: date, last: date):
    """Habitify's statistics for ``first..last`` (both inclusive)."""
    return _fetch(f"/habits/{urllib.parse.quote(habit_id, safe='')}/statistics",
                  api_key, {"startDate": first.isoformat(),
                            "endDate": last.isoformat()})


def completed_days(api_key: str, habit_id: str, first: date, last: date) -> set[date]:
    """The days in ``first..last`` that count, see :func:`parse_completed_days`."""
    return parse_completed_days(week_stats(api_key, habit_id, first, last))


# Units in which a logged value is a number of times, not an amount.
COUNT_UNITS = {"", "rep", "reps", "time", "times", "count"}


def _counts_reps(data: dict) -> bool:
    unit = data.get("unit")
    if isinstance(unit, dict):
        unit = unit.get("symbol") or unit.get("name") or unit.get("id") or ""
    return str(unit or "").strip().lower() in COUNT_UNITS


def parse_completed_days(payload) -> set[date]:
    """The days that count, once each.

    A daily goal counts on a day Habitify has ``completed``. A weekly goal is
    completed by the week: the single days on the way there are
    "inprogress", so the days with a log are what counts."""
    data = _data(payload)
    progress = (data or {}).get("dailyProgress") if isinstance(data, dict) else None
    weekly = isinstance(data, dict) and data.get("periodicity") == "weekly"
    days: set[date] = set()
    for row in progress or []:
        if not isinstance(row, dict):
            continue
        if weekly:
            try:
                logged = float(row.get("totalLog") or 0) > 0
            except (TypeError, ValueError):
                logged = False
            if not logged or row.get("status") in ("skipped", "failed"):
                continue
        elif row.get("status") != "completed":
            continue
        try:
            days.add(date.fromisoformat(str(row.get("date", ""))[:10]))
        except ValueError:
            continue
    return days


def week_count(payload) -> int:
    """The times one week's statistics count for.

    A weekly goal in reps (how Cairn creates "N times per week") counts every
    rep: ``data.totalLogs``, the sum of all values logged in the range asked
    for. That total is used rather than adding up ``dailyProgress``, whose
    per day values for a weekly goal can be one entry for the whole week or
    the week's running total; either way a sum of them would be wrong. Any
    other habit counts its days (:func:`parse_completed_days`), which can
    only ever be too few, never too many."""
    data = _data(payload)
    if isinstance(data, dict) and data.get("periodicity") == "weekly" \
            and _counts_reps(data):
        try:
            return max(0, int(round(float(data.get("totalLogs")))))
        except (TypeError, ValueError):
            pass
    return len(parse_completed_days(payload))


def count_by_week(cycle, tid: str, days: set[date]) -> dict[str, int]:
    """``{"<tid>:<week>": n}`` for the execution weeks those days fall in."""
    counts: dict[str, int] = {}
    for d in days:
        w = tw.week_of(cycle, d)
        if 1 <= w <= tw.WEEKS:
            k = tw.key(tid, w)
            counts[k] = counts.get(k, 0) + 1
    return counts


def sync_cycle(cycle, user, *, today: date, full: bool = False) -> int | None:
    """Pull completions for every mapped tactic of ``cycle`` and save them.

    ``full`` reads from week 1 up to today; otherwise the current and the
    previous week, which is what can still change. Returns the number of
    habits read, or None when there was nothing to do (no key, nothing
    mapped, cycle not running). A failure is stored on the cycle
    (``habitify_error``) and leaves the counts it already had alone."""
    from application.services.crypto import decrypt_for_user

    key = decrypt_for_user(user, user.habitify_api_key)
    mapped = [t for t in cycle.tactics if t.habitify_id]
    if not key or not mapped:
        return None
    current = tw.week_of(cycle, today)
    if current < 1:
        return None
    last_week = min(current, tw.WEEKS)
    first_week = 1 if full else max(1, last_week - 1)
    last = min(today, tw.week_range(cycle, last_week)[1])
    weeks = set(range(first_week, last_week + 1))

    counts = {k: v for k, v in (cycle.habitify_counts or {}).items()
              if int(k.rsplit(":", 1)[1]) not in weeks}
    try:
        by_habit: dict[str, dict[int, int]] = {}
        for t in mapped:
            if t.habitify_id not in by_habit:
                by_habit[t.habitify_id] = {
                    w: week_count(week_stats(
                        key, t.habitify_id, tw.week_range(cycle, w)[0],
                        min(last, tw.week_range(cycle, w)[1])))
                    for w in sorted(weeks)}
            for w, n in by_habit[t.habitify_id].items():
                if n:
                    counts[tw.key(t.tid, w)] = n
    except HabitifyError as exc:
        cycle.habitify_error = str(exc)[:500]
        cycle.save()
        return None
    cycle.habitify_counts = counts
    cycle.habitify_error = ""
    cycle.habitify_synced_at = datetime.now(timezone.utc)
    cycle.save()
    return len(by_habit)


# ---- writing the plan into Habitify ---------------------------------------

def frequency(per_week: int) -> dict:
    """``occurrence`` and ``goal`` for a habit done ``per_week`` times a week.

    7 is every day. Anything less is how Habitify itself says "3 times per
    week": it repeats daily (any day may be the one) with a weekly goal of 3
    times. A fixed set of weekdays would be the other way, but Cairn does not
    know which days."""
    n = max(1, min(7, int(per_week or 1)))
    if n == 7:
        return {"occurrence": {"type": "daily"},
                "goal": {"periodicity": "daily", "value": 1, "unit": "rep"}}
    return {"occurrence": {"type": "daily"},
            "goal": {"periodicity": "weekly", "value": n, "unit": "rep"}}


def pending_changes(cycle) -> list[dict]:
    """What saving the plan would change in Habitify, one entry per managed
    tactic whose habit is missing or whose dates moved:
    ``{"tid", "title", "action": "create"|"update", "body", "gaps"}``.
    Pure: reads the cycle, asks nobody."""
    from application.services import twelve_week as tw

    changes = []
    for t in cycle.tactics:
        if not t.habitify_manage:
            continue
        start, end = tw.habitify_window(t, cycle)
        if not start:
            continue
        gaps = tw.gaps(t)
        per_week = max(1, min(7, t.per_week or 1))
        if not t.habitify_id:
            changes.append({
                "tid": t.tid, "title": t.title, "action": "create", "gaps": gaps,
                "per_week": per_week,
                "body": {"name": t.title, "type": "good", "startDate": start,
                         **frequency(per_week),
                         "endCondition": {"type": "date", "date": end}}})
        elif (t.habitify_start, t.habitify_end, t.habitify_per_week) \
                != (start, end, per_week):
            changes.append({
                "tid": t.tid, "title": t.habitify_name or t.title,
                "action": "update", "gaps": gaps, "per_week": per_week,
                "old": {"startDate": t.habitify_start, "end": t.habitify_end,
                        "per_week": t.habitify_per_week},
                "body": {"startDate": start, **frequency(per_week),
                         "endCondition": {"type": "date", "date": end}}})
    return changes


def apply_changes(cycle, user, changes: list[dict]) -> tuple[int, list[str]]:
    """Write ``changes`` (from :func:`pending_changes`) with the user's key.
    Each success is recorded on its tactic right away, so a failure halfway
    leaves the rest pending for the next try instead of creating twice.
    Returns (done, errors)."""
    from application.services.crypto import decrypt_for_user

    key = decrypt_for_user(user, user.habitify_api_key)
    if not key:
        return 0, ["No Habitify key is saved (Settings > Habitify)."]
    done, errors = 0, []
    for ch in changes:
        t = cycle.tactic(ch["tid"])
        if t is None:
            continue
        body = ch["body"]
        try:
            if ch["action"] == "create":
                created = _data(_send("POST", "/habits", key, body)) or {}
                hid = str(created.get("id") or "") if isinstance(created, dict) else ""
                if not hid:
                    raise HabitifyError("Habitify created the habit but sent no id back.")
                t.habitify_id, t.habitify_name = hid, body["name"]
            else:
                _send("PUT", f"/habits/{urllib.parse.quote(t.habitify_id, safe='')}",
                      key, body)
        except HabitifyError as exc:
            errors.append(f"{ch['title']}: {exc}")
            continue
        t.habitify_start = body["startDate"]
        t.habitify_end = body["endCondition"]["date"]
        t.habitify_per_week = ch["per_week"]
        cycle.save()
        done += 1
    return done, errors


# ---- background ----------------------------------------------------------

SYNC_EVERY = timedelta(hours=1)


def due_cycles(now: datetime | None = None):
    """Running cycles of users with a key whose last read is older than an
    hour. One query for the keys, one for the cycles."""
    from application.models.twelve_week import TwelveWeekYear
    from application.models.user import User

    now = now or datetime.now(timezone.utc)
    users = {u.pk: u for u in User.objects(habitify_api_key__nin=["", None])
             .only("id", "habitify_api_key", "timezone")}
    if not users:
        return []
    window = now - timedelta(days=7 * (tw.REVIEW_WEEK) + 1)
    due = []
    for c in TwelveWeekYear.objects(owner__in=list(users), start__gte=window,
                                    start__lte=now, tactics__habitify_id__gt=""
                                    ).no_dereference():
        synced = c.habitify_synced_at
        if synced and synced.tzinfo is None:
            synced = synced.replace(tzinfo=timezone.utc)
        if synced is None or now - synced >= SYNC_EVERY:
            due.append((c, users[c.owner.id]))
    return due


def sync_due(now: datetime | None = None) -> int:
    """One background round: every due cycle, current and previous week."""
    from application.services.tz import user_zone

    done = 0
    for cycle, user in due_cycles(now):
        today = datetime.now(user_zone(user)).date()
        try:
            if sync_cycle(cycle, user, today=today) is not None:
                done += 1
        except Exception:  # noqa: BLE001, one user's failure is not the round's
            log.exception("habitify sync failed for cycle %s", cycle.pk)
    return done
