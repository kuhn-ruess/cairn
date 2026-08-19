"""Service layer for the team time-tracking / billing module.

Keeps the bucket mechanics (atomic append, soft-cancel, inheritance resolution)
out of the views. See :mod:`application.models.timetracking` for the data model
and the design rationale.
"""
from __future__ import annotations

from datetime import datetime, timezone

from application.models.item import Item
from application.models.timetracking import (
    MAX_ENTRIES_PER_BUCKET,
    Customer,
    TimeBucket,
    TimeEntry,
    TimeEntryValue,
    TimeProjectField,
    TimeTrackingProject,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_oid(raw):
    """A request-supplied id → ``ObjectId``, or ``None`` when it isn't one.

    mongoengine raises ``ValidationError`` — a 500 — the moment a query gets a
    malformed pk, and every id in this module arrives from a form field or a URL
    segment. (The "Link a folder / project…" placeholder posting an empty
    ``item_id`` was exactly that.) Funnelling them through here turns a bad id
    into an ordinary "not found"."""
    from bson import ObjectId
    from bson.errors import InvalidId
    if isinstance(raw, ObjectId):
        return raw
    try:
        return ObjectId((raw or "").strip())
    except (InvalidId, TypeError, ValueError):
        return None


def local_date(dt, user=None):
    """The *calendar day* ``dt`` belongs to, in ``user``'s timezone.

    Mongo hands datetimes back in UTC, so a naive ``dt.date()`` puts anything
    logged before 02:00 CEST on the previous day — which would silently shift
    entries into the wrong day (and, at a month boundary, the wrong report
    period). Every read path that groups by day goes through here."""
    if dt is None:
        return None
    if not hasattr(dt, "hour"):  # already a date
        return dt
    from application.services.tz import user_zone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(user_zone(user)).date()


# ---------------------------------------------------------------------------
# Team resolution / gating
# ---------------------------------------------------------------------------

def primary_team(user):
    """The single team this module operates in for ``user``.

    A member's ``user.team``; otherwise the first team they admin. ``None`` for
    solo users — the module is team-only, so views gate on this being set."""
    if user is None:
        return None
    if getattr(user, "team", None):
        return user.team
    from application.models.team import Team
    return Team.objects(admin=user._get_current_object()
                        if hasattr(user, "_get_current_object") else user).first()


def has_team(user) -> bool:
    return primary_team(user) is not None


def user_teams(user) -> list:
    """Every team the user may act in for time tracking — the canonical
    participating-teams set (member + payer + co-admin + biller)."""
    from application.services.permissions import participating_teams
    return participating_teams(user)


def user_team_ids(user) -> set:
    return {str(t.pk) for t in user_teams(user)}


def sorted_user_teams(user) -> list:
    """The user's teams, by name — the order the switcher shows."""
    return sorted(user_teams(user), key=lambda t: (t.name or "").lower())


def default_team(user):
    """The team the module operates in when the URL doesn't name one.

    The user's pinned default (``User.tt_default_team``) if they still belong to
    it, otherwise the first by name. Without this, someone in two teams lands in
    whichever sorts first every single time."""
    teams = sorted_user_teams(user)
    if not teams:
        return None
    want = getattr(user, "tt_default_team", "") or ""
    for t in teams:
        if str(t.pk) == want:
            return t
    return teams[0]


# ---------------------------------------------------------------------------
# Inheritance: resolve the billing project for a Cairn item
# ---------------------------------------------------------------------------

def resolve_time_project(item) -> TimeTrackingProject | None:
    """Climb ``item``'s parents until one carries a ``time_project`` link.

    Tasks rarely set it themselves; they inherit the folder/project binding.
    Bounded + cycle-guarded like the other ancestor walks in the model."""
    seen = set()
    node = item
    depth = 0
    while node is not None and depth < 50:
        if node.pk in seen:
            break
        seen.add(node.pk)
        tp = getattr(node, "time_project", None)
        if tp:
            # Dereference defensively — a deleted/archived project shouldn't crash.
            try:
                if tp and not tp.archived:
                    return tp
            except Exception:
                return None
        node = node.parent
        depth += 1
    return None


# ---------------------------------------------------------------------------
# Logging time into the monthly bucket
# ---------------------------------------------------------------------------

def period_for(spent_on) -> str:
    """The bucket key for a moment: the day it falls on, ``"YYYY-MM-DD"``.

    Callers pass a datetime already expressed in the logging user's timezone,
    so the bucket a booking lands in is the day the user says they worked."""
    return spent_on.strftime("%Y-%m-%d")


def day_range_q(date_from, date_to, pad_days: int = 1):
    """Bucket-period bounds for a day range, as a ``(gte, lte)`` string pair.

    ``"YYYY-MM-DD"`` sorts lexicographically, so a range query beats listing
    every day. The padding covers readers in a different timezone than the
    logger: an entry can sit in the neighbouring day's bucket, and the exact
    filtering happens per entry via :func:`local_date`."""
    lo = date_from - timedelta(days=pad_days)
    hi = date_to + timedelta(days=pad_days)
    return lo.isoformat(), hi.isoformat()


def _target_part(team, customer, period: str) -> int:
    """Which part of ``(team, customer, day)`` the next entry goes into.

    Almost always ``1``. Only when the newest part has filled up does this
    return the next number, which the atomic upsert then creates. Two writers
    racing here either both land in the same part (a few entries over the soft
    cap — harmless) or both upsert the same new part (the unique index makes
    that one document, not two)."""
    newest = (TimeBucket.objects(team=team, customer=customer, period=period)
              .order_by("-part").only("part", "entry_count").first())
    if newest is None:
        return 1
    if (newest.entry_count or 0) >= MAX_ENTRIES_PER_BUCKET:
        return (newest.part or 1) + 1
    return newest.part or 1


def source_snapshot(item) -> dict:
    """The soft backlink we keep for a Cairn item: ids + a name snapshot."""
    if item is None:
        return {}
    return {
        "source_item_id": str(item.pk),
        "source_kind": item.kind,
        "source_label": (item.title or "")[:200],
    }


def add_entry(*, project: TimeTrackingProject, user, minutes: int,
              spent_on: datetime, note: str = "", source_item=None,
              source: dict | None = None, external_id: str | None = None,
              external_ref: str = "", external_url: str = "",
              custom: list | None = None, start_time: str = "",
              end_time: str = "", billable: bool | None = None) -> TimeEntry:
    """Append a time entry to the ``(team, customer, month)`` bucket atomically.

    Freezes the project's current rate + computed amount onto the entry, and
    captures the source item only as a soft id + label snapshot. ``source``
    takes a ready-made snapshot instead of a live item (the stopwatch carries
    one, so a task deleted mid-timer still logs cleanly)."""
    team = project.team
    customer = project.customer
    period = period_for(spent_on)

    rate = project.hourly_rate_cents
    if billable is None:
        billable = True
    amount = None
    if billable and rate:
        amount = round(rate * minutes / 60)

    src = source or source_snapshot(source_item)
    src_id = src.get("source_item_id")
    src_kind = src.get("source_kind")
    label = src.get("source_label", "")

    actor = user._get_current_object() if hasattr(user, "_get_current_object") else user
    entry = TimeEntry(
        project_id=str(project.id),
        project_name=project.name,
        user_id=str(actor.id) if actor else None,
        user_name=getattr(actor, "display_name", None) or getattr(actor, "username", "") or "",
        minutes=minutes,
        spent_on=spent_on,
        start_time=start_time or None,
        end_time=end_time or None,
        custom=[TimeEntryValue(**c) for c in (custom or [])],
        note=(note or "")[:1000],
        source_item_id=src_id,
        source_kind=src_kind,
        source_label=label,
        hourly_rate_cents=rate,
        amount_cents=amount,
        billable=bool(billable),
        external_id=external_id or None,
        external_ref=external_ref or None,
        external_url=external_url or None,
    )

    # One atomic upsert-and-push: creates the day's bucket on first write, then
    # appends. Concurrent loggers each issue their own atomic $push, so they
    # never clobber one another — which is exactly why a whole team can share
    # one document per customer per day.
    TimeBucket.objects(team=team, customer=customer, period=period,
                       part=_target_part(team, customer, period)).update_one(
        upsert=True,
        push__entries=entry,
        inc__total_minutes=minutes,
        inc__entry_count=1,
        set__updated_at=_utcnow(),
        set_on_insert__created_at=_utcnow(),
    )
    return entry


def parse_duration(raw: str):
    """Parse a logged duration into whole minutes, or ``None`` if unparseable.

    Accepts ``90`` (minutes), ``1:30`` (h:mm), ``1h30m`` / ``1h`` / ``30m``,
    ``1.5h`` / ``1,5h`` (decimal hours). Lenient on spaces."""
    import re
    s = (raw or "").strip().lower().replace(" ", "")
    if not s:
        return None
    m = re.fullmatch(r"(\d+):([0-5]?\d)", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)h", s)
    if m:
        return round(float(m.group(1).replace(",", ".")) * 60)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m?)?", s)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    if s.isdigit():
        return int(s)
    return None


def pickable_projects(user) -> list[dict]:
    """Every billing project the user may log to, flat + deref-free.

    Two queries total (projects ``as_pymongo`` + one customer-name lookup), so
    rendering a project picker on a task or the day sheet never costs a query
    per row. Sorted customer → project, which is also the ``<optgroup>`` order.
    """
    teams = user_teams(user)
    if not teams:
        return []
    raw = list(TimeTrackingProject.objects(
        team__in=teams, archived__ne=True
    ).only("name", "customer", "team", "hourly_rate_cents",
           "custom_fields", "time_mode").as_pymongo())
    if not raw:
        return []
    names = {c.id: c.name for c in Customer.objects(
        id__in=list({r["customer"] for r in raw})).only("id", "name")}
    rows = [{
        "id": str(r["_id"]),
        "name": r.get("name", ""),
        "customer_id": str(r["customer"]),
        "customer": names.get(r["customer"], "—"),
        "team_id": str(r["team"]),
        "hourly_rate_cents": r.get("hourly_rate_cents"),
        "mode": r.get("time_mode") or "duration",
        # Field definitions travel with the picker so the capture form can ask
        # for them the moment a project is selected, without another round trip.
        "fields": [{"key": f.get("key"), "label": f.get("label"),
                    "type": f.get("field_type") or "text",
                    "default": f.get("default") or "",
                    "options": list(f.get("options") or [])}
                   for f in (r.get("custom_fields") or [])],
    } for r in raw]
    rows.sort(key=lambda r: (r["customer"].lower(), r["name"].lower()))
    return rows


def default_project_id(user, item=None, projects: list | None = None) -> str:
    """Which billing project a fresh log/timer on ``item`` should preselect.

    Inherited from the item's subtree → else the one the user last logged to →
    else the only project that exists. Keeps "log time from a task" a single
    click for the common case where everything sits under one customer."""
    if item is not None:
        p = resolve_time_project(item)
        if p is not None and str(p.team.id) in user_team_ids(user):
            return str(p.id)
    if projects is None:
        projects = pickable_projects(user)
    ids = {p["id"] for p in projects}
    last = getattr(user, "tt_last_project", "") or ""
    if last in ids:
        return last
    if len(projects) == 1:
        return projects[0]["id"]
    return ""


def item_entries(user, item) -> list[dict]:
    """Active time entries logged *on* ``item``, newest first, deref-free.

    Scans every bucket in the user's teams that contains the backlink (indexed
    on ``entries.source_item_id``), so entries logged to different billing
    projects on the same task all show up."""
    teams = user_teams(user)
    if not teams:
        return []
    src_id = str(item.pk)
    rows = []
    for b in TimeBucket.objects(team__in=teams, entries__source_item_id=src_id):
        for e in b.entries:
            if e.source_item_id != src_id or e.status != "active":
                continue
            rows.append({
                "entry_id": e.entry_id,
                "bucket_id": str(b.id),
                "minutes": e.minutes,
                "spent_on": local_date(e.spent_on, user),
                "start_time": e.start_time or "",
                "end_time": e.end_time or "",
                "custom": entry_custom(e),
                "external_ref": e.external_ref or "",
                "external_url": e.external_url or "",
                "note": e.note,
                "locked": e.locked,
                "billable": e.billable,
                "user_name": e.user_name,
                "project_name": e.project_name,
                "amount_cents": e.amount_cents,
            })
    rows.sort(key=lambda r: r["spent_on"], reverse=True)
    return rows


def task_time_context(user, item):
    """Everything the inspector time section needs for one task, or ``None``.

    ``None`` only for users without a team — the section is otherwise **always**
    available, even when nothing in the task's subtree is linked to a billing
    project: the picker then lets you choose one on the spot (mite-style "log
    from anywhere"), and the choice is remembered as the next default.
    """
    if not has_team(user):
        return None
    inherited = resolve_time_project(item)
    if inherited is not None and str(inherited.team.id) not in user_team_ids(user):
        inherited = None
    projects = pickable_projects(user)
    rows = item_entries(user, item)
    timer = getattr(user, "running_timer", None)
    return {
        "project": inherited,
        "projects": projects,
        "default_project_id": default_project_id(user, item, projects),
        "total_minutes": sum(r["minutes"] or 0 for r in rows),
        "entries": rows,
        "timer": timer if (timer and timer.source_item_id == str(item.pk)) else None,
        "timer_elsewhere": timer if (timer and timer.source_item_id != str(item.pk)) else None,
    }


# ---------------------------------------------------------------------------
# Report: scope, period presets, aggregation (all deref-free over the buckets)
# ---------------------------------------------------------------------------

from datetime import date, timedelta  # noqa: E402
from mongoengine import Q  # noqa: E402

PERIOD_PRESETS = [
    ("this_month", "This month"),
    ("last_month", "Last month"),
    ("this_week", "This week"),
    ("last_week", "Last week"),
    ("this_year", "This year"),
    ("last_year", "Last year"),
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("custom", "Custom range"),
]


def resolve_period(preset: str, from_str: str, to_str: str, today: date):
    """Return ``(date_from, date_to)`` for a preset or a custom YYYY-MM-DD range.

    Falls back to the current month on bad/blank input."""
    def _parse(s):
        try:
            return date.fromisoformat((s or "").strip())
        except ValueError:
            return None

    if preset == "custom":
        df = _parse(from_str) or today.replace(day=1)
        dt = _parse(to_str) or today
        return (df, dt) if df <= dt else (dt, df)
    if preset == "today":
        return today, today
    if preset == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if preset == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if preset == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)
    if preset == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    if preset == "this_year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    if preset == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    # default: this_month
    first = today.replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)





def reachable_project_ids(user, team) -> set:
    """Billing-project ids (as strings) the user may see/bill.

    Team managers + billers see every project in the team. A regular member
    sees projects linked at or below any Cairn container they own or are a
    member of (full-depth subtree walk)."""
    actor = user._get_current_object() if hasattr(user, "_get_current_object") else user
    # Team managers see/bill everything. Everyone else — billers included — is
    # bounded by their subtree: a biller bills the projects they have rights to,
    # not the whole team (their billing reach == their visibility reach).
    if team.is_managed_by(actor):
        return {str(p.id) for p in TimeTrackingProject.objects(team=team).only("id")}
    container_ids, frontier = set(), [
        it.id for it in Item.objects(
            (Q(owner=actor) | Q(members=actor)) & Q(kind__in=["project", "folder"])
        ).only("id")
    ]
    while frontier:
        container_ids.update(frontier)
        frontier = [
            it.id for it in Item.objects(
                parent__in=frontier, kind__in=["project", "folder"]
            ).only("id") if it.id not in container_ids
        ]
    if not container_ids:
        return set()
    projs = Item.objects(id__in=list(container_ids)).distinct("time_project")
    return {str(p.id) for p in projs if p}


def build_report_rows(*, team, scope_ids, date_from, date_to, customer_id=None,
                      project_id=None, user_id=None, billable=None, locked=None,
                      viewer=None):
    """Flat list of matching active entries. ``scope_ids`` (set of project-id
    strings) bounds visibility; ``None`` means unbounded (manager).

    Deref-free over the embedded entries — every field used is a snapshot — so
    a month of team activity is a handful of bucket reads, no per-entry queries.
    The customer name is dereferenced once per bucket."""
    lo, hi = day_range_q(date_from, date_to)
    q = TimeBucket.objects(team=team, period__gte=lo, period__lte=hi)
    if customer_id:
        q = q.filter(customer=customer_id)
    rows = []
    for b in q:
        cust_name = None
        for e in b.entries:
            if e.status != "active":
                continue
            if scope_ids is not None and e.project_id not in scope_ids:
                continue
            if project_id and e.project_id != project_id:
                continue
            dd = local_date(e.spent_on, viewer)
            if dd < date_from or dd > date_to:
                continue
            if user_id and e.user_id != user_id:
                continue
            if billable is not None and bool(e.billable) != billable:
                continue
            if locked is not None and bool(e.locked) != locked:
                continue
            if cust_name is None:
                cust_name = b.customer.name if b.customer else "—"
            rows.append({
                "entry_id": e.entry_id,
                "bucket_id": str(b.id),
                "customer": cust_name,
                "customer_id": str(b.customer.id) if b.customer else "",
                "project_id": e.project_id,
                "project": e.project_name,
                "user_name": e.user_name,
                "user_id": e.user_id,
                "spent_on": dd,
                "minutes": e.minutes or 0,
                "amount_cents": e.amount_cents,
                "billable": bool(e.billable),
                "locked": bool(e.locked),
                "note": e.note or "",
                "service": e.service or "",
                "start_time": e.start_time or "",
                "end_time": e.end_time or "",
                "custom": entry_custom(e),
                "external_ref": e.external_ref or "",
                "external_url": e.external_url or "",
            })
    return rows


def team_consumption(team, current_period: str) -> dict:
    """One deref-free pass over the team's buckets → per-project totals.

    Returns ``{project_id: {minutes, amount_cents, month_minutes,
    month_amount_cents}}`` (month_* limited to ``current_period``). Used to draw
    budget bars without a query per project."""
    out = {}
    for b in TimeBucket.objects(team=team):
        # ``period`` is a day; the "this month" columns match on its prefix.
        is_cur = (b.period or "")[:7] == current_period
        for e in b.entries:
            if e.status != "active":
                continue
            d = out.setdefault(e.project_id, {
                "minutes": 0, "amount_cents": 0,
                "month_minutes": 0, "month_amount_cents": 0})
            d["minutes"] += e.minutes or 0
            d["amount_cents"] += e.amount_cents or 0
            if is_cur:
                d["month_minutes"] += e.minutes or 0
                d["month_amount_cents"] += e.amount_cents or 0
    return out


def budget_status(project, consumption: dict | None):
    """Budget vs. actual for one project, or ``None`` if no budget set.

    ``consumption`` is this project's entry from :func:`team_consumption`.
    For a recurring (``*_per_month``) budget the used value is the current
    month's; otherwise it's the all-time total. Money budgets compare cents,
    time budgets compare minutes."""
    if not project.budget_type or not project.budget_value:
        return None
    recurring = project.budget_type.endswith("_per_month")
    is_money = project.budget_type.startswith("cents")
    c = consumption or {}
    if is_money:
        used = c.get("month_amount_cents" if recurring else "amount_cents", 0)
    else:
        used = c.get("month_minutes" if recurring else "minutes", 0)
    limit = project.budget_value
    pct = min(100, round(used * 100 / limit)) if limit else 0
    return {
        "is_money": is_money, "recurring": recurring,
        "limit": limit, "used": used,
        "pct": pct, "raw_pct": round(used * 100 / limit) if limit else 0,
        "over": used > limit,
    }


def delete_customer(customer) -> dict:
    """Hard-delete a customer and **all** its history: every billing project,
    every monthly bucket (with all logged entries), and the GTD links pointing
    at those projects. Irreversible — there is no trash for billing data. Returns
    a count summary. Uses ``Item.all_objects`` so links on trashed items clear too."""
    team = customer.team
    projects = list(TimeTrackingProject.objects(team=team, customer=customer))
    pids = [p.id for p in projects]
    unlinked = 0
    if pids:
        unlinked = Item.all_objects(time_project__in=pids).update(
            unset__time_project=True)
    buckets = TimeBucket.objects(team=team, customer=customer)
    n_buckets = buckets.count()
    n_entries = sum(len(b.entries or []) for b in buckets)
    buckets.delete()
    TimeTrackingProject.objects(team=team, customer=customer).delete()
    customer.delete()
    return {"projects": len(pids), "buckets": n_buckets,
            "entries": n_entries, "unlinked": unlinked}


def public_project_report(project, month: str | None = None):
    """Read-only time sheet for the customer share link.

    Hours, clock times, notes and the project's custom fields — never rates or
    amounts, which is the whole point of the link. Returns one flat, sorted
    ``rows`` list for the table plus the month index that drives the filter, so
    the page and the export render from exactly the same data.

    ``month`` is ``"YYYY-MM"``; anything else (or ``None``) means every month."""
    buckets = TimeBucket.objects(team=project.team, customer=project.customer)
    pid = str(project.id)
    rows, months, total = [], {}, 0
    for b in buckets:
        for e in b.entries:
            if e.project_id != pid or e.status != "active":
                continue
            dd = local_date(e.spent_on)
            key = f"{dd.year:04d}-{dd.month:02d}"
            mo = months.setdefault(key, {"key": key, "label": dd.strftime("%B %Y"),
                                         "minutes": 0})
            mo["minutes"] += e.minutes or 0
            if month and key != month:
                continue
            total += e.minutes or 0
            rows.append({
                "date": dd,
                "minutes": e.minutes or 0,
                "start_time": e.start_time or "",
                "end_time": e.end_time or "",
                # The ticket number is useful to the customer; the deep link
                # behind it points into an internal system, so it stays home.
                "note": ((f"#{e.external_ref} " if e.external_ref else "")
                         + (e.note or "")).strip(),
                "user_name": e.user_name or "",
                "custom": {c.key: c.value for c in (e.custom or []) if c.value},
            })
    rows.sort(key=lambda r: (r["date"], r["start_time"]), reverse=True)
    # Columns come from the project's current definition, plus any key that only
    # exists on older entries (a field since removed still has to be printable).
    columns = [{"key": f.key, "label": f.label}
               for f in (project.custom_fields or [])]
    seen = {c["key"] for c in columns}
    for r in rows:
        for k in r["custom"]:
            if k not in seen:
                seen.add(k)
                columns.append({"key": k, "label": k.replace("_", " ").title()})
    return {
        "rows": rows,
        "columns": columns,
        "total_minutes": total,
        "months": [months[k] for k in sorted(months, reverse=True)],
        "month": month or "",
        "has_times": any(r["start_time"] for r in rows),
    }


def can_bill(user, team) -> bool:
    """A team manager, or a member the manager has named a biller, may settle
    (lock) time — each still bounded by their reachable project scope."""
    actor = user._get_current_object() if hasattr(user, "_get_current_object") else user
    return team.is_managed_by(actor) or actor in (team.billers or [])


def set_locked(*, team, user, items, locked: bool) -> int:
    """Lock (bill) or unlock the given ``(bucket_id, entry_id)`` pairs.

    Locking marks entries billed + frozen (records who/when). Only a biller/
    manager may lock, and only entries whose project is in their reachable scope;
    only managers may unlock. Returns how many entries actually changed."""
    actor = user._get_current_object() if hasattr(user, "_get_current_object") else user
    if locked and not can_bill(actor, team):
        return 0
    if not locked and not team.is_managed_by(actor):
        return 0
    scope = reachable_project_ids(actor, team)

    from collections import defaultdict
    by_bucket = defaultdict(list)
    for bid, eid in items:
        by_bucket[bid].append(eid)

    changed = 0
    for bid, eids in by_bucket.items():
        bucket = TimeBucket.objects(pk=bid, team=team).first()
        if not bucket:
            continue
        wanted = set(eids)
        for e in bucket.entries:
            if e.entry_id not in wanted or e.status != "active":
                continue
            if e.project_id not in scope:
                continue
            if bool(e.locked) == locked:
                continue
            res = TimeBucket.objects(pk=bid, entries__entry_id=e.entry_id).update_one(
                set__entries__S__locked=locked,
                set__entries__S__billed_at=(_utcnow() if locked else None),
                set__entries__S__billed_by=(actor if locked else None),
                set__updated_at=_utcnow(),
            )
            if res:
                changed += 1
    return changed


def cancel_entry(*, bucket: TimeBucket, entry_id: str, user) -> bool:
    """Soft-cancel an entry: never removed, just flipped to ``cancelled``.

    Refuses if the entry is ``locked`` (billed). Targets the entry by its stable
    ``entry_id`` via a positional update — no reliance on list position."""
    entry = next((e for e in bucket.entries
                  if e.entry_id == entry_id and e.status == "active"), None)
    if entry is None:
        return False
    if entry.locked:
        return False
    actor = user._get_current_object() if hasattr(user, "_get_current_object") else user
    res = TimeBucket.objects(
        pk=bucket.pk, entries__entry_id=entry_id, entries__locked=False,
    ).update_one(
        set__entries__S__status="cancelled",
        set__entries__S__cancelled_at=_utcnow(),
        set__entries__S__cancelled_by=actor,
        inc__total_minutes=-(entry.minutes or 0),
        set__updated_at=_utcnow(),
    )
    return bool(res)


# ---------------------------------------------------------------------------
# The stopwatch (one per user, server-side — mite's core capture mode)
# ---------------------------------------------------------------------------

def _actor(user):
    return user._get_current_object() if hasattr(user, "_get_current_object") else user


def project_for_user(user, project_id: str):
    """Fetch a billing project, but only if the user may log to its team."""
    pid = to_oid(project_id)
    if pid is None:
        return None
    p = TimeTrackingProject.objects(pk=pid).first()
    if p is None or str(p.team.id) not in user_team_ids(user):
        return None
    return p


def start_timer(user, *, project, source_item=None, note: str = "",
                custom: dict | None = None, billable: bool = True):
    """Start the stopwatch on ``project``. Returns ``(timer, stopped_entry)``.

    Only one timer may run: an already-running one is *stopped and logged*
    first (mite does the same), so switching tasks never loses time."""
    from application.models.timetracking import RunningTimer
    actor = _actor(user)
    stopped = stop_timer(actor)
    src = source_snapshot(source_item)
    timer = RunningTimer(
        project_id=str(project.id),
        project_name=project.name,
        customer_name=project.customer.name if project.customer else "",
        team_id=str(project.team.id),
        started_at=_utcnow(),
        note=(note or "")[:1000],
        billable=bool(billable),
        custom=dict(custom or {}),
        **src,
    )
    actor.running_timer = timer
    actor.tt_last_project = str(project.id)
    actor.save()
    return timer, stopped


def stop_timer(user):
    """Stop the running stopwatch and log it as a normal entry.

    Returns the created :class:`TimeEntry`, or ``None`` when nothing ran (or
    the billing project vanished meanwhile — then the timer is just cleared).
    The entry is dated on the day the timer *started*, in the user's timezone."""
    from application.services.tz import user_zone
    actor = _actor(user)
    timer = getattr(actor, "running_timer", None)
    if not timer:
        return None
    project = TimeTrackingProject.objects(pk=timer.project_id).first()
    entry = None
    if project is not None:
        started = timer.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        # The stopwatch knows exactly when it ran — record that as the
        # entry's clock times, for free.
        zone = user_zone(actor)
        local_start = started.astimezone(zone)
        local_end = _utcnow().astimezone(zone)
        entry = add_entry(
            project=project, user=actor, minutes=timer.elapsed_minutes(),
            spent_on=local_start,
            start_time=local_start.strftime("%H:%M"),
            end_time=local_end.strftime("%H:%M"),
            custom=timer_custom_values(project, timer.custom),
            note=timer.note, billable=timer.billable,
            source={
                "source_item_id": timer.source_item_id,
                "source_kind": timer.source_kind,
                "source_label": timer.source_label,
            },
        )
    actor.running_timer = None
    actor.save()
    return entry


def discard_timer(user) -> bool:
    """Throw the running stopwatch away without logging anything."""
    actor = _actor(user)
    if not getattr(actor, "running_timer", None):
        return False
    actor.running_timer = None
    actor.save()
    return True


# ---------------------------------------------------------------------------
# Editing an entry (mite lets you fix a logged entry until it is billed)
# ---------------------------------------------------------------------------

def may_edit_entry(user, team, entry) -> bool:
    """The person who logged it, or a team manager/biller — never once locked."""
    actor = _actor(user)
    if entry is None or entry.locked or entry.status != "active":
        return False
    return (entry.user_id == str(actor.id)
            or team.is_managed_by(actor)
            or actor in (team.billers or []))


def edit_entry(*, bucket: TimeBucket, entry_id: str, user, minutes=None,
               spent_on=None, note=None, billable=None, custom=None,
               start_time=None, end_time=None) -> bool:
    """Update a logged entry in place (duration / date / note / billable).

    Money is recomputed from the rate frozen on the entry, so editing never
    silently reprices old work at today's rate. Moving an entry into another
    month would put it in the wrong bucket — that case is handled the
    append-only way: cancel here, re-append there."""
    entry = next((e for e in bucket.entries
                  if e.entry_id == entry_id and e.status == "active"), None)
    if entry is None or entry.locked:
        return False

    new_minutes = int(minutes) if minutes else entry.minutes
    new_spent = spent_on or entry.spent_on
    new_note = entry.note if note is None else (note or "")[:1000]
    new_billable = entry.billable if billable is None else bool(billable)
    new_start = entry.start_time if start_time is None else (start_time or None)
    new_end = entry.end_time if end_time is None else (end_time or None)
    new_custom = ([TimeEntryValue(**c) for c in custom] if custom is not None
                  else list(entry.custom or []))

    # Moved to another day → re-file into that day's bucket instead of
    # corrupting this one. Append-only, so that means cancel here, add there.
    if period_for(new_spent) != bucket.period:
        project = TimeTrackingProject.objects(pk=entry.project_id).first()
        if project is None:
            return False
        if not cancel_entry(bucket=bucket, entry_id=entry_id, user=user):
            return False
        add_entry(project=project, user=user, minutes=new_minutes,
                  spent_on=new_spent, note=new_note, billable=new_billable,
                  start_time=new_start or "", end_time=new_end or "",
                  custom=[{"key": c.key, "label": c.label, "value": c.value}
                          for c in new_custom],
                  source={"source_item_id": entry.source_item_id,
                          "source_kind": entry.source_kind,
                          "source_label": entry.source_label})
        return True

    rate = entry.hourly_rate_cents
    amount = round(rate * new_minutes / 60) if (new_billable and rate) else None
    res = TimeBucket.objects(
        pk=bucket.pk, entries__entry_id=entry_id, entries__locked=False,
    ).update_one(
        set__entries__S__minutes=new_minutes,
        set__entries__S__spent_on=new_spent,
        set__entries__S__note=new_note,
        set__entries__S__billable=new_billable,
        set__entries__S__start_time=new_start,
        set__entries__S__end_time=new_end,
        set__entries__S__custom=new_custom,
        set__entries__S__amount_cents=amount,
        inc__total_minutes=new_minutes - (entry.minutes or 0),
        set__updated_at=_utcnow(),
    )
    return bool(res)


# ---------------------------------------------------------------------------
# The day sheet — mite's home screen: one day of *my* time, week at a glance
# ---------------------------------------------------------------------------

def day_sheet(user, day: date) -> dict:
    """Everything the day view shows, from a single bucket query.

    Personal by design (mite's "Zeiten erfassen" is per user; the team picture
    lives in the report). Returns the day's entries plus per-day totals for the
    surrounding week, so the strip above the list costs nothing extra."""
    actor = _actor(user)
    teams = user_teams(actor)
    week_start = day - timedelta(days=day.weekday())
    week_days = [week_start + timedelta(days=i) for i in range(7)]
    # One indexed range over the visible week (padded for timezone edges);
    # a busy day may return several parts, which just merge into the list.
    lo, hi = day_range_q(week_days[0], week_days[-1])
    uid = str(actor.id)
    week = {d: 0 for d in week_days}
    entries = []
    for b in TimeBucket.objects(team__in=teams, period__gte=lo, period__lte=hi):
        cust_name = None
        for e in b.entries:
            if e.status != "active" or e.user_id != uid:
                continue
            dd = local_date(e.spent_on, actor)
            if dd in week:
                week[dd] += e.minutes or 0
            if dd != day:
                continue
            if cust_name is None:
                cust_name = b.customer.name if b.customer else "—"
            entries.append({
                "entry_id": e.entry_id,
                "bucket_id": str(b.id),
                "customer": cust_name,
                "project_id": e.project_id,
                "project": e.project_name,
                "minutes": e.minutes or 0,
                "start_time": e.start_time or "",
                "end_time": e.end_time or "",
                "custom": entry_custom(e),
                "external_ref": e.external_ref or "",
                "external_url": e.external_url or "",
                "note": e.note or "",
                "service": e.service or "",
                "billable": bool(e.billable),
                "locked": bool(e.locked),
                "amount_cents": e.amount_cents,
                "source_item_id": e.source_item_id,
                "source_kind": e.source_kind,
                "source_label": e.source_label or "",
                "created_at": e.created_at,
            })
    entries.sort(key=lambda r: r["created_at"] or _utcnow())
    projects = pickable_projects(actor)
    total = sum(r["minutes"] for r in entries)
    billable = sum(r["minutes"] for r in entries if r["billable"])
    return {
        "day": day,
        "prev_day": day - timedelta(days=1),
        "next_day": day + timedelta(days=1),
        "entries": entries,
        "total_minutes": total,
        "billable_minutes": billable,
        "amount_cents": sum(r["amount_cents"] or 0 for r in entries),
        "week": [{"date": d, "minutes": week[d], "is_day": d == day}
                 for d in week_days],
        "week_minutes": sum(week.values()),
        "projects": projects,
        # Field definitions keyed by project, so an entry's edit form can ask
        # for exactly what that project wants without a lookup per row.
        "fields_by_project": {p["id"]: p["fields"] for p in projects},
        "default_project_id": default_project_id(actor, None),
        "timer": getattr(actor, "running_timer", None),
    }


# ---------------------------------------------------------------------------
# Importing time logged in an external system (Zammad time accounting)
# ---------------------------------------------------------------------------

def imported_entry_ids(user, item) -> set:
    """External ids already imported onto ``item`` — the dedup key for re-syncs.

    One indexed query over the backlink, so a sync asks once per ticket that
    actually has time logged and never re-appends what it already has."""
    teams = user_teams(user)
    if not teams:
        return set()
    src_id = str(item.pk)
    out = set()
    for b in TimeBucket.objects(team__in=teams, entries__source_item_id=src_id):
        for e in b.entries:
            if e.source_item_id == src_id and e.external_id:
                out.add(e.external_id)
    return out


def import_entries_for_item(*, user, item, rows) -> int:
    """Append externally-tracked time to the billing project ``item`` inherits.

    ``rows`` are dicts of ``{external_id, minutes, spent_on, note, external_ref,
    external_url}``. Returns how many were actually written: rows whose
    ``external_id`` is already on the item are skipped, so syncing the same
    ticket again is a no-op. Does nothing at all when the item's subtree isn't
    linked to a billing project — that link *is* the opt-in."""
    if not rows or not has_team(user):
        return 0
    project = resolve_time_project(item)
    if project is None or str(project.team.id) not in user_team_ids(user):
        return 0
    known = imported_entry_ids(user, item)
    written = 0
    src = source_snapshot(item)
    for r in rows:
        ext = r.get("external_id")
        minutes = int(r.get("minutes") or 0)
        if not ext or ext in known or minutes < 1:
            continue
        add_entry(project=project, user=user, minutes=minutes,
                  spent_on=r["spent_on"], note=r.get("note", ""),
                  external_ref=r.get("external_ref", ""),
                  external_url=r.get("external_url", ""),
                  source=src, external_id=ext, billable=True)
        known.add(ext)
        written += 1
    return written


def repair_imported_entries(*, user, item, note: str, external_ref: str = "",
                            external_url: str = "", dry_run: bool = False) -> int:
    """Bring entries imported from ``item`` up to today's shape.

    Early imports stored a bare "Zammad #4711" as the description and no deep
    link. This backfills the ticket's *title* as the description plus the ref
    and url, for every entry booked against this item that is missing them.
    Untouched: anything the user has since edited by hand (a description that
    is no longer the old auto-generated one), and locked entries — billed time
    is frozen, including its text.

    Idempotent by construction: once an entry carries a url it is skipped, so
    this can sit in the sync path without writing on every run."""
    teams = user_teams(user)
    if not teams:
        return 0
    src_id = str(item.pk)
    old_auto = {f"Zammad #{external_ref}", f"zammad #{external_ref}"} if external_ref else set()
    fixed = 0
    for b in TimeBucket.objects(team__in=teams, entries__source_item_id=src_id):
        for e in b.entries:
            if (e.source_item_id != src_id or e.status != "active"
                    or not (e.external_id or "").startswith("zammad:")):
                continue
            if e.external_url or e.locked:
                continue
            # Only replace a description we generated ourselves.
            new_note = note if (e.note or "") in old_auto or not e.note else e.note
            if dry_run:
                fixed += 1
                continue
            res = TimeBucket.objects(
                pk=b.pk, entries__entry_id=e.entry_id, entries__locked=False,
            ).update_one(
                set__entries__S__note=new_note,
                set__entries__S__external_ref=external_ref or None,
                set__entries__S__external_url=external_url or None,
                set__updated_at=_utcnow(),
            )
            if res:
                fixed += 1
    return fixed


# ---------------------------------------------------------------------------
# Custom fields: definition on the project, values on the entry
# ---------------------------------------------------------------------------

def field_key(label: str, taken: set) -> str:
    """A stable slug for a field label, unique within the project."""
    import re
    base = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")[:50]
    base = base or "field"
    key = base
    n = 2
    while key in taken:
        key = f"{base}_{n}"
        n += 1
    return key


def parse_project_fields(rows) -> list:
    """Build a project's field definitions from the editor's parallel lists.

    ``rows`` is an iterable of ``(key, label, field_type, default, options)``
    strings, one per row of the form. A row whose label is blank is dropped —
    that is how a field is removed. An existing ``key`` is preserved so renaming
    a label never orphans the values already booked under it."""
    out, taken = [], set()
    for key, label, field_type, default, options in rows:
        label = (label or "").strip()[:100]
        if not label:
            continue
        key = (key or "").strip()[:60] or field_key(label, taken)
        if key in taken:  # a tampered/duplicated row must not shadow another
            key = field_key(label, taken)
        taken.add(key)
        field_type = "select" if field_type == "select" else "text"
        choices = [o.strip()[:200] for o in (options or "").split(",") if o.strip()]
        if field_type == "select" and not choices:
            # A dropdown with nothing to choose from is a text field.
            field_type = "text"
        out.append(TimeProjectField(
            key=key, label=label, field_type=field_type,
            default=(default or "").strip()[:200],
            options=choices,
        ))
    return out


def custom_values(project, get, prefix: str = "cf_") -> list[dict]:
    """Read a project's custom-field values out of a submitted form.

    ``get`` is the form's ``get``. Anything the form didn't carry falls back to
    the field's default, so a one-click capture path (the play button on a task
    row) still books complete data. A ``select`` value that isn't one of the
    choices is refused and falls back to the default — the dropdown exists to
    keep those values clean."""
    out = []
    for f in (project.custom_fields or []):
        raw = (get(prefix + f.key) or "").strip()[:500]
        if f.field_type == "select" and raw not in (f.options or []):
            raw = f.default if f.default in (f.options or []) else ""
        elif not raw:
            raw = f.default or ""
        out.append({"key": f.key, "label": f.label, "value": raw})
    return out


def timer_custom_values(project, stored: dict | None) -> list[dict]:
    """The values a running timer collected → entry snapshots.

    Resolved against the project's *current* definitions at stop time, so a
    field deleted while the clock ran simply drops out and a renamed one is
    snapshotted under its new label."""
    stored = stored or {}
    return custom_values(project, lambda k: stored.get(k[3:] if k.startswith("cf_") else k))


def entry_custom(entry) -> list[dict]:
    """An entry's values as plain dicts — deref-free rows for the templates."""
    return [{"key": c.key, "label": c.label, "value": c.value}
            for c in (entry.custom or []) if c.value]


# ---------------------------------------------------------------------------
# Booking by clock time instead of duration
# ---------------------------------------------------------------------------

def parse_clock(start: str, end: str):
    """``("09:00", "10:30")`` → ``(90, "09:00", "10:30")``, else ``None``.

    Both ends must be real wall-clock times. An end before the start is read as
    crossing midnight rather than as an error — night work is real, and the
    alternative is refusing a booking the user clearly meant."""
    import re

    def _minutes(raw):
        m = re.fullmatch(r"\s*(\d{1,2})[:.]?([0-5]\d)\s*", raw or "")
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2))
        return h * 60 + mi if h < 24 else None

    a, b = _minutes(start), _minutes(end)
    if a is None or b is None:
        return None
    span = b - a
    if span <= 0:
        span += 24 * 60
    if span <= 0 or span > 24 * 60:
        return None
    return span, f"{a // 60:02d}:{a % 60:02d}", f"{b // 60:02d}:{b % 60:02d}"


def repair_all_imported_entries(*, user=None, dry_run: bool = False) -> dict:
    """Sweep the buckets and backfill every imported entry that predates the
    title + deep link.

    The sync repairs the tickets it still visits; this reaches the rest — time
    booked on tickets that have since been closed, unlinked or dropped out of
    the sync scope. Everything needed is reconstructed from the Cairn task the
    entry points at (its title, ticket number and deep link), so no Zammad
    round trip is involved. Locked (billed) entries and hand-edited
    descriptions are left alone."""
    q = TimeBucket.objects
    if user is not None:
        teams = user_teams(user)
        if not teams:
            return {"entries": 0, "buckets": 0, "orphans": 0}
        q = q.filter(team__in=teams)

    fixed = touched = orphans = 0
    for b in q:
        pending = [e for e in b.entries
                   if e.status == "active" and not e.external_url
                   and (e.external_id or "").startswith("zammad:")
                   and e.source_item_id and not e.locked]
        if not pending:
            continue
        items = {str(i.pk): i for i in Item.all_objects(
            pk__in=list({e.source_item_id for e in pending}))}
        changed = False
        for e in pending:
            item = items.get(e.source_item_id)
            if item is None:  # the task is gone — nothing to reconstruct from
                orphans += 1
                continue
            ref = item.external_ref or ""
            url = item.external_url() or ""
            if not url:
                orphans += 1
                continue
            old_auto = {f"Zammad #{ref}", f"zammad #{ref}"} if ref else set()
            note = (item.title or e.note or "")[:1000] \
                if ((e.note or "") in old_auto or not e.note) else e.note
            if dry_run:
                fixed += 1
                changed = True
                continue
            res = TimeBucket.objects(
                pk=b.pk, entries__entry_id=e.entry_id, entries__locked=False,
            ).update_one(
                set__entries__S__note=note,
                set__entries__S__external_ref=ref or None,
                set__entries__S__external_url=url,
                set__updated_at=_utcnow(),
            )
            if res:
                fixed += 1
                changed = True
        if changed:
            touched += 1
    return {"entries": fixed, "buckets": touched, "orphans": orphans}
