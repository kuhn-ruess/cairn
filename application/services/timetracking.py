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


def ref_label(ref: str) -> str:
    """How a ticket reference is written: ``#4711`` for a number, ``CHMK-417``
    for a key that already reads as one. Zammad hands out numbers, Jira keys."""
    ref = (ref or "").strip()
    if not ref:
        return ""
    return f"#{ref}" if ref.isdigit() else ref


def source_snapshot(item, user=None) -> dict:
    """The soft backlink we keep for a Cairn item: ids, a name snapshot, and
    the upstream ticket it came from.

    The reference travels with the entry rather than being looked up later,
    because the task may be gone by the time a report is drawn — and because
    ``external_url`` needs the *logging* user's integration config, which a
    later reader might not share. Works for every importer that fills it
    (Zammad numbers, Jira issue keys, Todoist), since resolving the deep link
    is the model's job, not this module's."""
    if item is None:
        return {}
    try:
        url = item.external_url(user) or ""
    except Exception:      # a half-configured integration must not block a log
        url = ""
    return {
        "source_item_id": str(item.pk),
        "source_kind": item.kind,
        "source_label": (item.title or "")[:200],
        "external_ref": (item.external_ref or "")[:60],
        "external_url": url,
    }


def add_entry(*, project: TimeTrackingProject, user, minutes: int,
              spent_on: datetime, note: str = "", source_item=None,
              source: dict | None = None, external_id: str | None = None,
              external_ref: str = "", external_url: str = "",
              external_user: str = "", actor: dict | None = None,
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

    src = source or source_snapshot(source_item, user)
    src_id = src.get("source_item_id")
    src_kind = src.get("source_kind")
    label = src.get("source_label", "")
    # An explicit reference wins (the Zammad sync knows the exact booking);
    # otherwise take whatever the task carries.
    external_ref = external_ref or src.get("external_ref", "")
    external_url = external_url or src.get("external_url", "")

    # Whose time this is. Normally the person logging it; an importer passes an
    # ``actor`` snapshot instead, because the booking may belong to a colleague
    # (see the Zammad sync) and the *author* is what the report and the
    # customer's sheet show.
    who = user._get_current_object() if hasattr(user, "_get_current_object") else user
    if actor is not None:
        # An importer that supplies an actor owns the attribution outright —
        # including "this agent has no Cairn account" (id None, name kept).
        # Falling back to the syncing user there would quietly file a
        # colleague's hours under them.
        author_id = actor.get("user_id") or None
        author_name = actor.get("user_name") or ""
    else:
        author_id = str(who.id) if who else None
        author_name = (getattr(who, "display_name", None)
                       or getattr(who, "username", "") or "")
    entry = TimeEntry(
        project_id=str(project.id),
        project_name=project.name,
        user_id=author_id,
        user_name=author_name,
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
        external_user=external_user or None,
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
    # The customer lookup doubles as the archived filter: archiving a customer
    # takes their projects out of circulation too, whether or not each project
    # carries the flag (a mite import archives customers wholesale).
    names = {c.id: c.name for c in Customer.objects(
        id__in=list({r["customer"] for r in raw})).only("id", "name", "archived")
        if not c.archived}
    raw = [r for r in raw if r["customer"] in names]
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
        cust_name = None   # dereferenced once per bucket, never per entry
        for e in b.entries:
            if e.source_item_id != src_id or e.status != "active":
                continue
            if cust_name is None:
                cust_name = b.customer.name if b.customer else ""
            rows.append({
                "entry_id": e.entry_id,
                "bucket_id": str(b.id),
                "customer": cust_name,
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
    # The inherited project's customer, read off the picker data rather than
    # dereferenced again.
    inherited_customer = ""
    if inherited is not None:
        inherited_customer = next(
            (p["customer"] for p in projects if p["id"] == str(inherited.id)), "")
    return {
        "project": inherited,
        "project_customer": inherited_customer,
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


def archived_project_ids(team) -> set:
    """Ids (as strings) of the team's archived billing projects.

    "Archived" includes *every project of an archived customer*, whether or not
    the project itself carries the flag — archiving a customer is the broader
    statement of the two, and a mite import routinely archives a customer while
    leaving its projects untouched. Missing that is what kept archived work in
    the report's project picker.

    Two projections, used to mark report rows: an archived project's *history*
    still exists and still bills, so a report can't simply pretend it isn't
    there — it can only decide whether to show it, which is a question for the
    reader, not for the query."""
    dead_customers = [c["_id"] for c in Customer.objects(
        team=team, archived=True).only("id").as_pymongo()]
    q = Q(archived=True)
    if dead_customers:
        q = q | Q(customer__in=dead_customers)
    return {str(p["_id"]) for p in TimeTrackingProject.objects(
        Q(team=team) & q).only("id").as_pymongo()}


def build_report_rows(*, team, scope_ids, date_from, date_to, customer_id=None,
                      project_id=None, user_id=None, billable=None, locked=None,
                      state="active", archived_ids=None, viewer=None):
    """Flat list of matching entries. ``scope_ids`` (set of project-id strings)
    bounds visibility; ``None`` means unbounded (manager).

    ``state`` is ``active`` (the report proper), ``cancelled`` (what was
    deleted — nothing in this module is ever really gone, so it must be
    *findable*) or ``all``. ``user_id="none"`` selects the entries with no
    Cairn account behind them: time imported for an external agent nobody has
    mapped yet, which no personal day sheet can show.

    Deref-free over the embedded entries — every field used is a snapshot — so
    a month of team activity is a handful of bucket reads, no per-entry queries.
    The customer name is dereferenced once per bucket."""
    lo, hi = day_range_q(date_from, date_to)
    q = TimeBucket.objects(team=team, period__gte=lo, period__lte=hi)
    # Which projects want the task title rendered into the description — one
    # lookup for the whole report, not one per row.
    with_source = {str(p["_id"]) for p in TimeTrackingProject.objects(
        team=team, show_source=True).only("id").as_pymongo()}
    if customer_id:
        q = q.filter(customer=customer_id)
    rows = []
    for b in q:
        cust_name = None
        for e in b.entries:
            if state == "all":
                if e.status not in ("active", "cancelled"):
                    continue
            elif e.status != state:
                continue
            if scope_ids is not None and e.project_id not in scope_ids:
                continue
            if project_id and e.project_id != project_id:
                continue
            dd = local_date(e.spent_on, viewer)
            if dd < date_from or dd > date_to:
                continue
            if user_id == "none":
                if e.user_id:
                    continue
            elif user_id and e.user_id != user_id:
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
                "external_user": e.external_user or "",
                "status": e.status,
                "archived": e.project_id in (archived_ids or ()),
                "spent_on": dd,
                "minutes": e.minutes or 0,
                "amount_cents": e.amount_cents,
                "billable": bool(e.billable),
                "locked": bool(e.locked),
                "note": e.note or "",
                "description": described(e.note, e.source_label,
                                         e.project_id in with_source),
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


def purge_project_entries(project) -> dict:
    """Delete **every** time entry booked on ``project``. Irreversible.

    The deliberate exception to the module's append-only rule, and the reason it
    is guarded in the view: a project that was set up wrong, imported twice or
    used for a trial has to be resettable without dragging its history along
    forever. Cancelled entries go too — a reset that leaves ghosts behind isn't
    one — and so do billed ones, which is exactly why the count of those is
    reported back before anyone confirms.

    Only entries of this project are removed; the buckets keep everyone else's
    time for the same customer and day, with their totals corrected."""
    pid = str(project.id)
    stats = {"entries": 0, "locked": 0, "minutes": 0, "buckets": 0}
    for b in TimeBucket.objects(team=project.team, customer=project.customer,
                                entries__project_id=pid):
        keep, dropped = [], []
        for e in b.entries:
            (dropped if e.project_id == pid else keep).append(e)
        if not dropped:
            continue
        stats["entries"] += len(dropped)
        stats["locked"] += sum(1 for e in dropped if e.locked)
        stats["minutes"] += sum(e.minutes or 0 for e in dropped
                                if e.status == "active")
        stats["buckets"] += 1
        if keep:
            b.entries = keep
            b.entry_count = len(keep)
            b.total_minutes = sum(e.minutes or 0 for e in keep
                                  if e.status == "active")
            b.updated_at = _utcnow()
            b.save()
        else:
            b.delete()   # nothing left for that customer on that day
    return stats


def cancelled_entries(*, team, viewer) -> list[dict]:
    """The team's deleted time, newest first — the trash view's contents.

    "Never delete" is the module's rule, and it is what makes cancelled entries
    feel lost: they are kept forever, counted nowhere, and visible only in the
    day sheet of whoever logged them. This gathers them in one place, scoped
    the same way editing them is (:func:`may_touch_entry`): your own, or
    everything if you manage or bill for the team. Time imported for an
    external agent belongs to nobody, so only a manager/biller sees it — which
    is also the only way it can be seen at all.

    One indexed query (``entries.status``), then a pass over the embedded
    entries: no lookup per row."""
    actor = _actor(viewer)
    privileged = team.is_managed_by(actor) or actor in (team.billers or [])
    uid = str(actor.id)
    rows = []
    for b in TimeBucket.objects(team=team, entries__status="cancelled"):
        cust_name = None
        for e in b.entries:
            if e.status != "cancelled":
                continue
            if not privileged and e.user_id != uid:
                continue
            if cust_name is None:
                cust_name = b.customer.name if b.customer else "—"
            rows.append({
                "entry_id": e.entry_id,
                "bucket_id": str(b.id),
                "customer": cust_name,
                "project": e.project_name,
                "project_id": e.project_id,
                "user_name": e.user_name or "—",
                "user_id": e.user_id or "",
                "spent_on": local_date(e.spent_on, actor),
                "minutes": e.minutes or 0,
                "note": e.note or "",
                "source_label": e.source_label or "",
                "external_ref": e.external_ref or "",
                "external_url": e.external_url or "",
                "external_id": e.external_id or "",
                "locked": bool(e.locked),
                "cancelled_at": e.cancelled_at,
                "cancelled_by": (e.cancelled_by.display_name
                                 or e.cancelled_by.username)
                if e.cancelled_by else "",
            })
    rows.sort(key=lambda r: r["cancelled_at"] or datetime.min.replace(
        tzinfo=timezone.utc), reverse=True)
    return rows


def purge_entries(*, team, pairs, viewer) -> dict:
    """Delete cancelled entries for good. Returns a count summary.

    The only way an entry ever leaves a bucket one at a time, and deliberately
    narrow: **cancelled** entries only (deleting live time is what the ✕ is
    for, and it is reversible), never locked ones, and only what the viewer may
    touch. A bucket left with no entries is dropped rather than kept as an
    empty shell."""
    out = {"purged": 0, "skipped": 0}
    by_bucket = {}
    for bucket_id, entry_id in pairs:
        by_bucket.setdefault(str(bucket_id), []).append(str(entry_id))
    for bucket_id, entry_ids in by_bucket.items():
        bucket = TimeBucket.objects(pk=to_oid(bucket_id), team=team).first()
        if bucket is None:
            out["skipped"] += len(entry_ids)
            continue
        for entry_id in entry_ids:
            entry = next((e for e in bucket.entries
                          if e.entry_id == entry_id), None)
            if entry is None or entry.locked or entry.status != "cancelled" \
                    or not may_touch_entry(viewer, team, entry,
                                           status="cancelled"):
                out["skipped"] += 1
                continue
            # Cancelling already took the minutes out of the cached total, so
            # only the count moves here.
            res = TimeBucket.objects(
                pk=bucket.pk, entries__entry_id=entry_id,
                entries__status="cancelled", entries__locked=False,
            ).update_one(
                pull__entries__entry_id=entry_id,
                inc__entry_count=-1,
                set__updated_at=_utcnow(),
            )
            if res:
                out["purged"] += 1
            else:
                out["skipped"] += 1
        bucket.reload()
        if not bucket.entries:
            bucket.delete()
    return out


def project_entry_summary(project) -> dict:
    """How much time hangs off ``project`` — what a reset would destroy."""
    pid = str(project.id)
    out = {"entries": 0, "locked": 0, "minutes": 0}
    for b in TimeBucket.objects(team=project.team, customer=project.customer,
                                entries__project_id=pid).only("entries"):
        for e in b.entries:
            if e.project_id != pid:
                continue
            out["entries"] += 1
            if e.locked:
                out["locked"] += 1
            if e.status == "active":
                out["minutes"] += e.minutes or 0
    return out


def intake_project(team, customer):
    """The project incoming external time books to for ``customer``, if any."""
    return TimeTrackingProject.objects(
        team=team, customer=customer, intake=True, archived=False).first()


def set_intake_project(project, on: bool) -> None:
    """Make ``project`` the customer's intake target (or stop being it).

    One per customer: turning it on clears the flag on the customer's other
    projects, so "where does Zammad's time land" always has exactly one answer.
    """
    if on:
        TimeTrackingProject.objects(
            team=project.team, customer=project.customer, id__ne=project.id,
        ).update(set__intake=False)
    project.intake = bool(on)
    project.save()


def move_entries(*, team, pairs, target, actor=None) -> dict:
    """Move logged time onto another billing project. Returns a count summary.

    The way out of an auto-created intake project: the hours were captured
    correctly, they are just filed under the wrong project — and possibly the
    wrong customer, because the importer met the client as a Zammad
    organisation before anyone told it that this *is* an existing customer.

    Within one customer the entry stays in its bucket and only its project
    snapshot changes. Across customers the entry has to change bucket (the
    bucket **is** the customer + day), so it is appended to the target's bucket
    for the same day first and only then pulled from the old one — the order
    that risks a duplicate rather than a hole if the process dies in between.
    Cancelled entries move too, so a project can be emptied completely; billed
    (locked) ones never move, they are frozen where they were settled.

    The frozen rate travels *to the target's* rate: the entry wasn't billed
    yet, and the whole point of the move is that this project's terms apply."""
    out = {"moved": 0, "locked": 0, "missing": 0}
    by_bucket = {}
    for bucket_id, entry_id in pairs:
        by_bucket.setdefault(str(bucket_id), []).append(str(entry_id))
    rate = target.hourly_rate_cents
    for bucket_id, entry_ids in by_bucket.items():
        bucket = TimeBucket.objects(pk=to_oid(bucket_id), team=team).first()
        if bucket is None:
            out["missing"] += len(entry_ids)
            continue
        same_customer = str(bucket.customer.id) == str(target.customer.id)
        for entry_id in entry_ids:
            entry = next((e for e in bucket.entries
                          if e.entry_id == entry_id), None)
            if entry is None:
                out["missing"] += 1
                continue
            if entry.locked:
                out["locked"] += 1
                continue
            if entry.project_id == str(target.id):
                continue
            minutes = entry.minutes or 0
            amount = (round(rate * minutes / 60)
                      if rate and entry.billable else None)
            if same_customer:
                res = TimeBucket.objects(
                    pk=bucket.pk, entries__entry_id=entry_id,
                    entries__locked=False,
                ).update_one(
                    set__entries__S__project_id=str(target.id),
                    set__entries__S__project_name=target.name,
                    set__entries__S__hourly_rate_cents=rate,
                    set__entries__S__amount_cents=amount,
                    set__updated_at=_utcnow(),
                )
                if res:
                    out["moved"] += 1
                continue
            # Different customer → different bucket document.
            moved = entry
            moved.project_id = str(target.id)
            moved.project_name = target.name
            moved.hourly_rate_cents = rate
            moved.amount_cents = amount
            active = entry.status == "active"
            TimeBucket.objects(
                team=team, customer=target.customer, period=bucket.period,
                part=_target_part(team, target.customer, bucket.period),
            ).update_one(
                upsert=True, push__entries=moved,
                inc__total_minutes=minutes if active else 0,
                inc__entry_count=1,
                set__updated_at=_utcnow(),
                set_on_insert__created_at=_utcnow(),
            )
            TimeBucket.objects(pk=bucket.pk).update_one(
                pull__entries__entry_id=entry_id,
                inc__total_minutes=-(minutes if active else 0),
                inc__entry_count=-1,
                set__updated_at=_utcnow(),
            )
            out["moved"] += 1
        # A bucket that gave up its last entry is an empty shell — drop it,
        # the same way a purge does.
        if not same_customer:
            bucket.reload()
            if not bucket.entries:
                bucket.delete()
    return out


def project_entries_for_move(project) -> list:
    """``(bucket_id, entry_id)`` for everything booked on ``project`` — the
    input to :func:`move_entries` when emptying a project wholesale."""
    pid = str(project.id)
    return [(str(b.id), e.entry_id)
            for b in TimeBucket.objects(team=project.team,
                                        customer=project.customer,
                                        entries__project_id=pid)
            for e in b.entries if e.project_id == pid]


def delete_project(project, *, purge: bool = False) -> dict:
    """Remove a billing project for good. Returns a count summary.

    Refuses while time is still booked on it unless ``purge`` is set, in which
    case that time is destroyed with it (:func:`purge_project_entries`) — the
    honest reading of "delete without a trace". GTD links pointing at the
    project are cleared, including on trashed items, so nothing keeps a
    dangling reference."""
    summary = project_entry_summary(project)
    if summary["entries"] and not purge:
        return {"deleted": False, **summary}
    purged = purge_project_entries(project) if summary["entries"] else {}
    unlinked = Item.all_objects(time_project=project).update(
        unset__time_project=True)
    project.delete()
    return {"deleted": True, "unlinked": unlinked, **summary,
            "buckets": purged.get("buckets", 0)}


def reset_team_billing(team) -> dict:
    """Delete a team's entire time-tracking data: customers, projects, buckets.

    The blunt instrument, and deliberately so: when an import produced the wrong
    shape — duplicated customers, projects under the wrong client — the honest
    repair is to throw it away and import again, not to patch record by record.
    Everything imported comes back from its source; nothing here is the only
    copy of anything except time booked by hand in Cairn, which is exactly what
    the confirmation is for.

    Clears the GTD links too (``Item.all_objects`` so trashed items lose them as
    well), or the tree would keep pointing at projects that no longer exist."""
    projects = TimeTrackingProject.objects(team=team)
    buckets = TimeBucket.objects(team=team)
    summary = {
        "customers": Customer.objects(team=team).count(),
        "projects": projects.count(),
        "buckets": buckets.count(),
        "entries": sum(len(b.entries or []) for b in buckets),
        "unlinked": Item.all_objects(
            time_project__in=[p.id for p in projects.only("id")]
        ).update(unset__time_project=True) if projects.count() else 0,
    }
    buckets.delete()
    projects.delete()
    Customer.objects(team=team).delete()
    return summary


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


def described(note: str, source_label: str, show_source: bool) -> str:
    """The description as it should *read*: the task it was logged from, then
    the note. Composed here, never stored — see ``show_source``."""
    ref = (source_label or "").strip() if show_source else ""
    note = (note or "").strip()
    if ref and note:
        return f"{ref} — {note}"
    return ref or note


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
                # The task's title joins it when the project asks for it.
                "note": ((ref_label(e.external_ref) + " " if e.external_ref else "")
                         + described(e.note, e.source_label,
                                     project.show_source)).strip(),
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


def restore_entry(*, bucket: TimeBucket, entry_id: str, user) -> bool:
    """Un-cancel an entry — the way back from a mis-click.

    Nothing was ever deleted (entries are append-only, cancelling only flips
    ``status``), so this is simply the reverse move: flip it back and put its
    minutes back into the bucket's cached total. Refuses on a locked entry and
    on one that isn't cancelled, so a double-submit can't inflate the total."""
    entry = next((e for e in bucket.entries
                  if e.entry_id == entry_id and e.status == "cancelled"), None)
    if entry is None or entry.locked:
        return False
    res = TimeBucket.objects(
        pk=bucket.pk, entries__entry_id=entry_id, entries__status="cancelled",
    ).update_one(
        set__entries__S__status="active",
        set__entries__S__cancelled_at=None,
        set__entries__S__cancelled_by=None,
        inc__total_minutes=(entry.minutes or 0),
        set__updated_at=_utcnow(),
    )
    return bool(res)


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
    src = source_snapshot(source_item, actor)
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
            external_ref=timer.external_ref or "",
            external_url=timer.external_url or "",
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

def may_touch_entry(user, team, entry, *, status="active") -> bool:
    """Who may change an entry in ``status``: the person who logged it, or a
    team manager/biller. Never once locked — billed time is frozen."""
    actor = _actor(user)
    if entry is None or entry.locked or entry.status != status:
        return False
    return (entry.user_id == str(actor.id)
            or team.is_managed_by(actor)
            or actor in (team.billers or []))


def may_edit_entry(user, team, entry) -> bool:
    """The person who logged it, or a team manager/biller — never once locked."""
    return may_touch_entry(user, team, entry)


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
    entries, removed = [], []
    for b in TimeBucket.objects(team__in=teams, period__gte=lo, period__lte=hi):
        cust_name = None
        for e in b.entries:
            if e.user_id != uid or e.status not in ("active", "cancelled"):
                continue
            dd = local_date(e.spent_on, actor)
            if e.status == "cancelled":
                # Cancelled time is kept forever, so the day it belongs to is
                # also the place to take it back.
                if dd == day and not e.locked:
                    removed.append({
                        "entry_id": e.entry_id, "bucket_id": str(b.id),
                        "minutes": e.minutes or 0, "note": e.note or "",
                        "project": e.project_name,
                        "cancelled_at": e.cancelled_at,
                    })
                continue
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
    removed.sort(key=lambda r: r["cancelled_at"] or _utcnow(), reverse=True)
    projects = pickable_projects(actor)
    total = sum(r["minutes"] for r in entries)
    billable = sum(r["minutes"] for r in entries if r["billable"])
    # Where the day's last clock-booked entry ended — the natural start for the
    # next one, so a day booked by the clock chains without retyping.
    ends = sorted(r["end_time"] for r in entries if r.get("end_time"))
    return {
        "day": day,
        "suggest_start": ends[-1] if ends else "",
        "prev_day": day - timedelta(days=1),
        "next_day": day + timedelta(days=1),
        "entries": entries,
        "removed": removed,
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


def sync_external_entries(*, user, item, rows, project=None) -> tuple[int, int]:
    """Mirror an external system's time for ``item``. Returns ``(added, fixed)``.

    Each row is ``{external_id, minutes, spent_on, note, external_ref,
    external_url, user_id, user_name, external_user}``. Rows the team already
    has are not appended again — dedup is by ``external_id`` across the **whole
    team**, not just this item, because two people syncing the same ticket keep
    their own Cairn task for it and would otherwise book the same hour twice.
    A booking the team *deleted* here counts as known too: cancelling an
    imported entry is a decision, and a sync that quietly re-added it every few
    minutes would be impossible to argue with.

    A row the team already has is instead *corrected*: description, ticket
    number, deep link and — the point of the exercise — who the time belongs
    to. That is what repairs entries imported before any of this existed, and
    keeps attribution right if someone's account is matched up later. Locked
    (billed) entries are never touched.

    ``project`` overrides where the time books; without it the item's subtree
    link is resolved, and no link means nothing happens — that link *is* the
    opt-in."""
    if not rows or not has_team(user):
        return 0, 0
    project = project or resolve_time_project(item)
    if project is None or str(project.team.id) not in user_team_ids(user):
        return 0, 0
    team = project.team

    wanted = {r["external_id"]: r for r in rows if r.get("external_id")}
    if not wanted:
        return 0, 0

    # One indexed lookup for everything this team already knows of these ids.
    seen = {}
    for b in TimeBucket.objects(team=team,
                                entries__external_id__in=list(wanted)):
        for e in b.entries:
            if e.external_id in wanted:
                seen[e.external_id] = (b, e)

    added = fixed = 0
    src = source_snapshot(item)
    for ext, r in wanted.items():
        minutes = int(r.get("minutes") or 0)
        if minutes < 1:
            continue
        hit = seen.get(ext)
        if hit is None:
            add_entry(project=project, user=user, minutes=minutes,
                      spent_on=r["spent_on"], note=r.get("note", ""),
                      external_ref=r.get("external_ref", ""),
                      external_url=r.get("external_url", ""),
                      external_user=r.get("external_user", ""),
                      actor={"user_id": r.get("user_id") or None,
                             "user_name": r.get("user_name") or ""},
                      source=src, external_id=ext, billable=True)
            added += 1
            continue
        bucket, entry = hit
        if entry.locked or entry.status != "active":
            continue
        changes = _external_corrections(entry, r)
        if changes and TimeBucket.objects(
            pk=bucket.pk, entries__entry_id=entry.entry_id, entries__locked=False,
        ).update_one(set__updated_at=_utcnow(), **changes):
            fixed += 1
    return added, fixed


def repair_imported_entries(*, user, item, note: str, external_ref: str = "",
                            external_url: str = "", dry_run: bool = False) -> int:
    """Bring entries imported from ``item`` up to today's shape.

    The complement to :func:`sync_external_entries`, which corrects whatever the
    source system still returns: this reaches the rest — a booking deleted in
    Zammad leaves a Cairn entry that no fetch will ever mention again.

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


def _external_corrections(entry, row) -> dict:
    """What an imported entry is missing or has wrong, as a positional update.

    Deliberately narrow: the fields the source system owns. A description the
    user has rewritten is left alone (only the old auto-generated form, or an
    empty one, is replaced), because their words are better than the ticket's."""
    ref = row.get("external_ref") or ""
    old_auto = {f"Zammad #{ref}", f"zammad #{ref}"} if ref else set()
    changes = {}
    note = row.get("note") or ""
    if note and ((entry.note or "") in old_auto or not entry.note):
        if entry.note != note:
            changes["set__entries__S__note"] = note
    if ref and entry.external_ref != ref:
        changes["set__entries__S__external_ref"] = ref
    url = row.get("external_url") or ""
    if url and entry.external_url != url:
        changes["set__entries__S__external_url"] = url
    who = row.get("external_user") or ""
    if who and entry.external_user != who:
        changes["set__entries__S__external_user"] = who
    # Attribution: only ever *towards* a known Cairn user, never away from one.
    uid, uname = row.get("user_id") or "", row.get("user_name") or ""
    if uid and entry.user_id != uid:
        changes["set__entries__S__user_id"] = uid
    if uname and entry.user_name != uname and (uid or not entry.user_id):
        changes["set__entries__S__user_name"] = uname
    return changes


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


def parse_options(raw: str) -> list:
    """A dropdown's choices, typed one per line.

    Commas still work — that is how they used to be entered, and a pasted list
    tends to arrive that way — but only when there are no line breaks, so a
    choice like "Beratung, vor Ort" survives being typed on its own line."""
    text = raw or ""
    parts = text.splitlines() if "\n" in text else text.split(",")
    seen, out = set(), []
    for p in parts:
        p = p.strip()[:200]
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


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
        choices = parse_options(options)
        if field_type == "select" and not choices:
            # A dropdown with nothing to choose from is a text field.
            field_type = "text"
        value = (default or "").strip()[:200]
        if field_type == "select" and value not in choices:
            # A default that isn't one of the choices could never apply — don't
            # store a setting that silently does nothing.
            value = ""
        out.append(TimeProjectField(
            key=key, label=label, field_type=field_type,
            default=value, options=choices,
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


def backfill_entry_references(*, user=None, dry_run: bool = False) -> dict:
    """Give already-booked time the ticket reference it never captured.

    References (the number, the deep link) are taken from the task at the moment
    time is logged — so everything booked before that existed shows no ticket at
    all, and the customer's sheet has nothing to print. This walks the buckets
    and fills them in from the Cairn task each entry points at, which is where
    they would have come from anyway. Works for every source the model can build
    a link for (Zammad, Jira, Todoist), not just what an importer wrote.

    **Billed entries get their reference too.** A reference says what the work
    was, not what it cost — withholding it from exactly the entries an invoice
    is built from defeats the point. Their *text* is still frozen: the
    description rewrite below only ever touches unlocked entries.

    Entries imported from Zammad additionally get the ticket's title as their
    description, if they still carry the bare "Zammad #4711" the first version of
    that import wrote (or nothing at all). A description you have edited is left
    alone.

    The counts explain themselves: ``no_source`` are entries booked without a
    task behind them (the day sheet's quick-add), which can never have a
    reference; ``no_ref`` are tasks that carry none; ``gone`` are entries whose
    task has since been deleted. Idempotent."""
    q = TimeBucket.objects
    if user is not None:
        teams = user_teams(user)
        if not teams:
            return {"entries": 0, "buckets": 0, "no_source": 0, "no_ref": 0, "gone": 0}
        q = q.filter(team__in=teams)

    out = {"entries": 0, "buckets": 0, "no_source": 0, "no_ref": 0, "gone": 0}
    for b in q:
        pending = []
        for e in b.entries:
            if e.status != "active" or e.external_ref:
                continue
            if not e.source_item_id:
                out["no_source"] += 1     # booked without a task behind it
                continue
            pending.append(e)
        if not pending:
            continue
        items = {str(i.pk): i for i in Item.all_objects(
            pk__in=list({e.source_item_id for e in pending}))}
        changed = False
        for e in pending:
            item = items.get(e.source_item_id)
            if item is None:
                out["gone"] += 1
                continue
            ref = (item.external_ref or "").strip()
            try:
                url = item.external_url() or ""
            except Exception:
                url = ""
            if not ref and not url:
                out["no_ref"] += 1        # the task carries no ticket either
                continue
            update = {}
            if ref:
                update["set__entries__S__external_ref"] = ref
            if url:
                update["set__entries__S__external_url"] = url
            # The first Zammad import wrote its own description; replace that
            # (and only that) with the ticket's title — never on billed time,
            # whose text an invoice may already quote.
            if not e.locked and (e.external_id or "").startswith("zammad:"):
                old_auto = {f"Zammad #{ref}", f"zammad #{ref}"} if ref else set()
                if (e.note or "") in old_auto or not e.note:
                    title = (item.title or "")[:1000]
                    if title and title != e.note:
                        update["set__entries__S__note"] = title
            if dry_run:
                out["entries"] += 1
                changed = True
                continue
            if TimeBucket.objects(
                pk=b.pk, entries__entry_id=e.entry_id,
            ).update_one(set__updated_at=_utcnow(), **update):
                out["entries"] += 1
                changed = True
        if changed:
            out["buckets"] += 1
    return out
