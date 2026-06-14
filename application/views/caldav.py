"""A minimal CalDAV server exposing each user's tasks as VTODO items.

Lets a standard CalDAV client — Apple Reminders, Things, Tasks.org … — use
Cairn as a read/write task backend. Scope of this first version:

* One calendar collection per user ("Cairn") holding all their tasks, flat.
  (Per-project collections are a later iteration; iCalendar VTODO has no good
  cross-client notion of folders/sections, so the hierarchy flattens here.)
* HTTP Basic auth (username **or** email + password) — clients send it over
  HTTPS. The blueprint is CSRF-exempt (no browser form posts).
* Apple-style change detection via ``CS:getctag`` + ``calendar-query`` /
  ``calendar-multiget`` REPORTs (no ``sync-token`` yet — Reminders is happy
  with ctag polling).

Mapping (``Item`` ⇄ ``VTODO``):
  title↔SUMMARY, note↔DESCRIPTION, due_date↔DUE, defer_date↔DTSTART,
  priority↔PRIORITY (Cairn 1–4 ⇄ iCal 1/3/5/7), tags↔CATEGORIES,
  status: done→COMPLETED, dropped→CANCELLED, else NEEDS-ACTION.

This is deliberately small and standards-pragmatic; some clients are picky,
so expect to iterate against a real device.
"""
import base64
from datetime import datetime, timezone
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

from flask import Blueprint, Response, abort, redirect, request
from mongoengine import Q
from mongoengine.errors import ValidationError

from icalendar import Calendar, Todo

from application.models.item import Item
from application.models.user import User

caldav_bp = Blueprint("caldav", __name__)

_NS = ('xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" '
       'xmlns:CS="http://calendarserver.org/ns/" '
       'xmlns:ICAL="http://apple.com/ns/ical/"')

# Cairn priority (1 highest … 4 lowest) ⇄ iCalendar PRIORITY (1 highest …
# 9 lowest, 0 = undefined).
_CAIRN_TO_ICAL_PRIO = {1: 1, 2: 3, 3: 5, 4: 7}


def _utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- auth ----

def _authenticate():
    """Return the User for the request's Basic credentials, or None."""
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
    """Authenticate; 401 if missing/bad, 403 if the URL targets another user."""
    user = _authenticate()
    if not user:
        resp = Response("Authentication required", 401)
        resp.headers["WWW-Authenticate"] = 'Basic realm="Cairn CalDAV"'
        abort(resp)
    if username is not None and username != user.username:
        abort(403)
    return user


# -------------------------------------------------------------- hrefs ----

def _principal_href(u):
    return f"/caldav/principals/{u.username}/"


def _home_href(u):
    return f"/caldav/calendars/{u.username}/"


def _collection_href(u):
    return f"/caldav/calendars/{u.username}/tasks/"


def _resource_href(u, uid):
    return f"/caldav/calendars/{u.username}/tasks/{uid}.ics"


def _uid(item):
    return item.caldav_uid or f"cairn-{item.pk}"


def _etag(item):
    ts = int((item.updated_at or item.created_at or _utcnow()).timestamp())
    return f'"{item.pk}-{ts}"'


def _user_tasks(user):
    return list(Item.objects(owner=user, kind="task").order_by("-updated_at"))


def _ctag(user):
    """A collection tag that changes on any add / edit / delete."""
    latest, count = 0, 0
    for t in Item.objects(owner=user, kind="task").only("updated_at", "created_at"):
        count += 1
        ts = int((t.updated_at or t.created_at or _utcnow()).timestamp())
        latest = max(latest, ts)
    return f'"{count}-{latest}"'


def _find(user, uid):
    """Resolve a resource UID back to the user's Item, or None."""
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
    # all-day DATE → midnight UTC
    return datetime(val.year, val.month, val.day, tzinfo=timezone.utc)


def _todo_ics(item):
    """Serialise one Item to a VCALENDAR/VTODO string."""
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
    """Apply an inbound VTODO to ``item`` (unsaved). Returns False if no VTODO."""
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
    done_by_pct = pc is not None and int(pc) >= 100
    if st == "COMPLETED" or done_by_pct:
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


def _collection_props(user):
    return ('<D:resourcetype><D:collection/><C:calendar/></D:resourcetype>'
            '<D:displayname>Cairn</D:displayname>'
            '<C:supported-calendar-component-set><C:comp name="VTODO"/>'
            '</C:supported-calendar-component-set>'
            f'<CS:getctag>{escape(_ctag(user))}</CS:getctag>'
            '<ICAL:calendar-color>#1F6FEB</ICAL:calendar-color>'
            + _cup(user))


def _options():
    resp = Response(status=200)
    resp.headers["DAV"] = "1, 2, 3, calendar-access"
    resp.headers["Allow"] = ("OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, "
                             "PROPPATCH, REPORT, MKCALENDAR")
    return resp


# -------------------------------------------------------------- routes ----

@caldav_bp.route("/.well-known/caldav")
def wellknown():
    return redirect("/caldav/", code=301)


@caldav_bp.route("/caldav/", methods=["PROPFIND", "OPTIONS"])
def root():
    if request.method == "OPTIONS":
        return _options()
    user = _require()
    props = ('<D:resourcetype><D:collection/></D:resourcetype>'
             + _cup(user)
             + f'<D:principal-URL><D:href>{escape(_principal_href(user))}</D:href>'
             '</D:principal-URL>')
    return _multistatus([_ok("/caldav/", props)])


@caldav_bp.route("/caldav/principals/<username>/", methods=["PROPFIND", "OPTIONS"])
def principal(username):
    if request.method == "OPTIONS":
        return _options()
    user = _require(username)
    props = ('<D:resourcetype><D:principal/></D:resourcetype>'
             + _cup(user)
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
        responses.append(_ok(_collection_href(user), _collection_props(user)))
    return _multistatus(responses)


@caldav_bp.route("/caldav/calendars/<username>/tasks/", strict_slashes=False,
                 methods=["PROPFIND", "REPORT", "PROPPATCH", "OPTIONS"])
def collection(username):
    if request.method == "OPTIONS":
        return _options()
    user = _require(username)
    if request.method == "PROPPATCH":
        # Pretend any property set (displayname / color) succeeded so the
        # client doesn't treat the collection as read-broken.
        return _multistatus([_ok(_collection_href(user), "")])
    if request.method == "REPORT":
        return _report(user)
    # PROPFIND
    responses = [_ok(_collection_href(user), _collection_props(user))]
    if request.headers.get("Depth", "0") != "0":
        for t in _user_tasks(user):
            rprops = (f'<D:getetag>{escape(_etag(t))}</D:getetag>'
                      '<D:getcontenttype>text/calendar; component=vtodo'
                      '</D:getcontenttype><D:resourcetype/>')
            responses.append(_ok(_resource_href(user, _uid(t)), rprops))
    return _multistatus(responses)


def _data_response(href, item):
    props = (f'<D:getetag>{escape(_etag(item))}</D:getetag>'
             f'<C:calendar-data>{escape(_todo_ics(item))}</C:calendar-data>')
    return _ok(href, props)


def _report(user):
    try:
        root_el = ET.fromstring(request.get_data() or b"")
    except ET.ParseError:
        root_el = None
    tag = root_el.tag if root_el is not None else ""
    if tag.endswith("calendar-multiget"):
        responses = []
        for href_el in root_el.iter("{DAV:}href"):
            href = (href_el.text or "").strip()
            item = _find(user, href.rstrip("/").split("/")[-1])
            if item is not None:
                responses.append(_data_response(href, item))
        return _multistatus(responses)
    # calendar-query (or anything else) → return the whole collection
    return _multistatus([_data_response(_resource_href(user, _uid(t)), t)
                         for t in _user_tasks(user)])


@caldav_bp.route("/caldav/calendars/<username>/tasks/<name>",
                 methods=["GET", "HEAD", "PUT", "DELETE", "OPTIONS"])
def resource(username, name):
    if request.method == "OPTIONS":
        return _options()
    user = _require(username)

    if request.method in ("GET", "HEAD"):
        item = _find(user, name)
        if item is None:
            abort(404)
        resp = Response(_todo_ics(item), mimetype="text/calendar; charset=utf-8")
        resp.headers["ETag"] = _etag(item)
        return resp

    if request.method == "PUT":
        uid = name[:-4] if name.endswith(".ics") else name
        item = _find(user, uid)
        created = item is None
        if created:
            item = Item(owner=user, kind="task", status="open")
            # Client-chosen UID → remember it so future ops map back. A
            # cairn-<pk> UID is derived, never stored.
            if not uid.startswith("cairn-"):
                item.caldav_uid = uid
        if not _apply_ics(item, request.get_data()):
            abort(400)
        item.save()
        resp = Response(status=201 if created else 204)
        resp.headers["ETag"] = _etag(item)
        return resp

    if request.method == "DELETE":
        item = _find(user, name)
        if item is None:
            abort(404)
        item.trashed_at = _utcnow()
        item.save()
        return Response(status=204)

    abort(405)
