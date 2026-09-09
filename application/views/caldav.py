"""A minimal CalDAV server exposing each user's tasks as VTODO items.

Lets a standard CalDAV client — Apple Reminders, Things, Tasks.org … — use
Cairn as a read/write task backend.

Structure: **one calendar collection per Cairn project** (= one list in
Reminders), plus an **Inbox** collection for tasks with no project. A task
belongs to the collection of its nearest ancestor project; tasks under a
section fold into that section's project (Reminders can't show nested lists
via CalDAV, so sections/folders flatten into the project list). Moving a
reminder between lists re-parents the task (handled via MOVE and via the
client's PUT-new / DELETE-old dance).

* HTTP Basic auth (username **or** email + password) over HTTPS.
* CSRF-exempt (no browser form posts).
* Apple-style change detection via per-collection ``CS:getctag`` +
  ``calendar-query`` / ``calendar-multiget`` REPORTs (no ``sync-token`` yet).

Mapping (``Item`` ⇄ ``VTODO``):
  title↔SUMMARY, note↔DESCRIPTION (with a link back to the task on its first
  line, stripped again on the way in), due_date↔DUE, defer_date↔DTSTART,
  priority↔PRIORITY (Apple buckets: P1=High/1, P2=Medium/5, P3+P4=Low/9),
  tags↔CATEGORIES,
  status: done→COMPLETED, dropped→CANCELLED, else NEEDS-ACTION.

A task that is **parked is not delivered at all**: deferred into the future, or
sitting in one of the model's ``BLOCKED_STATUS`` states (waiting, waiting for a
reply, someday). Its own value counts and so does an ancestor's — deferring or
parking a project holds its whole subtree back, the same rule as
``Item.is_blocked``. Such a task isn't visible in Cairn yet, so it has no
business sitting in a Reminders list; a deferred one comes back by itself once
the date passes, because the collection's ctag counts the tasks it serves.

Standards-pragmatic and small; expect to iterate against a real device.
"""
import base64
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

from flask import (
    Blueprint, Response, abort, current_app, redirect, request, url_for,
)
from mongoengine import Q
from mongoengine.errors import ValidationError

from icalendar import Calendar, Event, Todo

from application import limiter
from application.models.item import BLOCKED_STATUS, Item
from application.models.user import User

caldav_bp = Blueprint("caldav", __name__)

# Brute-force limit across every CalDAV route. This surface authenticates with
# HTTP Basic against the user's real account password, is CSRF-exempt by design
# and needs no session round-trip, which makes it the softest credential-
# stuffing target in the app.
#
# Only 401s are counted: clients poll constantly and a per-request limit would
# throttle normal syncing. A client sending good credentials is never affected,
# however chatty it is; one hammering bad ones runs out.
limiter.limit(
    lambda: current_app.config["RATELIMIT_CALDAV"],
    deduct_when=lambda response: response.status_code == 401,
)(caldav_bp)

_NS = ('xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" '
       'xmlns:CS="http://calendarserver.org/ns/" '
       'xmlns:ICAL="http://apple.com/ns/ical/"')

_INBOX = "inbox"
# Reserved collection id for the read-only VEVENT feed (tasks-as-events).
_CALENDAR = "calendar"
# Cairn priority (1 highest … 4 lowest) ⇄ iCalendar PRIORITY using Apple's
# buckets: 1 = High, 5 = Medium, 9 = Low (read side: 1–4 High, 5 Medium,
# 6–9 Low, 0/none = none). Cairn has 4 levels vs Apple's 3, so P4 shares the
# "Low" bucket with P3 — see _ical_priority_bucket for the round-trip guard.
_CAIRN_TO_ICAL_PRIO = {1: 1, 2: 5, 3: 9, 4: 9}


def _ical_priority_bucket(p):
    """iCal PRIORITY int → 'high' | 'medium' | 'low' | None (Apple buckets)."""
    if not p:
        return None
    return "high" if p <= 4 else "medium" if p == 5 else "low"


def _cairn_priority_bucket(c):
    """Cairn priority (1–4) → the same bucket vocabulary."""
    return {1: "high", 2: "medium", 3: "low", 4: "low"}.get(c)


def _utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- auth ----

def _authenticate():
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Basic "):
        return None
    try:
        login, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
    except Exception:
        return None
    login = login.strip().lower()
    if not login or not pw:
        return None
    user = User.objects(Q(email=login) | Q(username=login)).first()
    if user and user.is_active_user and user.check_password(pw):
        return user
    return None


def _require(username=None):
    user = _authenticate()
    if not user:
        resp = Response("Authentication required", 401)
        resp.headers["WWW-Authenticate"] = 'Basic realm="Cairn CalDAV"'
        abort(resp)
    if username is not None and username != user.username:
        abort(403)
    return user


# --------------------------------------------------- collections / map ----

def _principal_href(u):
    return f"/caldav/principals/{u.username}/"


def _home_href(u):
    return f"/caldav/calendars/{u.username}/"


def _collection_href(u, colid):
    return f"/caldav/calendars/{u.username}/{colid}/"


def _resource_href(u, colid, uid):
    return f"/caldav/calendars/{u.username}/{colid}/{uid}.ics"


def _uid(item):
    return item.caldav_uid or f"cairn-{item.pk}"


def _etag(item):
    ts = int((item.updated_at or item.created_at or _utcnow()).timestamp())
    return f'"{item.pk}-{ts}"'


def _user_tasks(user):
    return list(Item.objects(owner=user, kind="task").order_by("-updated_at"))


def _parent_id(item):
    """The parent's id WITHOUT dereferencing it. ``item.parent`` fetches the
    document — once per task, on every poll — and the walks below only ever
    need the id, because the containers are already loaded."""
    raw = item._data.get("parent")
    if raw is None:
        return None
    return str(getattr(raw, "id", None) or getattr(raw, "pk", None) or raw)


def _resolve_ancestors(nodes):
    """Pull in ancestors that aren't loaded yet, one ``$in`` query per level.

    ``nodes`` holds the user's own containers and tasks, which covers the whole
    chain in the normal case; a task filed under someone else's shared project
    is the exception, and it is fetched in bulk rather than one dereference at
    a time. ``all_objects`` so a trashed parent still resolves — reference
    dereferencing bypasses the queryset manager, and this replaces it."""
    absent = set()
    for _ in range(10):
        missing = {pid for it in nodes.values()
                   if (pid := _parent_id(it)) and pid not in nodes
                   and pid not in absent}
        if not missing:
            return
        found = set()
        for c in (Item.all_objects(pk__in=list(missing))
                  .only("pk", "kind", "parent", "defer_date", "status", "trashed_at")):
            nodes[str(c.pk)] = c
            found.add(str(c.pk))
        # Ids that resolve to nothing (a deleted parent) must not be asked for
        # again on the next round.
        absent |= missing - found


def _container_paths(user):
    """``({container_pk: "Folder / Sub / Project"}, {pk: Item})`` for every
    ACTIVE project/folder/section the user owns. Ancestor paths resolve
    in-memory from one query so list names carry the full breadcrumb
    (gleichnamige Projekte in verschiedenen Ordnern bleiben unterscheidbar).

    ``status="active"`` is the same filter the project navigator uses. Without
    it a completed or dropped project stayed a list in Reminders forever — the
    user had closed it years ago and still saw it on the phone. (Trashed items
    never got here: ``Item.objects`` hides them.)"""
    containers = list(Item.objects(owner=user, status="active",
                                   kind__in=["project", "folder", "section"])
                      .only("pk", "title", "kind", "parent", "color"))
    by_id = {str(c.pk): c for c in containers}

    def label(c):
        parts, cur, seen = [], c, set()
        while cur is not None and str(cur.pk) not in seen:
            seen.add(str(cur.pk))
            parts.append(cur.name)
            cur = by_id.get(_parent_id(cur))
        return " / ".join(reversed(parts))

    return {pk: label(c) for pk, c in by_id.items()}, by_id


def _collections(user, with_tasks=None):
    """[(colid, label, color)] — Inbox first, then every project plus any
    folder that directly holds tasks, labelled with its full path. Folders
    with no direct tasks (pure organisers of sub-projects) are omitted so
    Reminders isn't cluttered with empty lists. ``with_tasks`` = the set of
    colids that currently have tasks."""
    paths, by_id = _container_paths(user)
    with_tasks = with_tasks or set()
    cols = [(_INBOX, "Inbox", "#8E8E93")]
    items = [c for c in by_id.values()
             if c.kind == "project"
             or (c.kind == "folder" and str(c.pk) in with_tasks)]
    items.sort(key=lambda c: paths[str(c.pk)].lower())
    for c in items:
        cols.append((str(c.pk), paths[str(c.pk)], (c.color or "#1F6FEB")))
    return cols


def _held_back(item, nodes=None, now=None):
    """True if ``item`` is parked — a defer date still in the future, or a
    ``BLOCKED_STATUS`` (waiting / waiting_reply / someday) — on itself or on any
    ancestor. Same rule as ``Item.is_blocked``, minus the parts that cost a
    query per task (dependencies, sequential order); ``BLOCKED_STATUS`` is
    imported rather than restated so the two can't drift apart.

    ``nodes`` is the pre-loaded ``{pk: Item}`` map of the walk in
    ``_colid_map``, so a whole list costs no extra query; without it (single
    item, e.g. DELETE) the parent chain is dereferenced, which is one query
    per level and fine for one task.
    """
    now = now or _utcnow()
    node, seen, depth = item, set(), 0
    while node is not None and depth < 50:
        depth += 1
        pk = str(node.pk)
        if pk in seen:
            break
        seen.add(pk)
        # A folder holds nothing back — same rule as ``Item._own_block``.
        if node.kind != "folder":
            if getattr(node, "status", None) in BLOCKED_STATUS:
                return True
            deferred = _aware(getattr(node, "defer_date", None))
            if deferred and deferred > now:
                return True
        pid = _parent_id(node)
        if pid is None:
            break
        # With a node map the whole chain is already in memory (see
        # _resolve_ancestors); without one — a single item, e.g. on DELETE —
        # dereference, which costs one query per level and is fine for one task.
        node = nodes.get(pid) if nodes is not None else node.parent
    return False


def _colid_map(user):
    """Return ``({task_pk: colid}, [tasks])`` — colid is the nearest ancestor
    project's pk, or "inbox". Parked tasks are dropped here, which is the one
    place every listing goes through.

    Every parent chain is walked in memory over ids: a client polls constantly,
    and dereferencing ``task.parent`` cost one query per task on every single
    poll."""
    nodes = {}
    for c in (Item.objects(owner=user, kind__in=["project", "section", "folder"])
              .only("pk", "kind", "parent", "defer_date", "status", "trashed_at")):
        nodes[str(c.pk)] = c
    tasks = _user_tasks(user)
    for t in tasks:
        nodes.setdefault(str(t.pk), t)
    _resolve_ancestors(nodes)
    now = _utcnow()
    tasks = [t for t in tasks if not _held_back(t, nodes, now)]
    out, exported = {}, []
    for t in tasks:
        colid = _colid_of(t, nodes)
        if colid is None:
            # Its project is closed or gone, so there is no list to put it in.
            # Dropping it is the honest answer: inventing an Inbox entry would
            # tip every task of every finished project into Reminders.
            continue
        out[str(t.pk)] = colid
        exported.append(t)
    return out, exported


def _is_list(node):
    """Is this container a Reminders list? Active and not in the trash. The
    node map is closed with ``all_objects`` so a defer date on a trashed parent
    still counts — which means the trashed parent is in there, and the
    collection decision has to say no to it explicitly."""
    return (node.kind in ("project", "folder")
            and node.status == "active"
            and getattr(node, "trashed_at", None) is None)


def _colid_of(item, nodes):
    """The collection ``item`` belongs to, walked over the pre-loaded ``nodes``:
    the nearest ancestor project/folder, ``_INBOX`` when there is no container
    above it at all, or ``None`` when the container it does have is not
    exported (closed project, or one this map can't see)."""
    pid, seen, guard = _parent_id(item), set(), 0
    while pid is not None and guard < 50:
        guard += 1
        if pid in seen:
            return None
        seen.add(pid)
        node = nodes.get(pid)
        if node is None:
            return None
        if node.kind in ("project", "folder"):
            return pid if _is_list(node) else None
        pid = _parent_id(node)
    return _INBOX if _parent_id(item) is None else None


def _nearest_colid(item):
    """The collection a single Item currently lives in — same answer as
    ``_colid_of``, for the one-item paths (PUT / DELETE) that have no map."""
    cur, seen, guard = item.parent, set(), 0
    while cur is not None and guard < 50:
        guard += 1
        if str(cur.pk) in seen:
            return None
        seen.add(str(cur.pk))
        if getattr(cur, "kind", None) in ("project", "folder"):
            return str(cur.pk) if _is_list(cur) else None
        cur = cur.parent
    return _INBOX if item.parent is None else None


def _resolve_collection(user, colid):
    """The project Item for ``colid``; None for inbox; 404 if unknown."""
    if colid == _INBOX:
        return None
    try:
        proj = Item.objects(pk=colid, owner=user, status="active",
                            kind__in=["project", "folder"]).first()
    except (ValidationError, Exception):
        proj = None
    if proj is None:
        abort(404)
    return proj


def _find(user, uid):
    if uid.endswith(".ics"):
        uid = uid[:-4]
    if uid.startswith("cairn-"):
        try:
            return Item.objects(pk=uid[len("cairn-"):], owner=user, kind="task").first()
        except (ValidationError, Exception):
            return None
    return Item.objects(caldav_uid=uid, owner=user, kind="task").first()


# ------------------------------------------------------- iCalendar I/O ----

def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _same_user_day(a, b, user):
    """True if aware datetimes ``a`` and ``b`` fall on the same calendar day
    in the user's timezone."""
    from application.services.tz import user_zone
    z = user_zone(user)
    return a.astimezone(z).date() == b.astimezone(z).date()


def _to_dt(prop, user=None):
    from application.services.tz import attach_user_tz
    val = getattr(prop, "dt", None)
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo:
            return val
        # Floating time (no TZID) → interpret as the user's local wall-clock,
        # exactly like Cairn's datetime-local form inputs. Treating it as UTC
        # shifted timed reminders by the user's offset (e.g. +2h Berlin summer).
        return attach_user_tz(val, user)
    # all-day DATE → midnight in the user's tz
    return attach_user_tz(datetime(val.year, val.month, val.day), user)


def _task_url(item):
    """Absolute link back to the task in Cairn. CalDAV always runs inside a
    request and ProxyFix is on, so ``_external`` builds the public https URL;
    the fallback keeps the export working if url_for ever can't."""
    try:
        return url_for("tasks.detail", task_id=item.pk, _external=True)
    except Exception:
        return f"{request.host_url.rstrip('/')}/tasks/{item.pk}"


def _description(item):
    """DESCRIPTION for the export: the link back to Cairn on the first line,
    then the note. A reminder carries no other handle on the task — without the
    link the way back is searching Cairn for the title, which is what a link is
    for. Stripped again on the way in (see ``_strip_backlink``)."""
    url = _task_url(item)
    return f"{url}\n\n{item.note}" if item.note else url


def _strip_backlink(text, item):
    """Drop the backlink from an inbound DESCRIPTION. A client sends the whole
    description back on every edit, so without this the link would be saved
    into the note and a second one prepended on the next export, and so on. Only
    a link to THIS task counts, so a Cairn link the user typed themselves
    survives."""
    if not text or not item.pk:
        return text
    m = re.match(r"\s*https?://\S*/tasks/" + re.escape(str(item.pk))
                 + r"/?[ \t]*(?:\r?\n)*", text, re.IGNORECASE)
    return text[m.end():] if m else text


def _todo_ics(item):
    cal = Calendar()
    cal.add("prodid", "-//Cairn//CalDAV//EN")
    cal.add("version", "2.0")
    todo = Todo()
    todo.add("uid", _uid(item))
    todo.add("summary", item.title or "")
    todo.add("description", _description(item))
    # Clients that show a URL field (Reminders on recent OS versions) get the
    # link as a tappable one rather than as text in the notes.
    todo.add("url", _task_url(item))
    # CalDAV/Reminders has a single date (DUE). Per the user's rule: the PLAN
    # date is the Reminders date whenever it's set (that's their "do it" date,
    # so planned tasks always land in Reminders' Today); the due date is only
    # the fallback when there's no plan date. The real due is preserved across
    # round-trips by the _apply_ics guard below.
    due_for_ics = item.plan_date or item.due_date
    if due_for_ics:
        todo.add("due", _aware(due_for_ics))
    if item.defer_date:
        todo.add("dtstart", _aware(item.defer_date))
    if item.location:
        todo.add("location", item.location)
    todo.add("dtstamp", _aware(item.updated_at or _utcnow()))
    todo.add("created", _aware(item.created_at or _utcnow()))
    todo.add("last-modified", _aware(item.updated_at or _utcnow()))
    prio = _CAIRN_TO_ICAL_PRIO.get(item.priority)
    if prio:
        todo.add("priority", prio)
    if item.status == "done":
        todo.add("status", "COMPLETED")
        todo.add("percent-complete", 100)
        todo.add("completed", _aware(item.completed_at or item.updated_at or _utcnow()))
    elif item.status == "dropped":
        todo.add("status", "CANCELLED")
    else:
        todo.add("status", "NEEDS-ACTION")
    if item.tags:
        todo.add("categories", list(item.tags))
    cal.add_component(todo)
    return cal.to_ical().decode("utf-8")


def _apply_ics(item, raw, user=None):
    """Apply an inbound VTODO to ``item`` (unsaved). Returns False if the body
    can't be parsed at all. Every individual field is read defensively so an
    exotic client payload (Apple location reminders, alarms, X- props, odd
    dates) updates what it can instead of 500-ing the whole PUT. ``user``
    supplies the timezone for floating (naive) dates."""
    try:
        cal = Calendar.from_ical(raw)
        todo = next((c for c in cal.walk("VTODO")), None)
    except Exception:
        return False
    if todo is None:
        return False

    def g(name):
        try:
            return todo.get(name)
        except Exception:
            return None

    item.title = (str(g("summary") or "") or item.title or "Untitled")[:500]
    desc = g("description")
    item.note = _strip_backlink(str(desc), item) if desc is not None else ""
    loc = g("location")
    location = str(loc) if loc is not None else ""
    if not location:
        # Apple geofenced reminders ("hier erinnern") don't set a top-level
        # LOCATION — the place lives in a VALARM's X-APPLE-STRUCTURED-LOCATION
        # (readable name in the X-TITLE param, coords in the geo: URI value).
        try:
            for sub in todo.subcomponents:
                sl = sub.get("X-APPLE-STRUCTURED-LOCATION")
                if sl is None:
                    continue
                params = getattr(sl, "params", {}) or {}
                title = params.get("X-TITLE") or params.get("X-APPLE-TITLE")
                if title:
                    location = str(title)
                    break
        except Exception:
            pass
    item.location = location
    try:
        due = _to_dt(g("due"), user)
    except Exception:
        due = None
    try:
        start = _to_dt(g("dtstart"), user)
    except Exception:
        start = None
    # The DUE we emit mirrors plan_date (if set) else due_date. If the client
    # echoes it unchanged, keep both fields intact. A genuinely changed date
    # updates whichever the DUE represents — the plan when set, otherwise the
    # due — so the other date is never clobbered. Second precision (iCalendar
    # drops sub-second).
    _mirrored = item.plan_date or item.due_date
    echoed = (due is not None and _mirrored is not None
              and abs((due - _mirrored).total_seconds()) < 1)
    if not echoed:
        if item.plan_date is not None:
            item.plan_date = due
        else:
            item.due_date = due
    # DTSTART counts as a defer only when it's a genuinely earlier *day* than
    # the due (in the user's tz). Apple Reminders mirrors DTSTART onto the due
    # day — sometimes as a date-only value against a date-time DUE, so exact
    # equality isn't enough — and that mirror is NOT a user-chosen defer.
    if start is None:
        item.defer_date = None
    elif due is not None and _same_user_day(start, due, user):
        item.defer_date = None
    else:
        item.defer_date = start

    try:
        pr = g("priority")
        pr = None if pr is None else int(pr)
    except Exception:
        pr = None
    incoming = _ical_priority_bucket(pr)
    if incoming is None:
        item.priority = None
    elif _cairn_priority_bucket(item.priority) == incoming:
        # Same bucket as now — keep the exact Cairn level so a P4 task isn't
        # silently downgraded to P3 just because Apple only has one "Low".
        pass
    else:
        item.priority = {"high": 1, "medium": 2, "low": 3}[incoming]

    st = str(g("status") or "").upper()
    try:
        pc = g("percent-complete")
        pc = None if pc is None else int(pc)
    except Exception:
        pc = None
    if st == "COMPLETED" or (pc is not None and pc >= 100):
        item.status = "done"
        item.completed_via = "caldav"
    elif st == "CANCELLED":
        item.status = "dropped"
        item.completed_via = "caldav"
    else:
        item.status = "open"

    cats = g("categories")
    if cats is None:
        item.tags = []
    else:
        try:
            item.tags = [str(c) for c in cats.cats]
        except AttributeError:
            item.tags = [str(cats)]
    return True


# ------------------------------------------- calendar feed (VEVENT) ----

def _calendar_enabled(user):
    return bool(getattr(user, "caldav_calendar_enabled", False))


def _calendar_source(user):
    src = getattr(user, "caldav_calendar_source", None)
    return src if src in ("due", "plan") else "due"


def _event_date(item, source):
    return item.plan_date if source == "plan" else item.due_date


def _calendar_tasks(user, source):
    """Open tasks that carry the chosen date — they become calendar events."""
    field = "plan_date" if source == "plan" else "due_date"
    return list(Item.objects(owner=user, kind="task",
                             status__nin=["done", "dropped"],
                             **{f"{field}__ne": None}).order_by(f"+{field}"))


def _event_uid(item):
    # Distinct from the VTODO uid so todo + event can coexist for one task.
    return f"cairn-cal-{item.pk}"


def _find_event(user, name):
    uid = name[:-4] if name.endswith(".ics") else name
    if not uid.startswith("cairn-cal-"):
        return None
    try:
        return Item.objects(pk=uid[len("cairn-cal-"):], owner=user, kind="task").first()
    except (ValidationError, Exception):
        return None


def _event_ics(item, source):
    cal = Calendar()
    cal.add("prodid", "-//Cairn//CalDAV//EN")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", _event_uid(item))
    ev.add("summary", item.title or "")
    ev.add("description", _description(item))
    ev.add("url", _task_url(item))
    start = _aware(_event_date(item, source))
    ev.add("dtstart", start)
    ev.add("dtend", start + timedelta(minutes=30))
    ev.add("dtstamp", _aware(item.updated_at or _utcnow()))
    ev.add("last-modified", _aware(item.updated_at or _utcnow()))
    cal.add_component(ev)
    return cal.to_ical().decode("utf-8")


# ----------------------------------------------------------- XML build ----

def _multistatus(responses):
    body = ('<?xml version="1.0" encoding="utf-8"?>\n'
            f'<D:multistatus {_NS}>' + "".join(responses) + '</D:multistatus>')
    return Response(body, status=207, mimetype='application/xml; charset="utf-8"')


def _ok(href, props):
    return (f'<D:response><D:href>{escape(href)}</D:href>'
            f'<D:propstat><D:prop>{props}</D:prop>'
            f'<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>')


def _cup(user):
    return (f'<D:current-user-principal><D:href>{escape(_principal_href(user))}'
            f'</D:href></D:current-user-principal>')


def _ctag(tasks):
    latest, n = 0, 0
    for t in tasks:
        n += 1
        ts = int((t.updated_at or t.created_at or _utcnow()).timestamp())
        latest = max(latest, ts)
    return f'"{n}-{latest}"'


def _privset(read_only=False):
    """``current-user-privilege-set`` so Apple knows whether it may write."""
    privs = "<D:privilege><D:read/></D:privilege>"
    if not read_only:
        privs += ("<D:privilege><D:write/></D:privilege>"
                  "<D:privilege><D:write-content/></D:privilege>"
                  "<D:privilege><D:bind/></D:privilege>"
                  "<D:privilege><D:unbind/></D:privilege>")
    return f"<D:current-user-privilege-set>{privs}</D:current-user-privilege-set>"


def _collection_props(user, label, color, tasks):
    return ('<D:resourcetype><D:collection/><C:calendar/></D:resourcetype>'
            f'<D:displayname>{escape(label)}</D:displayname>'
            '<C:supported-calendar-component-set><C:comp name="VTODO"/>'
            '</C:supported-calendar-component-set>'
            f'<CS:getctag>{escape(_ctag(tasks))}</CS:getctag>'
            f'<ICAL:calendar-color>{escape(color)}</ICAL:calendar-color>'
            + _privset(read_only=False) + _cup(user))


def _calendar_collection_props(user, tasks):
    return ('<D:resourcetype><D:collection/><C:calendar/></D:resourcetype>'
            '<D:displayname>Cairn (Kalender)</D:displayname>'
            '<C:supported-calendar-component-set><C:comp name="VEVENT"/>'
            '</C:supported-calendar-component-set>'
            f'<CS:getctag>{escape(_ctag(tasks))}</CS:getctag>'
            '<ICAL:calendar-color>#34C759</ICAL:calendar-color>'
            + _privset(read_only=True) + _cup(user))


def _options():
    resp = Response(status=200)
    resp.headers["DAV"] = "1, 2, 3, calendar-access"
    resp.headers["Allow"] = ("OPTIONS, GET, HEAD, PUT, DELETE, MOVE, PROPFIND, "
                             "PROPPATCH, REPORT, MKCALENDAR")
    return resp


# -------------------------------------------------------------- routes ----

@caldav_bp.route("/.well-known/caldav",
                 methods=["GET", "PROPFIND", "OPTIONS", "REPORT"])
def wellknown():
    # Clients (Apple especially) probe well-known with OPTIONS/PROPFIND, not
    # GET. Answer OPTIONS directly; redirect everything else to the real
    # context path with 308 so the original method (PROPFIND) is preserved.
    if request.method == "OPTIONS":
        return _options()
    return redirect("/caldav/", code=308)


@caldav_bp.route("/", methods=["PROPFIND"])
def site_root_discovery():
    """RFC 6764 fallback: clients given just the hostname PROPFIND ``/`` for
    ``current-user-principal``. Point them at our principal so discovery works
    without entering the full ``/caldav/`` path (and without relying on the
    nginx-served ``/.well-known/caldav``). GET ``/`` stays the dashboard."""
    user = _require()
    props = ('<D:resourcetype><D:collection/></D:resourcetype>' + _cup(user)
             + f'<D:principal-URL><D:href>{escape(_principal_href(user))}</D:href>'
             '</D:principal-URL>')
    return _multistatus([_ok("/", props)])


@caldav_bp.route("/caldav/", methods=["PROPFIND", "OPTIONS"])
def root():
    if request.method == "OPTIONS":
        return _options()
    user = _require()
    props = ('<D:resourcetype><D:collection/></D:resourcetype>' + _cup(user)
             + f'<D:principal-URL><D:href>{escape(_principal_href(user))}</D:href>'
             '</D:principal-URL>')
    return _multistatus([_ok("/caldav/", props)])


@caldav_bp.route("/caldav/principals/<username>/", methods=["PROPFIND", "OPTIONS"])
def principal(username):
    if request.method == "OPTIONS":
        return _options()
    user = _require(username)
    props = ('<D:resourcetype><D:principal/></D:resourcetype>' + _cup(user)
             + f'<C:calendar-home-set><D:href>{escape(_home_href(user))}</D:href>'
             '</C:calendar-home-set>'
             f'<D:displayname>{escape(str(user))}</D:displayname>')
    return _multistatus([_ok(_principal_href(user), props)])


@caldav_bp.route("/caldav/calendars/<username>/", methods=["PROPFIND", "OPTIONS"])
def home(username):
    if request.method == "OPTIONS":
        return _options()
    user = _require(username)
    home_props = ('<D:resourcetype><D:collection/></D:resourcetype>'
                  '<D:displayname>Cairn</D:displayname>' + _cup(user))
    responses = [_ok(_home_href(user), home_props)]
    if request.headers.get("Depth", "0") != "0":
        # One pass over the tree groups tasks per collection so we can stamp
        # each collection's ctag without re-walking per list.
        mapping, all_tasks = _colid_map(user)
        by_col = {}
        for t in all_tasks:
            by_col.setdefault(mapping[str(t.pk)], []).append(t)
        for colid, label, color in _collections(user, with_tasks=set(by_col)):
            responses.append(_ok(
                _collection_href(user, colid),
                _collection_props(user, label, color, by_col.get(colid, [])),
            ))
        # The read-only VEVENT feed (tasks as calendar events) — opt-out.
        if _calendar_enabled(user):
            source = _calendar_source(user)
            responses.append(_ok(
                _collection_href(user, _CALENDAR),
                _calendar_collection_props(user, _calendar_tasks(user, source)),
            ))
    return _multistatus(responses)


@caldav_bp.route("/caldav/calendars/<username>/<colid>/", strict_slashes=False,
                 methods=["PROPFIND", "REPORT", "PROPPATCH", "OPTIONS"])
def collection(username, colid):
    if request.method == "OPTIONS":
        return _options()
    user = _require(username)

    if colid == _CALENDAR:
        if not _calendar_enabled(user):
            abort(404)
        if request.method == "PROPPATCH":
            return _multistatus([_ok(_collection_href(user, colid), "")])
        source = _calendar_source(user)
        tasks = _calendar_tasks(user, source)
        if request.method == "REPORT":
            return _report_calendar(user, tasks, source)
        responses = [_ok(_collection_href(user, colid),
                         _calendar_collection_props(user, tasks))]
        if request.headers.get("Depth", "0") != "0":
            for t in tasks:
                rprops = (f'<D:getetag>{escape(_etag(t))}</D:getetag>'
                          '<D:getcontenttype>text/calendar; component=vevent'
                          '</D:getcontenttype><D:resourcetype/>')
                responses.append(_ok(_resource_href(user, colid, _event_uid(t)), rprops))
        return _multistatus(responses)

    proj = _resolve_collection(user, colid)  # validates / 404s
    if proj is None:
        label, color = "Inbox", "#8E8E93"
    else:
        paths, _ = _container_paths(user)
        label = paths.get(colid, proj.name)
        color = proj.color or "#1F6FEB"

    if request.method == "PROPPATCH":
        return _multistatus([_ok(_collection_href(user, colid), "")])

    mapping, all_tasks = _colid_map(user)
    tasks = [t for t in all_tasks if mapping[str(t.pk)] == colid]

    if request.method == "REPORT":
        return _report(user, colid, tasks)

    responses = [_ok(_collection_href(user, colid),
                     _collection_props(user, label, color, tasks))]
    if request.headers.get("Depth", "0") != "0":
        for t in tasks:
            rprops = (f'<D:getetag>{escape(_etag(t))}</D:getetag>'
                      '<D:getcontenttype>text/calendar; component=vtodo'
                      '</D:getcontenttype><D:resourcetype/>')
            responses.append(_ok(_resource_href(user, colid, _uid(t)), rprops))
    return _multistatus(responses)


def _data_response(href, item):
    props = (f'<D:getetag>{escape(_etag(item))}</D:getetag>'
             f'<C:calendar-data>{escape(_todo_ics(item))}</C:calendar-data>')
    return _ok(href, props)


def _report(user, colid, tasks):
    try:
        root_el = ET.fromstring(request.get_data() or b"")
    except ET.ParseError:
        root_el = None
    if root_el is not None and root_el.tag.endswith("calendar-multiget"):
        # Answer only for what this collection currently serves: a client may
        # still ask for a task it cached before the task was parked (or moved
        # away), and handing the data back would resurrect the reminder.
        visible = {str(t.pk) for t in tasks}
        responses = []
        for href_el in root_el.iter("{DAV:}href"):
            href = (href_el.text or "").strip()
            item = _find(user, href.rstrip("/").split("/")[-1])
            if item is not None and str(item.pk) in visible:
                responses.append(_data_response(href, item))
        return _multistatus(responses)
    # calendar-query (or anything else) → the whole collection
    return _multistatus([_data_response(_resource_href(user, colid, _uid(t)), t)
                         for t in tasks])


def _event_data_response(href, item, source):
    props = (f'<D:getetag>{escape(_etag(item))}</D:getetag>'
             f'<C:calendar-data>{escape(_event_ics(item, source))}</C:calendar-data>')
    return _ok(href, props)


def _report_calendar(user, tasks, source):
    try:
        root_el = ET.fromstring(request.get_data() or b"")
    except ET.ParseError:
        root_el = None
    if root_el is not None and root_el.tag.endswith("calendar-multiget"):
        responses = []
        for href_el in root_el.iter("{DAV:}href"):
            href = (href_el.text or "").strip()
            item = _find_event(user, href.rstrip("/").split("/")[-1])
            if item is not None:
                responses.append(_event_data_response(href, item, source))
        return _multistatus(responses)
    return _multistatus([
        _event_data_response(_resource_href(user, _CALENDAR, _event_uid(t)), t, source)
        for t in tasks])


def _colid_from_path(path):
    """Pull the collection id out of a CalDAV resource path/URL."""
    parts = [p for p in urlparse(path).path.split("/") if p]
    # …/caldav/calendars/<user>/<colid>/<name.ics>
    if len(parts) >= 2 and parts[-1].lower().endswith(".ics"):
        return unquote(parts[-2])
    return None


@caldav_bp.route("/caldav/calendars/<username>/<colid>/<name>",
                 methods=["GET", "HEAD", "PUT", "DELETE", "MOVE", "OPTIONS"])
def resource(username, colid, name):
    if request.method == "OPTIONS":
        return _options()
    user = _require(username)

    if colid == _CALENDAR:
        if not _calendar_enabled(user):
            abort(404)
        # Read-only feed: serve the VEVENT, refuse writes.
        if request.method in ("GET", "HEAD"):
            item = _find_event(user, name)
            if item is None:
                abort(404)
            resp = Response(_event_ics(item, _calendar_source(user)),
                            mimetype="text/calendar; charset=utf-8")
            resp.headers["ETag"] = _etag(item)
            return resp
        abort(403)

    if request.method in ("GET", "HEAD"):
        item = _find(user, name)
        if item is None:
            abort(404)
        resp = Response(_todo_ics(item), mimetype="text/calendar; charset=utf-8")
        resp.headers["ETag"] = _etag(item)
        return resp

    if request.method == "PUT":
        proj = _resolve_collection(user, colid)  # validates target list
        uid = name[:-4] if name.endswith(".ics") else name
        item = _find(user, uid)
        created = item is None
        if created:
            item = Item(owner=user, kind="task", status="open", parent=proj)
            if not uid.startswith("cairn-"):
                item.caldav_uid = uid
        elif _nearest_colid(item) != colid:
            # Genuine move to a different list. (Same-list edits keep the
            # parent so a task nested in a section isn't yanked to the root.)
            item.parent = proj
        raw = request.get_data()
        # Opt-in wire dump (BKTODO_CALDAV_DEBUG=1) so we can see exactly what a
        # client sends — invaluable for mapping Apple-specific fields.
        if current_app.config.get("CALDAV_DEBUG"):
            current_app.logger.info(
                "CalDAV PUT %s by %s:\n%s",
                request.path, user.username,
                raw.decode("utf-8", "replace")[:6000],
            )
        if not _apply_ics(item, raw, user):
            abort(400)
        item.save()
        resp = Response(status=201 if created else 204)
        resp.headers["ETag"] = _etag(item)
        return resp

    if request.method == "MOVE":
        item = _find(user, name)
        if item is None:
            abort(404)
        dest_colid = _colid_from_path(request.headers.get("Destination", ""))
        if dest_colid is None:
            abort(400)
        item.parent = _resolve_collection(user, dest_colid)
        item.save()
        return Response(status=201)

    if request.method == "DELETE":
        item = _find(user, name)
        if item is None:
            abort(404)
        # If it already moved to another list, the resource is "gone" from
        # here — don't trash it (handles client move = PUT-new + DELETE-old).
        # Same answer for a task we stopped serving because it is parked: the
        # client is catching up with a disappearance, not deleting a task.
        if _nearest_colid(item) != colid or _held_back(item):
            return Response(status=204)
        item.trashed_at = _utcnow()
        item.save()
        return Response(status=204)

    abort(405)
