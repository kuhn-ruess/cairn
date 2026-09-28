"""Service layer for the team time-tracking / billing module.

Keeps the bucket mechanics (atomic append, soft-cancel, inheritance resolution)
out of the views. See :mod:`application.models.timetracking` for the data model
and the design rationale.
"""
from __future__ import annotations

from collections import Counter
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
        # A link belongs on a folder/project, never on a task: a task binding
        # is a one-off nobody maintains, and a thousand of them turn a billing
        # project's link list into landfill. Where a task's time books is
        # answered by the time already logged on it (default_project_id) — so
        # any binding left on a task by an older import is stepped over here
        # and the time resolves through the folder, as it would have anyway.
        tp = None if getattr(node, "kind", None) == "task" \
            else getattr(node, "time_project", None)
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
# Logging time into the day's bucket
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
              external_ids: list | None = None,
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
        external_ids=list(external_ids or []),
        external_ref=external_ref or None,
        external_url=external_url or None,
        external_user=external_user or None,
    )

    # What a person books by hand becomes their shortlist for next time.
    # Imports pass an ``actor`` and are excluded: nobody wants a thousand
    # migrated mite entries deciding what their capture bar offers.
    if actor is None and who is not None:
        remember_project(who, project)

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


RECENT_PROJECT_LIMIT = 12


def remember_project(user, project) -> None:
    """Put ``project`` at the front of the user's shortlist.

    One tiny ``$set`` on the user document, and only when the order actually
    changes — booking the same project ten times in a row costs one write."""
    pid = str(project.id)
    current = list(getattr(user, "tt_recent_projects", None) or [])
    if current[:1] == [pid]:
        return
    fresh = [pid] + [p for p in current if p != pid]
    fresh = fresh[:RECENT_PROJECT_LIMIT]
    user.tt_recent_projects = fresh
    user.__class__.objects(pk=user.pk).update_one(set__tt_recent_projects=fresh)


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


def qualified_name(name: str, ambiguous: set, team_name: str) -> str:
    """``"Acme GmbH"`` → ``"Acme GmbH · Kuhn IT"`` when the name is not unique.

    Shared by the pickers that reach across teams. The team is added only where
    it decides something — a suffix on every row would be noise on the 99% that
    are unambiguous. ``ambiguous`` holds trimmed, case-folded names —
    ``ambiguous_names`` builds it."""
    return (f"{name} · {team_name}"
            if team_name and (name or "").strip().casefold() in ambiguous
            else name)


def ambiguous_names(names) -> set:
    """The trimmed, case-folded names that occur more than once."""
    seen = Counter((n or "").strip().casefold() for n in names)
    return {n for n, count in seen.items() if count > 1}


def pickable_projects(user, *, limit: int | None = None, query: str = "",
                     keep: str = "", customer: str = "") -> list[dict]:
    """Billing projects the user may log to, flat + deref-free.

    Two queries total (projects ``as_pymongo`` + one customer-name lookup), so
    rendering a project picker on a task or the day sheet never costs a query
    per row. Sorted customer → project, which is also the ``<optgroup>`` order.

    ``limit`` returns only the most useful ones: the projects this user booked
    on most recently (``User.tt_recent_projects``), then alphabetically, plus
    ``keep`` whatever happens — the currently selected project must be in the
    list or the form would silently change it. A team with a thousand projects
    must not put a thousand ``<option>``s on a page that re-renders after every
    booking; the rest are reached by typing, which asks the server (see
    ``views.timetracking.project_search``).

    ``query`` filters by customer or project name, case-insensitively, on every
    word — "acme web" finds "Acme › Website".

    ``customer`` narrows the list to ONE customer's projects — what a container
    that already names its customer may bind (``services.customers.
    billing_choices``). ``keep`` survives that filter too: a project already
    bound must stay on offer even when it belongs to somebody else, because
    dropping it out of the picker is how a binding that carries hours
    disappears on the next save."""
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
    # A customer belongs to one team, so somebody in two teams that both
    # invoice "Acme" gets two rows reading "Acme › Support" with nothing to
    # choose between them — which is what a picker must never offer, because
    # the wrong one of two identical rows books the hours to the other team.
    # Only a name that actually appears twice carries its team.
    team_names = {str(t.pk): t.name for t in teams}
    twice = ambiguous_names(names.values())
    rows = [{
        "id": str(r["_id"]),
        "name": r.get("name", ""),
        "customer_id": str(r["customer"]),
        "customer": qualified_name(names.get(r["customer"], "—"), twice,
                                   team_names.get(str(r["team"]), "")),
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

    if customer:
        rows = [r for r in rows
                if r["customer_id"] == customer or r["id"] == keep]
    if query:
        words = query.lower().split()
        rows = [r for r in rows
                if all(w in f"{r['customer']} {r['name']}".lower() for w in words)]
    if limit is None:
        return rows

    recent = list(getattr(user, "tt_recent_projects", None) or [])
    rank = {pid: i for i, pid in enumerate(recent)}
    rows.sort(key=lambda r: (rank.get(r["id"], len(recent)),
                             r["customer"].lower(), r["name"].lower()))
    short = rows[:limit]
    if keep and not any(r["id"] == keep for r in short):
        pinned = next((r for r in rows if r["id"] == keep), None)
        if pinned is not None:
            short = [pinned] + short[:limit - 1]
    return short


# How many projects a capture bar ships to the browser. Enough that the answer
# is usually already there, few enough that a team with a thousand projects
# doesn't pay for them on every re-render — anything else is one keystroke and
# one request away (views.timetracking.project_search).
PICKER_SHORTLIST = 25


def project_fields(project_ids) -> dict:
    """``{project_id: [field definitions]}`` for the given projects.

    The edit form of an already-logged entry has to ask for that entry's own
    project's fields, and its project may well not be on the shortlist the
    picker ships. One keyed query, no matter how many entries are on screen."""
    ids = [to_oid(i) for i in set(project_ids or ()) if to_oid(i)]
    if not ids:
        return {}
    out = {}
    for r in TimeTrackingProject.objects(id__in=ids).only(
            "id", "custom_fields").as_pymongo():
        out[str(r["_id"])] = [
            {"key": f.get("key"), "label": f.get("label"),
             "type": f.get("field_type") or "text",
             "default": f.get("default") or "",
             "options": list(f.get("options") or [])}
            for f in (r.get("custom_fields") or [])]
    return out


def last_entry_project_id(user, item) -> str:
    """The billing project the newest time logged on ``item`` went to.

    This is how a task remembers where it books: not by a link stamped onto it,
    but by the time it already carries. One indexed query over
    ``entries.source_item_id`` — the same one the inspector's list uses, which
    is why ``default_project_id`` takes those rows when the caller has them."""
    teams = user_teams(user)
    if not teams:
        return ""
    src = str(item.pk)
    best_id, best_on = "", None
    for b in TimeBucket.objects(
            team__in=teams, entries__source_item_id=src).only("entries"):
        for e in b.entries:
            if e.source_item_id != src or e.status != "active":
                continue
            # Ties go to whatever was appended last — same day, later booking.
            if best_on is None or (e.spent_on and e.spent_on >= best_on):
                best_id, best_on = e.project_id, e.spent_on
    return best_id


def default_project_id(user, item=None, projects: list | None = None,
                       entries: list | None = None) -> str:
    """Which billing project a fresh log/timer on ``item`` should preselect.

    In order: where this very task booked its time last → the project its
    subtree is linked to → the one the user last logged to anywhere → the only
    project that exists. The task's own history comes first on purpose: picking
    a project while logging must not bind the task to it, and it doesn't need
    to — the entry it just wrote is the record. ``entries`` is the inspector's
    already-loaded row list; pass it and the history costs nothing.
    """
    teams = user_teams(user)
    if not teams:
        return ""
    ids = {p["id"] for p in projects} if projects is not None else None

    def usable(pid: str) -> bool:
        """Is this still a project one can book on today?"""
        if not pid or to_oid(pid) is None:
            return False
        if ids is not None:
            # A caller that already has the list saves the lookup entirely.
            return pid in ids
        hit = TimeTrackingProject.objects(
            pk=to_oid(pid), team__in=teams, archived__ne=True
        ).only("id", "customer").first()
        return hit is not None and not hit.customer.archived

    if item is not None:
        # ``entries`` given (even empty) means the caller already ran the
        # lookup — a task with no time must not pay for a second query.
        booked = ((entries[0]["project_id"] if entries else "")
                  if entries is not None else last_entry_project_id(user, item))
        if usable(booked):
            return booked
        p = resolve_time_project(item)
        if p is not None and str(p.team.id) in user_team_ids(user):
            return str(p.id)
    last = getattr(user, "tt_last_project", "") or ""
    if usable(last):
        return last
    if ids is not None:
        return projects[0]["id"] if len(projects) == 1 else ""
    only = list(TimeTrackingProject.objects(
        team__in=teams, archived__ne=True).only("id").limit(2))
    return str(only[0].id) if len(only) == 1 else ""


def item_entries(user, item) -> list[dict]:
    """Active time entries logged *on* ``item``, newest first, deref-free.

    Scans every bucket in the user's teams that contains the backlink (indexed
    on ``entries.source_item_id``), so entries logged to different billing
    projects on the same task all show up."""
    teams = user_teams(user)
    if not teams:
        return []
    src_id = str(item.pk)
    live_names = current_project_names(teams)
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
                "project_id": e.project_id,
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
                "project_name": live_names.get(e.project_id) or e.project_name,
                "amount_cents": e.amount_cents,
            })
    rows.sort(key=lambda r: r["spent_on"], reverse=True)
    return rows


def task_time_context(user, item):
    """Everything the inspector time section needs for one task, or ``None``.

    ``None`` only for users without a team — the section is otherwise **always**
    available, even when nothing in the task's subtree is linked to a billing
    project: the picker then lets you choose one on the spot (mite-style "log
    from anywhere"). The choice is never written onto the task; the entry it
    produces is what makes it the next default (``default_project_id``).
    """
    if not has_team(user):
        return None
    rows = item_entries(user, item)
    preselect = default_project_id(user, item, entries=rows)
    projects = pickable_projects(user, limit=PICKER_SHORTLIST, keep=preselect)
    timer = getattr(user, "running_timer", None)
    return {
        "projects": projects,
        "default_project_id": preselect,
        "total_minutes": sum(r["minutes"] or 0 for r in rows),
        "entries": rows,
        "timer": timer if (timer and timer.source_item_id == str(item.pk)) else None,
        "timer_elsewhere": timer if (timer and timer.source_item_id != str(item.pk)) else None,
    }


def comment_time_target(user, item) -> dict | None:
    """Where time typed into ``item``'s comment box books to, or ``None``.

    The same preselect the item's time section shows (``default_project_id``),
    resolved once so the box can NAME it: a duration field that books to a
    project nobody can see is how hours end up on the wrong invoice. ``None``
    for a user without a team or when nothing resolves — the box then offers
    no time field and the time section's picker is the way in."""
    if not has_team(user):
        return None
    pid = default_project_id(user, item)
    project = project_for_user(user, pid) if pid else None
    if project is None:
        return None
    customer = project.customer.name if project.customer else ""
    return {"id": str(project.id),
            "label": f"{customer} › {project.name}" if customer else project.name}


def quick_log_context(user, *, item=None, project_id: str = "",
                      customer_id: str = "") -> dict | None:
    """What the global "Log time" dialog (``t``) offers and preselects.

    The page the dialog was opened on says whose work this probably is: a task,
    note or project (its own history, then its subtree's billing link), a
    billing project's page, or a customer's page (that customer's project the
    user booked on most recently). The picker carries the whole shortlist
    either way — the context only decides the preselect, and ``context`` names
    what decided it, so the dialog can say why that project is selected. With
    no context that resolves it is the ordinary default (last used project).

    ``None`` for a user without a team."""
    if not has_team(user):
        return None
    rows = pickable_projects(user)
    ids = {r["id"] for r in rows}
    chosen, why = "", ""
    if item is not None:
        tp = resolve_time_project(item)
        for pid in (last_entry_project_id(user, item), str(tp.id) if tp else ""):
            if pid in ids:
                chosen, why = pid, item.title or "this item"
                break
    elif project_id in ids:
        chosen = project_id
        why = next(f"{r['customer']} › {r['name']}" for r in rows
                   if r["id"] == project_id)
    elif customer_id:
        recent = list(getattr(user, "tt_recent_projects", None) or [])
        rank = {pid: i for i, pid in enumerate(recent)}
        mine = sorted((r for r in rows if r["customer_id"] == customer_id),
                      key=lambda r: rank.get(r["id"], len(recent)))
        if mine:
            chosen, why = mine[0]["id"], mine[0]["customer"]
    if not chosen:
        chosen = default_project_id(user, None, projects=rows)
    # A source to link the entry to: only work items — a folder is where the
    # project came from, not what the time was spent on.
    source = item if item is not None and item.kind in ("task", "note") else None
    return {
        "projects": pickable_projects(user, limit=PICKER_SHORTLIST, keep=chosen),
        "default_project_id": chosen,
        "context": why,
        "source": source,
    }


def booking_default_project_id(user, booking, *, projects=None, entries=None) -> str:
    """Which billing project a meeting should be billed to, preselected.

    In order: where this very appointment already booked → the customer's
    ``intake`` project → that customer's only / first project. And then it
    STOPS: unlike a task, a booking that knows its customer must never fall back
    to "wherever you logged last", because that silently offers a different
    client's project for a call you are about to invoice. Without a customer it
    behaves like a task and takes the usual default.
    """
    teams = user_teams(user)
    if not teams:
        return ""
    ids = {p["id"] for p in projects} if projects is not None else None

    def offered(pid: str) -> bool:
        return bool(pid) and (ids is None or pid in ids)

    booked = ((entries[0]["project_id"] if entries else "")
              if entries is not None else last_entry_project_id(user, booking))
    if offered(booked):
        return booked
    cust = None
    try:
        cust = booking.customer
    except Exception:
        cust = None
    if cust is None:
        return default_project_id(user, projects=projects)
    if cust.archived:
        return ""
    rows = list(TimeTrackingProject.objects(customer=cust, archived__ne=True)
                .only("id", "name", "intake").order_by("+name"))
    for p in rows:
        if p.intake and offered(str(p.id)):
            return str(p.id)
    return str(rows[0].id) if rows and offered(str(rows[0].id)) else ""


def booking_time_context(user, booking):
    """What the bookings list needs to bill one appointment, or ``None``.

    ``None`` only for a user without a team — there is no billing without one.
    Shaped like ``task_time_context`` on purpose: the same picker, the same
    entry rows, the same cancel action, so the panel in the list is the
    inspector's time section and not a second way of doing this.
    """
    if not has_team(user):
        return None
    rows = item_entries(user, booking)
    projects = pickable_projects(user, limit=PICKER_SHORTLIST)
    preselect = booking_default_project_id(user, booking, entries=rows)
    if preselect and not any(p["id"] == preselect for p in projects):
        # The right answer must be IN the shortlist or the form would quietly
        # bill somewhere else — same rule as ``pickable_projects(keep=…)``.
        projects = pickable_projects(user, limit=PICKER_SHORTLIST, keep=preselect)
    return {
        "projects": projects,
        "default_project_id": preselect,
        "total_minutes": sum(r["minutes"] or 0 for r in rows),
        "entries": rows,
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


MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def monthly_breakdown(rows, date_from: date, date_to: date) -> dict:
    """The report's rows pivoted into one column per calendar month.

    Pure: takes the rows ``build_report_rows`` produced (already filtered) and
    the period, returns customer → project rows with minutes per month. Every
    month the period touches gets a column, and a month without time reads 0
    rather than going missing — "February: nothing" is an answer the reader
    asked for, a skipped column is one they have to notice."""
    months = []
    y, m = date_from.year, date_from.month
    while (y, m) <= (date_to.year, date_to.month):
        months.append({"key": (y, m), "label": f"{MONTH_ABBR[m - 1]} {y}"})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    at = {mo["key"]: i for i, mo in enumerate(months)}
    n = len(months)

    customers: dict = {}
    col_totals = [0] * n
    for r in rows:
        i = at.get((r["spent_on"].year, r["spent_on"].month))
        if i is None:
            continue
        c = customers.setdefault(r["customer"], {
            "name": r["customer"], "id": r["customer_id"],
            "minutes": [0] * n, "total": 0, "projects": {}})
        p = c["projects"].setdefault(r["project_id"], {
            "name": r["project"], "id": r["project_id"],
            "minutes": [0] * n, "total": 0})
        for bucket in (c, p):
            bucket["minutes"][i] += r["minutes"]
            bucket["total"] += r["minutes"]
        col_totals[i] += r["minutes"]

    out = []
    for c in sorted(customers.values(), key=lambda x: (x["name"] or "").lower()):
        projects = sorted(c["projects"].values(),
                          key=lambda x: (x["name"] or "").lower())
        out.append({**c, "projects": projects})
    return {"months": months, "customers": out,
            "totals": col_totals, "total": sum(col_totals)}





def reachable_project_ids(user, team) -> set:
    """Billing-project ids (as strings) the user may see/bill.

    Team managers + billers see every project in the team. A regular member
    sees projects linked at or below any Cairn container they own or are a
    member of (full-depth subtree walk).

    A biller is deliberately *not* bounded by their subtree. Billing is the
    whole team's books: the person who settles the month is typically in the
    office, owns no GTD containers at all, and a subtree scope leaves them with
    an empty report and nothing to bill — the role would name a right it never
    grants. They still can't *manage* (edit projects, unlock billed time); that
    stays with the managers."""
    actor = user._get_current_object() if hasattr(user, "_get_current_object") else user
    # Managers and billers see/bill everything in the team; everyone else is
    # bounded by the containers they own or are a member of.
    if team.is_managed_by(actor) or actor in (team.billers or []):
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


def current_project_names(teams) -> dict:
    """``{project id: what that project is called now}`` — for display only.

    Every entry carries the project's name *as it read when the time was
    booked*. That snapshot is what keeps a sheet readable after the project was
    deleted, and it stays. But while the project is still there its own name is
    the true one: renaming "Synced: Beratung" to "Beratung" has to reach the
    report, the export, the day sheet and the trash — not only the management
    list, which reads the project directly and was therefore the one place that
    ever looked right.

    One projection per read path, never a lookup per row. Every display path
    goes through here and falls back to the snapshot, so an entry whose project
    is gone still says what it was booked on."""
    if not isinstance(teams, (list, tuple, set)):
        teams = [teams] if teams else []
    teams = list(teams)
    if not teams:
        return {}
    return {str(p["_id"]): p["name"]
            for p in TimeTrackingProject.objects(team__in=teams)
            .only("id", "name").as_pymongo() if p.get("name")}


def build_report_rows(*, team, scope_ids, date_from, date_to, customer_id=None,
                      project_id=None, user_id=None, billable=None, locked=None,
                      state="active", archived_ids=None, viewer=None):
    """Flat list of matching entries. ``scope_ids`` (set of project-id strings)
    bounds visibility; ``None`` means unbounded (manager).

    ``state`` is ``active`` (the report proper), ``cancelled`` (what was
    deleted — nothing in this module is ever really gone, so it must be
    *findable*) or ``all``. ``user_id="none"`` selects the entries with no
    Cairn account behind them: time imported for an external agent nobody has
    mapped yet, which no personal day sheet can show. (The report view leaves
    ``user_id`` unset and filters afterwards, so its picker can list the
    authors the result actually contains — see ``_grouped_report``.)

    Deref-free over the embedded entries — every field used is a snapshot — so
    a month of team activity is a handful of bucket reads, no per-entry queries.
    The customer name is dereferenced once per bucket."""
    lo, hi = day_range_q(date_from, date_to)
    q = TimeBucket.objects(team=team, period__gte=lo, period__lte=hi)
    # Which projects want the task title rendered into the description — one
    # lookup for the whole report, not one per row.
    with_source = {str(p["_id"]) for p in TimeTrackingProject.objects(
        team=team, show_source=True).only("id").as_pymongo()}
    # The snapshot on the entry is the fallback, not the answer — see
    # current_project_names.
    live_names = current_project_names(team)
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
                "project": live_names.get(e.project_id) or e.project_name,
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
                # Kept beside the composed description so the export can give
                # the task a column of its own instead (views._report_cell) —
                # the same choice the customer's sheet offers.
                "source_label": e.source_label or "",
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


def set_custom_value(*, team, pairs, key: str, label: str, value: str,
                     viewer) -> dict:
    """Set one custom field on many entries at once. Returns a count summary.

    The field is defined per project, so an entry booked on a project that
    doesn't have it is skipped rather than given a value nothing will ever show
    — reported, not silently dropped. Billed entries stay frozen. An empty
    ``value`` removes the field from the entry, which is how a value set by
    mistake is taken back."""
    out = {"changed": 0, "skipped": 0, "locked": 0}
    by_bucket = {}
    for bucket_id, entry_id in pairs:
        by_bucket.setdefault(str(bucket_id), []).append(str(entry_id))
    # Which projects actually define this field — one lookup for the whole run.
    ok_projects = {str(p["_id"]) for p in TimeTrackingProject.objects(
        team=team, custom_fields__key=key).only("id").as_pymongo()}
    for bucket_id, entry_ids in by_bucket.items():
        bucket = TimeBucket.objects(pk=to_oid(bucket_id), team=team).first()
        if bucket is None:
            out["skipped"] += len(entry_ids)
            continue
        for entry_id in entry_ids:
            entry = next((e for e in bucket.entries
                          if e.entry_id == entry_id), None)
            if entry is None or entry.status != "active":
                out["skipped"] += 1
                continue
            if entry.locked or not may_edit_entry(viewer, team, entry):
                out["locked"] += 1
                continue
            if entry.project_id not in ok_projects:
                out["skipped"] += 1
                continue
            values = [c for c in (entry.custom or []) if c.key != key]
            if value:
                values.append(TimeEntryValue(key=key, label=label, value=value))
            res = TimeBucket.objects(
                pk=bucket.pk,
                entries__match={"entry_id": entry_id, "locked": {"$ne": True}},
            ).update_one(set__entries__S__custom=values,
                         set__updated_at=_utcnow())
            if res:
                out["changed"] += 1
            else:
                out["skipped"] += 1
    return out


def people_in_time(team) -> list[dict]:
    """Everyone who appears as the author of this team's time, matched or not.

    Built from the entries themselves rather than from the roster, because the
    roster only knows people an *importer* met: time can also carry a bare name
    from an import that ran before anyone was matched. Each row is one identity
    — a source key when the entry has one, otherwise the name it was booked
    under — with what it holds and whether a Cairn account is behind it.

    One pass over the team's buckets, which is the same cost as the report."""
    rows: dict = {}
    for b in TimeBucket.objects(team=team).only("entries"):
        for e in b.entries:
            if e.status != "active":
                continue
            key = e.external_user or f"name:{e.user_name or '—'}"
            row = rows.setdefault(key, {
                "key": key,
                "external_user": e.external_user or "",
                "name": e.user_name or "—",
                "user_id": e.user_id or "",
                "entries": 0, "minutes": 0, "locked": 0,
                "source": (e.external_id or "").split(":")[0] if e.external_id else "",
                "first": None, "last": None,
            })
            row["entries"] += 1
            row["minutes"] += e.minutes or 0
            if e.locked:
                row["locked"] += 1
            # A single identity can hold a mix if it was mapped halfway through;
            # "has an account" is true only when every entry of it does.
            if not e.user_id:
                row["user_id"] = ""
            day = local_date(e.spent_on)
            row["first"] = min(row["first"] or day, day)
            row["last"] = max(row["last"] or day, day)
    out = sorted(rows.values(), key=lambda r: (bool(r["user_id"]), -r["minutes"]))
    return out


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
    live_names = current_project_names(team)
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
                "project": live_names.get(e.project_id) or e.project_name,
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
                pk=bucket.pk,
                entries__match={"entry_id": entry_id, "status": "cancelled",
                                "locked": {"$ne": True}},
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
                    pk=bucket.pk,
                    entries__match={"entry_id": entry_id, "locked": {"$ne": True}},
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


# Prefixes of the importers that book into an intake project. Time typed here,
# and history imported from elsewhere, is on that project because somebody put
# it there — only what an importer filed by fallback is "unassigned".
INTAKE_SOURCES = ("zammad:",)


def unassigned_groups(team) -> list:
    """Time sitting in an intake project, grouped by the ticket it came from.

    An intake project is where imported time lands when nothing said where it
    belongs — the ticket's Cairn subtree isn't linked to a billing project, so
    the importer files it under the customer it recognised and leaves the
    question open. Which is right: it keeps the hours, and asking is better
    than guessing. But nobody sees the question, and the pile grows.

    One row per source item (the ticket, normally), because assigning happens
    per ticket, not per booking: how many entries, how many minutes, which days,
    and — the part that stops it coming back — the nearest folder above the
    ticket, which is what the next sync would resolve through if it were linked.
    """
    from mongoengine import DoesNotExist

    intakes = {str(p.id): p for p in TimeTrackingProject.objects(
        team=team, intake=True, archived=False)}
    if not intakes:
        return []

    groups: dict = {}
    for bucket in TimeBucket.objects(team=team,
                                     entries__project_id__in=list(intakes)):
        try:
            customer = bucket.customer
            customer.name
        except DoesNotExist:
            # A bucket whose customer was deleted is broken beyond what this
            # page can do about it — its entries can't be moved either (the
            # bucket *is* the customer + day). Leave it out rather than take
            # the page down with it.
            continue
        for e in bucket.entries:
            if e.project_id not in intakes or e.status != "active":
                continue
            # Only what an importer filed here for lack of anywhere better.
            # A customer's one long-standing project can carry the intake flag
            # (the import reuses it rather than inventing a second project), and
            # everything ever booked on it is not suddenly unassigned.
            if not (e.external_id or "").startswith(INTAKE_SOURCES):
                continue
            key = (e.project_id, e.source_item_id or "", e.external_ref or "")
            row = groups.get(key)
            if row is None:
                row = groups[key] = {
                    "project": intakes[e.project_id],
                    "customer": customer,
                    "item_id": e.source_item_id or "",
                    "label": e.source_label or e.note or "—",
                    "external_ref": e.external_ref or "",
                    "external_url": e.external_url or "",
                    "entries": 0, "minutes": 0, "locked": 0,
                    "first": bucket.period, "last": bucket.period,
                    "pairs": [],
                }
            row["entries"] += 1
            row["minutes"] += e.minutes or 0
            row["first"] = min(row["first"], bucket.period)
            row["last"] = max(row["last"], bucket.period)
            if e.locked:
                row["locked"] += 1
            else:
                row["pairs"].append((str(bucket.id), e.entry_id))
    rows = sorted(groups.values(),
                  key=lambda r: (-r["minutes"], r["label"].lower()))
    _attach_containers(rows)
    # Time whose own subtree points at the very project it sits on is filed,
    # not waiting: the import reuses a customer's single project as its intake
    # target, so "sits on an intake project" alone says nothing.
    return [r for r in rows
            if not (r["resolved"] is not None
                    and str(r["resolved"].id) == str(r["project"].id))]


def _attach_containers(rows) -> None:
    """Add ``container`` and ``resolved`` for each row's source task.

    ``container`` is the nearest folder/project above the task — linking *that*
    is what makes the next sync book straight to the right project instead of
    the intake pile, so the page can offer it in the same click.

    ``resolved`` is what the tree already answers for that task, and it is the
    difference between "nobody has said where this belongs" and "it is exactly
    where it belongs". A customer's single project is flagged as its intake
    target by the import (rather than a second project being invented), so time
    that landed there correctly, through a link somebody made, sits on an
    intake project too — and would otherwise be listed as unassigned while
    being nothing of the sort."""
    from application.models.item import Item

    from bson import ObjectId
    from bson.errors import InvalidId

    def _oid(value):
        try:
            return ObjectId(value)
        except (InvalidId, TypeError):
            # Not every source id is a Cairn item — an importer may stamp its
            # own reference here, and asking Mongo about it raises.
            return None

    ids = [o for o in (_oid(r["item_id"]) for r in rows if r["item_id"]) if o]
    if not ids:
        for r in rows:
            r["container"] = r["resolved"] = None
        return
    tasks = {str(t.pk): t for t in Item.all_objects(pk__in=ids)}
    for r in rows:
        task = tasks.get(r["item_id"])
        r["resolved"] = resolve_time_project(task) if task is not None else None
        node = task.parent if task is not None else None
        depth = 0
        while node is not None and node.kind not in ("project", "folder") \
                and depth < 50:
            node = node.parent
            depth += 1
        r["container"] = node


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
    every day's bucket (with all logged entries), and the GTD links pointing
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


def duplicate_customers(team) -> list[dict]:
    """The team's customers that look like one client filed twice, grouped.

    What an importer leaves behind when it meets a client under a key it does
    not recognise: the company is already here as ``mite:41``, Zammad offers it
    as ``zammad:org:7``, and a second record is opened on it. Both importers
    adopt by name now, so this finds precisely the records they would no longer
    create — the comparison is the same trimmed, case-folded name they adopt by.

    Each record carries what the decision actually needs: the keys it answers
    to, its projects (and which one takes imported time), and how much time
    hangs under it. The one with the history is normally the one to keep, so
    the heaviest comes first in each group.

    Three reads for the whole team, all deref-free — this is offered to a
    person staring at a list of customers, not to a loop."""
    groups = {}
    for c in Customer.objects(team=team):
        groups.setdefault((c.name or "").strip().casefold(), []).append(c)
    groups = {k: v for k, v in groups.items() if len(v) > 1}
    if not groups:
        return []
    wanted = {c.id for rows in groups.values() for c in rows}

    projects = {}
    for p in TimeTrackingProject.objects(team=team).only(
            "id", "customer", "name", "intake", "archived").as_pymongo():
        if p.get("customer") in wanted:
            projects.setdefault(p["customer"], []).append(p)

    minutes, counts = {}, {}
    for b in TimeBucket.objects(team=team).only(
            "customer", "entries.minutes", "entries.status").as_pymongo():
        cid = b.get("customer")
        if cid not in wanted:
            continue
        for e in b.get("entries") or []:
            counts[cid] = counts.get(cid, 0) + 1
            if (e.get("status") or "active") == "active":
                minutes[cid] = minutes.get(cid, 0) + (e.get("minutes") or 0)

    out = []
    for key in sorted(groups):
        rows = []
        for c in groups[key]:
            mine = projects.get(c.id) or []
            rows.append({
                "id": str(c.id),
                "name": c.name,
                "archived": bool(c.archived),
                "external_id": c.external_id or None,
                "external_ids": list(c.external_ids or []),
                "minutes": minutes.get(c.id, 0),
                "entries": counts.get(c.id, 0),
                "projects": [{"id": str(p["_id"]), "name": p.get("name") or "",
                              "intake": bool(p.get("intake")),
                              "archived": bool(p.get("archived"))}
                             for p in sorted(mine, key=lambda r: (r.get("name") or "").lower())],
            })
        # Heaviest first: that is the record the others should be merged into.
        rows.sort(key=lambda r: (-r["minutes"], -r["entries"], r["name"]))
        out.append({"name": groups[key][0].name, "customers": rows})
    return out


def _append_entries(team, customer, period: str, entries: list) -> None:
    """Append a batch of already-built entries to ``(team, customer, day)``.

    One write per bucket rather than one per entry: a merge moves a whole day
    at a time and the entries are not being raced for — they came out of a
    document that is about to be deleted. The part is re-read for each chunk
    because filling one is exactly what opens the next."""
    entries = list(entries)
    while entries:
        newest = (TimeBucket.objects(team=team, customer=customer, period=period)
                  .order_by("-part").only("part", "entry_count").first())
        part = (newest.part or 1) if newest is not None else 1
        used = (newest.entry_count or 0) if newest is not None else 0
        if used >= MAX_ENTRIES_PER_BUCKET:
            part, used = part + 1, 0
        room = MAX_ENTRIES_PER_BUCKET - used
        chunk, entries = entries[:room], entries[room:]
        TimeBucket.objects(
            team=team, customer=customer, period=period, part=part,
        ).update_one(
            upsert=True,
            push_all__entries=chunk,
            inc__total_minutes=sum(e.minutes or 0 for e in chunk
                                   if e.status == "active"),
            inc__entry_count=len(chunk),
            set__updated_at=_utcnow(),
            set_on_insert__created_at=_utcnow(),
        )


def merge_customers(source, target, *, fold_projects: bool = True) -> dict:
    """Fold ``source`` into ``target`` and delete it. Returns a count summary.

    Two records for one client is what an importer leaves behind when it meets
    them under a key it did not recognise yet — the client is in Cairn from
    mite as ``mite:41``, Zammad offers them as ``zammad:org:7``, and a second
    file is opened on the same company. Both importers adopt by name now, but
    nothing merges what they already split, and a customer cannot simply be
    deleted: the hours are underneath it.

    Three things move, in the order that a crash between them leaves work to
    redo rather than time destroyed:

    * **Projects.** One of the same name on the target *is* the same project:
      its entries are re-pointed at the survivor and the emptied duplicate goes
      (``fold_projects``). Every other project is re-parented, which leaves its
      entries' ``project_id`` snapshot correct without touching a single one of
      them. Only one project can be the customer's intake, so an incoming one
      yields to the intake the target already has.
    * **Entries.** The bucket *is* the customer plus the day, so each source
      bucket's entries are appended to the target's bucket for that day and the
      empty shell is dropped. The rate is **not** re-frozen the way a project
      move re-freezes it: the project travelled with the entry, so the terms it
      was captured under still apply.
    * **Identity.** Every key the source answered to moves onto the target, so
      the next import goes straight there instead of re-creating what was just
      merged. Billing details only fill *gaps* — the target's own values win.

    **Nothing is counted twice.** An entry whose external key the target
    already carries is the same booking imported under both records; it moves
    with the rest (this module does not delete logged time) but arrives
    cancelled, so it is in the trash to look at instead of in the total. One
    already billed is the exception — a locked entry is frozen and stays
    active, counted in ``duplicates_kept`` for a human to settle."""
    if str(source.id) == str(target.id):
        raise ValueError("A customer cannot be merged into itself.")
    team = source.team
    if str(team.id) != str(target.team.id):
        raise ValueError("Customers of different teams cannot be merged.")

    out = {"projects_moved": 0, "projects_folded": 0, "entries_moved": 0,
           "duplicates_cancelled": 0, "duplicates_kept": 0, "relinked": 0}

    # --- projects ----------------------------------------------------------
    # Fold only into a live project: folding into an archived twin would file
    # the time somewhere the pickers no longer offer.
    by_name = {}
    for p in TimeTrackingProject.objects(team=team, customer=target,
                                         archived=False):
        by_name.setdefault((p.name or "").strip().lower(), p)
    has_intake = intake_project(team, target) is not None
    survivors = {}  # id of a folded project -> the project that survives it
    for p in TimeTrackingProject.objects(team=team, customer=source):
        twin = by_name.get((p.name or "").strip().lower()) if fold_projects else None
        if twin is not None:
            survivors[str(p.id)] = twin
            # A Cairn folder billing to the duplicate follows the survivor;
            # all_objects so a link on a trashed item is re-pointed too.
            out["relinked"] += Item.all_objects(time_project=p.id).update(
                set__time_project=twin.id)
            known = {k for k in [twin.external_id, *(twin.external_ids or [])] if k}
            more = [k for k in [p.external_id, *(p.external_ids or [])]
                    if k and k not in known]
            if more:
                twin.external_ids = list(twin.external_ids or []) + more
                twin.save()
            p.delete()
            out["projects_folded"] += 1
            continue
        p.customer = target
        if p.intake and has_intake:
            p.intake = False
        elif p.intake:
            has_intake = True
        p.save()
        if not p.archived:
            by_name.setdefault((p.name or "").strip().lower(), p)
        out["projects_moved"] += 1

    # --- entries -----------------------------------------------------------
    for bucket in TimeBucket.objects(team=team, customer=source).order_by(
            "+period", "+part"):
        period = bucket.period
        # What the target already holds for that day, read once: the entry ids
        # (so a re-run after a half-finished merge adds nothing) and every
        # external key (so a booking imported under both records lands once).
        seen_ids, seen_keys = set(), set()
        for b in TimeBucket.objects(team=team, customer=target, period=period):
            for e in b.entries:
                seen_ids.add(e.entry_id)
                seen_keys.update(k for k in [e.external_id, *(e.external_ids or [])] if k)
        moving = []
        for e in bucket.entries:
            if e.entry_id in seen_ids:
                continue
            keys = {k for k in [e.external_id, *(e.external_ids or [])] if k}
            twin = survivors.get(e.project_id)
            if twin is not None:
                e.project_id = str(twin.id)
                e.project_name = twin.name
            if keys & seen_keys:
                if e.locked:
                    out["duplicates_kept"] += 1
                elif e.status == "active":
                    e.status = "cancelled"
                    e.cancelled_at = _utcnow()
                    out["duplicates_cancelled"] += 1
            seen_ids.add(e.entry_id)
            seen_keys |= keys
            moving.append(e)
        if moving:
            _append_entries(team, target, period, moving)
            out["entries_moved"] += len(moving)
        bucket.delete()

    # --- identity ----------------------------------------------------------
    known = {k for k in [target.external_id, *(target.external_ids or [])] if k}
    more = [k for k in [source.external_id, *(source.external_ids or [])]
            if k and k not in known]
    if more:
        target.external_ids = list(target.external_ids or []) + more
    for field in ("legal_name", "address", "vat_id", "tax_number",
                  "invoice_email", "contact_name", "phone",
                  "payment_terms_days", "customer_number", "billing_note",
                  "note"):
        if not getattr(target, field, None) and getattr(source, field, None):
            setattr(target, field, getattr(source, field))
    target.save()
    # What points at the record about to disappear. A folder filed under the
    # duplicate, and a meeting booked by its people, mean the same client as
    # everything else that just moved — left behind they would dereference to
    # nothing, which reads as "no customer" on a folder that plainly has one.
    out["relinked"] += Item.all_objects(customer=source.id).update(
        set__customer=target.id)
    from application.models.booking import Booking
    out["relinked"] += Booking.objects(customer=source.id).update(
        set__customer=target.id)
    source.delete()
    return out


def described(note: str, source_label: str, show_source: bool) -> str:
    """The description as it should *read*: the task it was logged from, then
    the note. Composed here, never stored — see ``show_source``."""
    ref = (source_label or "").strip() if show_source else ""
    note = (note or "").strip()
    if ref and note:
        return f"{ref} — {note}"
    return ref or note


def fmt_duration(minutes) -> str:
    """Minutes → ``2h 30m``. The Jinja filter's twin, for strings built in
    Python (the customer's sheet renders its cells here, not in the template)."""
    h, m = divmod(int(minutes or 0), 60)
    return f"{h}h {m}m" if h and m else (f"{h}h" if h else f"{m}m")


# The customer sheet's fixed columns. ``label`` is what the page shows,
# ``csv`` what the download's header says — the file goes to the customer, who
# has been reading "Datum; Von; Bis" for as long as this link has existed.
# There is deliberately no money column here and no way to add one: the whole
# promise of this link is hours only, and a promise kept by having nothing to
# switch on is stronger than one kept by a switch.
SHEET_BASE_COLUMNS = [
    {"key": "date",    "label": "Date",        "csv": "Datum",        "num": True},
    {"key": "start",   "label": "From",        "csv": "Von",          "num": True},
    {"key": "end",     "label": "To",          "csv": "Bis",          "num": True},
    {"key": "hours",   "label": "Hours",       "csv": "Stunden",      "num": True},
    {"key": "user",    "label": "Who",         "csv": "Bearbeiter"},
    # Ticket, task and service normally ride *inside* the description, which is
    # how this sheet has always read. Give them a column of their own and the
    # description hands them over instead of repeating them (see below), so
    # either arrangement is a choice rather than a duplication.
    {"key": "ref",     "label": "Ticket",      "csv": "Ticket"},
    {"key": "source",  "label": "Task",        "csv": "Aufgabe"},
    {"key": "service", "label": "Service",     "csv": "Leistung"},
    {"key": "note",    "label": "Description", "csv": "Beschreibung"},
]

# What the description absorbs when nothing else shows it.
_NOTE_ABSORBS = ("ref", "source")


def available_sheet_columns(project) -> list[dict]:
    """Every column this project's sheet *could* show — the configurator's list.

    The fixed vocabulary plus one entry per custom field. Money is not in it and
    cannot be: see SHEET_BASE_COLUMNS."""
    out = list(SHEET_BASE_COLUMNS)
    for f in (project.custom_fields or []):
        out.append({"key": f"cf:{f.key}", "label": f.label, "csv": f.label})
    return out


def sheet_columns(project, *, has_times: bool = True) -> list[dict]:
    """The sheet's columns for ``project``, in the order it wants them.

    The project's own order via ``share_columns`` when it has one — followed
    exactly, including leaving columns out — otherwise the default set. A custom
    field removed from the project after being configured simply drops out."""
    available = available_sheet_columns(project)
    by_key = {c["key"]: c for c in available}
    wanted = [k for k in (project.share_columns or []) if k in by_key]
    if wanted:
        return [by_key[k] for k in wanted]
    # Default: the sheet as it has always read — dates, hours, who, the
    # description (with ticket and task folded into it) and the project's own
    # fields. Ticket / task / service only appear as columns when asked for.
    skip = {"ref", "source", "service"}
    if not has_times:
        skip |= {"start", "end"}
    return [c for c in available if c["key"] not in skip]


def sheet_cell(row: dict, key: str, *, for_csv: bool = False) -> str:
    """One cell of the customer's sheet, as text. Page and download read the
    same rows through the same function, so they can never disagree."""
    if key == "date":
        return row["date"].strftime("%d.%m.%Y")
    if key == "start":
        return row["start_time"]
    if key == "end":
        return row["end_time"]
    if key == "hours":
        # Excel reads "1,50" as a number in a German locale; "1.50" is text.
        return (f"{row['minutes'] / 60:.2f}".replace(".", ",") if for_csv
                else fmt_duration(row["minutes"]))
    if key == "user":
        return row["user_name"]
    if key == "ref":
        return row["ref"]
    if key == "source":
        return row["source_label"]
    if key == "service":
        return row["service"]
    if key == "note":
        return row["note"]
    if key.startswith("cf:"):
        return row["custom"].get(key[3:], "")
    return ""


def public_project_report(project, month: str | None = None,
                          group_by: str = "user", who: str = ""):
    """Read-only time sheet for the customer share link.

    Hours, clock times, notes and the project's custom fields — never rates or
    amounts, which is the whole point of the link. Returns one flat, sorted
    ``rows`` list for the table plus the month index that drives the filter, so
    the page and the export render from exactly the same data.

    ``month`` is ``"YYYY-MM"``; anything else (or ``None``) means every month."""
    buckets = TimeBucket.objects(team=project.team, customer=project.customer)
    pid = str(project.id)
    rows, months, total = [], {}, 0
    # Budget consumption is a property of the project, not of the month being
    # looked at, so it is summed over every entry — before the month filter
    # below — in the pass that is already walking the buckets.
    from application.services.tz import user_zone
    cur_period = datetime.now(user_zone(None)).strftime("%Y-%m")
    cons = {"minutes": 0, "amount_cents": 0,
            "month_minutes": 0, "month_amount_cents": 0}
    for b in buckets:
        for e in b.entries:
            if e.project_id != pid or e.status != "active":
                continue
            dd = local_date(e.spent_on)
            key = f"{dd.year:04d}-{dd.month:02d}"
            cons["minutes"] += e.minutes or 0
            cons["amount_cents"] += e.amount_cents or 0
            if key == cur_period:
                cons["month_minutes"] += e.minutes or 0
                cons["month_amount_cents"] += e.amount_cents or 0
            mo = months.setdefault(key, {"key": key, "label": dd.strftime("%B %Y"),
                                         "minutes": 0})
            mo["minutes"] += e.minutes or 0
            if month and key != month:
                continue
            if who and (e.user_name or "") != who:
                continue
            total += e.minutes or 0
            rows.append({
                "date": dd,
                "minutes": e.minutes or 0,
                "start_time": e.start_time or "",
                "end_time": e.end_time or "",
                # The ticket number is useful to the customer; the deep link
                # behind it points into an internal system, so it stays home.
                # The task's title joins it when the project asks for it. Kept
                # apart here and assembled below, once it is known whether they
                # have columns of their own.
                "note": (e.note or "").strip(),
                "ref": ref_label(e.external_ref) if e.external_ref else "",
                "source_label": e.source_label or "" if project.show_source else "",
                "service": e.service or "",
                "user_name": e.user_name or "",
                "custom": {c.key: c.value for c in (e.custom or []) if c.value},
            })
    rows.sort(key=lambda r: (r["date"], r["start_time"]), reverse=True)
    # Who worked in the shown month — offered as a filter, and counted from the
    # month rather than from the filtered result so the buttons don't vanish as
    # soon as one is pressed.
    people: dict = {}
    for b in buckets:
        for e in b.entries:
            if e.project_id != pid or e.status != "active":
                continue
            dd = local_date(e.spent_on)
            if month and f"{dd.year:04d}-{dd.month:02d}" != month:
                continue
            name = e.user_name or "—"
            people[name] = people.get(name, 0) + (e.minutes or 0)
    people_rows = [{"name": n, "minutes": m}
                   for n, m in sorted(people.items(), key=lambda kv: (-kv[1], kv[0].lower()))]
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
    # What the sheet can be broken down by: who did the work, and — because a
    # project that asks for "Abrechnungsart" on every booking is asking exactly
    # so it can be read back that way — each of its custom fields.
    options = [{"key": "user", "label": "Who"}] + [
        {"key": f"cf:{c['key']}", "label": c["label"]} for c in columns]
    if group_by not in {o["key"] for o in options}:
        group_by = "user"
    tally: dict = {}
    for r in rows:
        if group_by == "user":
            label = r["user_name"] or "—"
        else:
            label = r["custom"].get(group_by[3:]) or "—"
        tally[label] = tally.get(label, 0) + r["minutes"]
    top = max(tally.values(), default=0)
    groups = [{
        "label": label,
        "minutes": minutes,
        # Two different jobs: the bar's length compares the entries with each
        # other (longest fills the row), the percentage says what share of the
        # month this is.
        "width": round(100 * minutes / top) if top else 0,
        "share": round(100 * minutes / total) if total else 0,
    } for label, minutes in sorted(tally.items(),
                                   key=lambda kv: (-kv[1], kv[0].lower()))]

    cols = sheet_columns(project, has_times=(project.time_mode == "clock"
                                             or any(r["start_time"] for r in rows)))
    shown = {c["key"] for c in cols}
    for r in rows:
        # Whatever has no column of its own goes into the description, exactly
        # as this sheet has always read.
        lead = [r[k if k != "source" else "source_label"]
                for k in _NOTE_ABSORBS if k not in shown]
        r["note"] = " ".join([p for p in lead if p]
                             + ([r["note"]] if r["note"] else [])).strip() \
            if lead else r["note"]
    for r in rows:
        r["cells"] = [{"text": sheet_cell(r, c["key"]), "num": c.get("num", False)}
                      for c in cols]

    return {
        "rows": rows,
        "columns": columns,
        "sheet_columns": cols,
        "people": people_rows,
        "who": who,
        "group_by": group_by,
        "group_options": options,
        # Always returned, even when it is a single value: the page needs to be
        # able to say "all of it is X" instead of dropping the whole block —
        # which took the split switcher with it and left no way back.
        "groups": groups,
        "total_minutes": total,
        # The project's budget and what is left of it. Independent of the month
        # being viewed: a total budget is all-time, a retainer is the running
        # month. ``None`` when no budget is set. A *money* budget is rendered on
        # this page as a percentage only — the amount would give the hourly rate
        # away next to the hours (see the module note on the sheet's columns).
        "budget": budget_status(project, cons),
        "months": [months[k] for k in sorted(months, reverse=True)],
        "month": month or "",
        # A project booked by the clock shows the columns even on a month whose
        # entries have no times yet — the sheet's shape is a property of the
        # project, not of what happens to be in it. Otherwise a customer sees a
        # table that changes columns from month to month.
        "has_times": (project.time_mode == "clock"
                      or any(r["start_time"] for r in rows)),
    }


def can_bill(user, team) -> bool:
    """A team manager, or a member the manager has named a biller, may settle
    (lock) time. Both see the team's whole book (reachable_project_ids); only
    a manager may unlock what was billed."""
    actor = user._get_current_object() if hasattr(user, "_get_current_object") else user
    return team.is_managed_by(actor) or actor in (team.billers or [])


def set_locked(*, team, user, items, locked: bool) -> int:
    """Lock (bill) or unlock the given ``(bucket_id, entry_id)`` pairs.

    Locking marks entries billed + frozen (records who/when). Only a biller/
    manager may lock, and only entries whose project is in their reachable scope
    (the whole team, for both); only managers may unlock. Returns how many
    entries actually changed."""
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
        pk=bucket.pk,
        entries__match={"entry_id": entry_id, "status": "cancelled"},
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
        pk=bucket.pk,
                entries__match={"entry_id": entry_id, "locked": {"$ne": True}},
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
        pk=bucket.pk,
                entries__match={"entry_id": entry_id, "locked": {"$ne": True}},
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
    live_names = current_project_names(teams)
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
                        "project": live_names.get(e.project_id) or e.project_name,
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
                "project": live_names.get(e.project_id) or e.project_name,
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
    def _in_the_day(r):
        """Where an entry sits in the day's list.

        A day booked by the clock is a timeline, so the clock decides: 09:00
        before 13:00, whatever order they were typed in. An entry booked as a
        plain duration has no place on that timeline — it follows the timed
        ones, in the order it was logged. Both branches build the same tuple
        shape, or the keys wouldn't compare."""
        stamp = r["created_at"] or _utcnow()
        if r["start_time"]:
            return (0, r["start_time"], r["end_time"] or "", stamp)
        return (1, "", "", stamp)

    entries.sort(key=_in_the_day)
    removed.sort(key=lambda r: r["cancelled_at"] or _utcnow(), reverse=True)
    # The shortlist for the capture bar, with the preselected project pinned
    # into it; anything else is found by typing (project_search).
    preselect = default_project_id(actor, None)
    projects = pickable_projects(actor, limit=PICKER_SHORTLIST, keep=preselect)
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
        # for exactly what that project wants without a lookup per row. Taken
        # from the day's own entries as well as the shortlist: an entry booked
        # on a project that isn't offered here still has to be editable.
        "fields_by_project": {
            **{p["id"]: p["fields"] for p in projects},
            **project_fields([r["project_id"] for r in entries]),
        },
        "default_project_id": preselect,
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


def sync_external_entries(*, user, item, rows, project=None,
                          team=None) -> tuple[int, int]:
    """Mirror an external system's time for ``item``. Returns ``(added, fixed)``.

    Each row is ``{external_id, minutes, spent_on, note, external_ref,
    external_url, user_id, user_name, external_user}`` plus an optional
    ``covered``: the source records this one row stands for. The Zammad sync
    sends a day's bookings as a single row that way — one entry per day and
    person rather than one per booking — and the ids it covers are stored on
    the entry, so the dedup below recognises the total *and* each part of it.

    Rows the team already has are not appended again — dedup is by
    ``external_id`` across the **whole team**, not just this item, because two
    people syncing the same ticket keep their own Cairn task for it and would
    otherwise book the same hour twice. A booking the team *deleted* here
    counts as known too: cancelling an imported entry is a decision, and a sync
    that quietly re-added it every few minutes would be impossible to argue
    with.

    A row that grew since last time (more bookings that day) updates the entry's
    minutes. Once the entry is **billed** it is frozen instead, and the bookings
    it doesn't already cover are added as their own entries — the invoiced lump
    stays exactly as invoiced, and the new hours still show up.

    A row the team already has is instead *corrected*: description, ticket
    number, deep link and — the point of the exercise — who the time belongs
    to. That is what repairs entries imported before any of this existed, and
    keeps attribution right if someone's account is matched up later. Locked
    (billed) entries are never touched.

    ``project`` overrides where the time books; without it the item's subtree
    link is resolved, and no link means nothing happens — that link *is* the
    opt-in."""
    if not rows or not (team or has_team(user)):
        return 0, 0
    project = project or resolve_time_project(item)
    if project is None:
        return 0, 0
    # A team run books on the team's own authority (one connection, one team);
    # a user run may only book into a team they belong to.
    if team is None and str(project.team.id) not in user_team_ids(user):
        return 0, 0
    team = project.team

    wanted = {r["external_id"]: r for r in rows if r.get("external_id")}
    if not wanted:
        return 0, 0

    # Every id that could already be here: the day's own key, and each booking
    # it sums up — an entry written before the day was summed carries the
    # booking's id, and must be recognised as part of the total rather than
    # left beside it.
    keys = {k: {k} | set(r.get("covered") or []) for k, r in wanted.items()}
    lookup = {i: k for k, ids in keys.items() for i in ids}

    # One indexed lookup for everything this team already knows of these ids.
    seen: dict = {}
    for b in TimeBucket.objects(
            Q(entries__external_id__in=list(lookup))
            | Q(entries__external_ids__in=list(lookup)), team=team):
        for e in b.entries:
            hit = lookup.get(e.external_id or "") or next(
                (lookup[i] for i in (e.external_ids or []) if i in lookup), None)
            if hit:
                seen.setdefault(hit, []).append((b, e))

    added = fixed = 0
    src = source_snapshot(item)
    for ext, r in wanted.items():
        minutes = int(r.get("minutes") or 0)
        if minutes < 1:
            continue
        covered = list(r.get("covered") or [])
        hits = seen.get(ext) or []
        if not hits:
            add_entry(project=project, user=user, minutes=minutes,
                      spent_on=r["spent_on"], note=r.get("note", ""),
                      external_ref=r.get("external_ref", ""),
                      external_url=r.get("external_url", ""),
                      external_user=r.get("external_user", ""),
                      actor={"user_id": r.get("user_id") or None,
                             "user_name": r.get("user_name") or ""},
                      source=src, external_id=ext, external_ids=covered,
                      billable=True)
            added += 1
            continue
        # Whichever of them is the day's entry — prefer one that already is,
        # otherwise the oldest, which is the one the sheet has been showing.
        hits.sort(key=lambda be: (be[1].external_id != ext, be[1].entry_id))
        bucket, entry = hits[0]
        rest = hits[1:]
        if entry.locked or entry.status != "active":
            # Billed, or cancelled on purpose: leave it exactly as it is and
            # book only what it doesn't already stand for.
            known = {entry.external_id or ""} | set(entry.external_ids or [])
            for part in [c for c in covered if c not in known]:
                add_entry(project=project, user=user,
                          minutes=int(r.get("part_minutes", {}).get(part, 0)) or 0,
                          spent_on=r["spent_on"], note=r.get("note", ""),
                          external_ref=r.get("external_ref", ""),
                          external_url=r.get("external_url", ""),
                          external_user=r.get("external_user", ""),
                          actor={"user_id": r.get("user_id") or None,
                                 "user_name": r.get("user_name") or ""},
                          source=src, external_id=part, billable=True)
                added += 1
            continue
        changes = _external_corrections(entry, r)
        # The day's total: what it is now, what it stands for, and — once the
        # parts are folded in — the one line that replaces them.
        if entry.minutes != minutes:
            changes["set__entries__S__minutes"] = minutes
        if entry.external_id != ext:
            changes["set__entries__S__external_id"] = ext
        if set(entry.external_ids or []) != set(covered):
            changes["set__entries__S__external_ids"] = covered
        if changes and TimeBucket.objects(
            pk=bucket.pk,
            entries__match={"entry_id": entry.entry_id, "locked": {"$ne": True}},
        ).update_one(set__updated_at=_utcnow(), **changes):
            fixed += 1
        for other_bucket, other in rest:
            # Written per booking before the day was summed: its minutes are in
            # the total now, so the line goes rather than being counted twice.
            if other.locked or other.status != "active":
                continue
            TimeBucket.objects(pk=other_bucket.pk).update_one(
                pull__entries__entry_id=other.entry_id,
                inc__entry_count=-1, set__updated_at=_utcnow())
        _recount(bucket.pk)
        for other_bucket, _ in rest:
            _recount(other_bucket.pk)
    return added, fixed


def _recount(bucket_pk) -> None:
    """Re-derive a bucket's cached totals from the entries it now holds.

    The counters are maintained incrementally everywhere else; after a merge
    (entries removed, another's minutes rewritten) deriving them is the only
    way to be sure they still describe the document."""
    bucket = TimeBucket.objects(pk=bucket_pk).first()
    if bucket is None:
        return
    minutes = sum(e.minutes or 0 for e in bucket.entries if e.status == "active")
    count = len(bucket.entries)
    if bucket.total_minutes == minutes and bucket.entry_count == count:
        return
    if not bucket.entries:
        bucket.delete()
        return
    TimeBucket.objects(pk=bucket_pk).update_one(
        set__total_minutes=minutes, set__entry_count=count,
        set__updated_at=_utcnow())


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
                pk=b.pk,
                entries__match={"entry_id": e.entry_id, "locked": {"$ne": True}},
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


def split_note_times(*, user=None, dry_run: bool = False) -> dict:
    """Lift "(10:00 bis 12:00)" out of old notes into the entry's own fields.

    mite has no start/end fields, so the times of day were written into the
    note. The importer decodes that now, but everything imported before it did
    still carries the times as text — invisible to the report's time columns and
    to the customer's sheet, which is where they were wanted in the first place.

    Only touches entries that have no times yet and whose note actually opens
    with a time range; billed entries are left frozen. Idempotent: once the
    times are fields, the note no longer matches."""
    from application.services.mite_import import split_clock

    q = TimeBucket.objects
    if user is not None:
        teams = user_teams(user)
        if not teams:
            return {"entries": 0, "buckets": 0}
        q = q.filter(team__in=teams)

    out = {"entries": 0, "buckets": 0}
    for b in q:
        touched = False
        for e in b.entries:
            if e.status != "active" or e.locked or e.start_time or e.end_time:
                continue
            start, end, note = split_clock(e.note or "")
            if not start:
                continue
            out["entries"] += 1
            touched = True
            if dry_run:
                continue
            TimeBucket.objects(
                pk=b.pk,
                entries__match={"entry_id": e.entry_id, "locked": {"$ne": True}},
            ).update_one(
                set__entries__S__start_time=start,
                set__entries__S__end_time=end,
                set__entries__S__note=note,
                set__updated_at=_utcnow(),
            )
        if touched:
            out["buckets"] += 1
    return out


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
