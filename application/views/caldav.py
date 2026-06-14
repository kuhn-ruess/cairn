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
  title↔SUMMARY, note↔DESCRIPTION, due_date↔DUE, defer_date↔DTSTART,
  priority↔PRIORITY (Cairn 1–4 ⇄ iCal 1/3/5/7), tags↔CATEGORIES,
  status: done→COMPLETED, dropped→CANCELLED, else NEEDS-ACTION.

Standards-pragmatic and small; expect to iterate against a real device.
"""
import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

from flask import Blueprint, Response, abort, redirect, request
from mongoengine import Q
from mongoengine.errors import ValidationError

from icalendar import Calendar, Event, Todo

from application.models.item import Item
from application.models.user import User

caldav_bp = Blueprint("caldav", __name__)

_NS = ('xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" '
       'xmlns:CS="http://calendarserver.org/ns/" '
       'xmlns:ICAL="http://apple.com/ns/ical/"')

_INBOX = "inbox"
# Reserved collection id for the read-only VEVENT feed (tasks-as-events).
_CALENDAR = "calendar"
# Cairn priority (1 highest … 4 lowest) ⇄ iCalendar PRIORITY (1 highest …
# 9 lowest, 0 = undefined).
_CAIRN_TO_ICAL_PRIO = {1: 1, 2: 3, 3: 5, 4: 7}


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


def _collections(user):
    """[(colid, label, color)] — Inbox first, then every project A→Z."""
    cols = [(_INBOX, "Inbox", "#8E8E93")]
    for p in (Item.objects(owner=user, kind="project")
              .order_by("+title").only("pk", "title", "color")):
        cols.append((str(p.pk), p.name, p.color or "#1F6FEB"))
    return cols


def _colid_map(user):
    """Return ``({task_pk: colid}, [tasks])`` — colid is the nearest ancestor
    project's pk, or "inbox". Walks parent chains in-memory."""
    nodes = {}
    for c in (Item.objects(owner=user, kind__in=["project", "section", "folder"])
              .only("pk", "kind", "parent")):
        nodes[str(c.pk)] = c
    tasks = _user_tasks(user)
    for t in tasks:
        nodes.setdefault(str(t.pk), t)
    out = {}
    for t in tasks:
        cur, colid, seen, guard = t.parent, _INBOX, set(), 0
        while cur is not None and guard < 50:
            guard += 1
            pk = str(cur.pk)
            if pk in seen:
                break
            seen.add(pk)
            node = nodes.get(pk)
            if node is None:
                break
            if node.kind == "project":
                colid = pk
                break
            cur = node.parent
        out[str(t.pk)] = colid
    return out, tasks


def _nearest_colid(item):
    """The collection a single Item currently lives in (project pk / inbox)."""
    cur, seen, guard = item.parent, set(), 0
    while cur is not None and guard < 50:
        guard += 1
        if str(cur.pk) in seen:
            break
        seen.add(str(cur.pk))
        if getattr(cur, "kind", None) == "project":
            return str(cur.pk)
        cur = cur.parent
    return _INBOX


def _resolve_collection(user, colid):
    """The project Item for ``colid``; None for inbox; 404 if unknown."""
    if colid == _INBOX:
        return None
    try:
        proj = Item.objects(pk=colid, owner=user, kind="project").first()
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


def _to_dt(prop):
    val = getattr(prop, "dt", None)
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    return datetime(val.year, val.month, val.day, tzinfo=timezone.utc)


def _todo_ics(item):
    cal = Calendar()
    cal.add("prodid", "-//Cairn//CalDAV//EN")
    cal.add("version", "2.0")
    todo = Todo()
    todo.add("uid", _uid(item))
    todo.add("summary", item.title or "")
    if item.note:
        todo.add("description", item.note)
    if item.due_date:
        todo.add("due", _aware(item.due_date))
    if item.defer_date:
        todo.add("dtstart", _aware(item.defer_date))
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


def _apply_ics(item, raw):
    cal = Calendar.from_ical(raw)
    todo = next((c for c in cal.walk("VTODO")), None)
    if todo is None:
        return False
    item.title = (str(todo.get("summary") or "") or item.title or "Untitled")[:500]
    desc = todo.get("description")
    item.note = str(desc) if desc is not None else ""
    item.due_date = _to_dt(todo.get("due"))
    item.defer_date = _to_dt(todo.get("dtstart"))

    pr = todo.get("priority")
    if pr is None:
        item.priority = None
    else:
        pr = int(pr)
        item.priority = (None if pr == 0
                         else 1 if pr <= 2 else 2 if pr <= 4 else 3 if pr == 5 else 4)

    st = str(todo.get("status") or "").upper()
    pc = todo.get("percent-complete")
    if st == "COMPLETED" or (pc is not None and int(pc) >= 100):
        item.status = "done"
        item.completed_via = "caldav"
    elif st == "CANCELLED":
        item.status = "dropped"
        item.completed_via = "caldav"
    else:
        item.status = "open"

    cats = todo.get("categories")
    if cats is None:
        item.tags = []
    else:
        try:
            item.tags = [str(c) for c in cats.cats]
        except AttributeError:
            item.tags = [str(cats)]
    return True


# ------------------------------------------- calendar feed (VEVENT) ----

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
    if item.note:
        ev.add("description", item.note)
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


def _collection_props(user, label, color, tasks):
    return ('<D:resourcetype><D:collection/><C:calendar/></D:resourcetype>'
            f'<D:displayname>{escape(label)}</D:displayname>'
            '<C:supported-calendar-component-set><C:comp name="VTODO"/>'
            '</C:supported-calendar-component-set>'
            f'<CS:getctag>{escape(_ctag(tasks))}</CS:getctag>'
            f'<ICAL:calendar-color>{escape(color)}</ICAL:calendar-color>'
            + _cup(user))


def _calendar_collection_props(user, tasks):
    return ('<D:resourcetype><D:collection/><C:calendar/></D:resourcetype>'
            '<D:displayname>Cairn (Kalender)</D:displayname>'
            '<C:supported-calendar-component-set><C:comp name="VEVENT"/>'
            '</C:supported-calendar-component-set>'
            f'<CS:getctag>{escape(_ctag(tasks))}</CS:getctag>'
            '<ICAL:calendar-color>#34C759</ICAL:calendar-color>'
            + _cup(user))


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
        for colid, label, color in _collections(user):
            responses.append(_ok(
                _collection_href(user, colid),
                _collection_props(user, label, color, by_col.get(colid, [])),
            ))
        # The read-only VEVENT feed (tasks as calendar events).
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
    label = "Inbox" if proj is None else proj.name
    color = "#8E8E93" if proj is None else (proj.color or "#1F6FEB")

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
        responses = []
        for href_el in root_el.iter("{DAV:}href"):
            href = (href_el.text or "").strip()
            item = _find(user, href.rstrip("/").split("/")[-1])
            if item is not None:
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
        if not _apply_ics(item, request.get_data()):
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
        if _nearest_colid(item) != colid:
            return Response(status=204)
        item.trashed_at = _utcnow()
        item.save()
        return Response(status=204)

    abort(405)
