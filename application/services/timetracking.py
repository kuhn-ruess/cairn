"""Service layer for the team time-tracking / billing module.

Keeps the bucket mechanics (atomic append, soft-cancel, inheritance resolution)
out of the views. See :mod:`application.models.timetracking` for the data model
and the design rationale.
"""
from __future__ import annotations

from datetime import datetime, timezone

from application.models.item import Item
from application.models.timetracking import (
    Customer,
    TimeBucket,
    TimeEntry,
    TimeTrackingProject,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

def period_for(spent_on: datetime) -> str:
    return spent_on.strftime("%Y-%m")


def add_entry(*, project: TimeTrackingProject, user, minutes: int,
              spent_on: datetime, note: str = "", source_item=None,
              billable: bool | None = None) -> TimeEntry:
    """Append a time entry to the ``(team, customer, month)`` bucket atomically.

    Freezes the project's current rate + computed amount onto the entry, and
    captures the source item only as a soft id + label snapshot."""
    team = project.team
    customer = project.customer
    period = period_for(spent_on)

    rate = project.hourly_rate_cents
    if billable is None:
        billable = True
    amount = None
    if billable and rate:
        amount = round(rate * minutes / 60)

    label = ""
    src_id = None
    src_kind = None
    if source_item is not None:
        src_id = str(source_item.pk)
        src_kind = source_item.kind
        label = (source_item.title or "")[:200]

    actor = user._get_current_object() if hasattr(user, "_get_current_object") else user
    entry = TimeEntry(
        project_id=str(project.id),
        project_name=project.name,
        user_id=str(actor.id) if actor else None,
        user_name=getattr(actor, "display_name", None) or getattr(actor, "username", "") or "",
        minutes=minutes,
        spent_on=spent_on,
        note=(note or "")[:1000],
        source_item_id=src_id,
        source_kind=src_kind,
        source_label=label,
        hourly_rate_cents=rate,
        amount_cents=amount,
        billable=bool(billable),
    )

    # One atomic upsert-and-push: creates the bucket on first write for the
    # month, then appends. Concurrent loggers each issue their own atomic
    # $push, so they never clobber one another.
    TimeBucket.objects(team=team, customer=customer, period=period).update_one(
        upsert=True,
        push__entries=entry,
        inc__total_minutes=minutes,
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


def task_time_context(user, item):
    """Everything the inspector time section needs for one task, or ``None``.

    ``None`` when the user has no team or the task's subtree isn't linked to a
    billing project — in which case the section renders nothing. Otherwise:
    ``{project, total_minutes, entries}`` where each entry is a flat dict (so the
    template never dereferences across documents)."""
    if not has_team(user):
        return None
    project = resolve_time_project(item)
    if project is None:
        return None
    team = project.team
    # The billing project may live in any team the user belongs to; only show
    # the section if they're actually a member of that team.
    if str(team.id) not in user_team_ids(user):
        return None
    customer = project.customer
    src_id = str(item.pk)
    buckets = TimeBucket.objects(team=team, customer=customer,
                                 entries__source_item_id=src_id)
    rows = []
    total = 0
    for b in buckets:
        for e in b.entries:
            if e.source_item_id != src_id or e.status != "active":
                continue
            total += e.minutes or 0
            rows.append({
                "entry_id": e.entry_id,
                "bucket_id": str(b.id),
                "minutes": e.minutes,
                "spent_on": e.spent_on,
                "note": e.note,
                "locked": e.locked,
                "billable": e.billable,
                "user_name": e.user_name,
                "amount_cents": e.amount_cents,
            })
    rows.sort(key=lambda r: r["spent_on"] or _utcnow(), reverse=True)
    return {"project": project, "total_minutes": total, "entries": rows}


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


def _months_between(d_from: date, d_to: date):
    periods, y, m = [], d_from.year, d_from.month
    while (y, m) <= (d_to.year, d_to.month):
        periods.append(f"{y:04d}-{m:02d}")
        m, y = (1, y + 1) if m == 12 else (m + 1, y)
    return periods


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
                      project_id=None, user_id=None, billable=None, locked=None):
    """Flat list of matching active entries. ``scope_ids`` (set of project-id
    strings) bounds visibility; ``None`` means unbounded (manager).

    Deref-free over the embedded entries — every field used is a snapshot — so
    a month of team activity is a handful of bucket reads, no per-entry queries.
    The customer name is dereferenced once per bucket."""
    periods = _months_between(date_from, date_to)
    q = TimeBucket.objects(team=team, period__in=periods)
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
            d = e.spent_on
            dd = d.date() if hasattr(d, "date") else d
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
            })
    return rows


def team_consumption(team, current_period: str) -> dict:
    """One deref-free pass over the team's buckets → per-project totals.

    Returns ``{project_id: {minutes, amount_cents, month_minutes,
    month_amount_cents}}`` (month_* limited to ``current_period``). Used to draw
    budget bars without a query per project."""
    out = {}
    for b in TimeBucket.objects(team=team):
        is_cur = b.period == current_period
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


def public_project_report(project):
    """Read-only hours for the customer share link — months → entries, hours
    only (never rates or amounts). Deref-free over the buckets."""
    buckets = TimeBucket.objects(team=project.team, customer=project.customer)
    pid = str(project.id)
    months = {}
    total = 0
    for b in buckets:
        for e in b.entries:
            if e.project_id != pid or e.status != "active":
                continue
            total += e.minutes or 0
            d = e.spent_on
            dd = d.date() if hasattr(d, "date") else d
            key = f"{dd.year:04d}-{dd.month:02d}"
            mo = months.setdefault(key, {
                "key": key, "label": dd.strftime("%B %Y"),
                "minutes": 0, "entries": []})
            mo["minutes"] += e.minutes or 0
            mo["entries"].append({"date": dd, "minutes": e.minutes or 0,
                                  "note": e.note or ""})
    out = []
    for key in sorted(months, reverse=True):
        mo = months[key]
        mo["entries"].sort(key=lambda r: r["date"], reverse=True)
        out.append(mo)
    return {"months": out, "total_minutes": total}


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
