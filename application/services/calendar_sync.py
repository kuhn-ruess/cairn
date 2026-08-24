"""Calendar event fetching for external calendars.

Two flavours of provider live behind the same ``fetch_events`` API:

* **google** — full OAuth (read/write capable). Tokens are refreshed in
  place when expired. Events come from the Calendar v3 REST API.
* **ical** — read-only subscription via secret/public ICS URL. Useful
  when the user doesn't want to wire up OAuth.

The OAuth client credentials come from configuration (set
``BKTODO_GOOGLE_CLIENT_ID`` / ``BKTODO_GOOGLE_CLIENT_SECRET``).
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from flask import current_app

log = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_API_URL = "https://www.googleapis.com/calendar/v3"
# Read/write so the Today integration can later push tasks to the calendar.
GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar"


@dataclass
class CalendarEvent:
    """Normalised event regardless of provider."""

    uid: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    url: str = ""
    calendar_color: str = "#458588"
    calendar_name: str = ""
    calendar_pk: str = ""   # owning ExternalCalendar.pk_ref, for note linking

    def as_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "all_day": self.all_day,
            "location": self.location,
            "url": self.url,
            "calendar_color": self.calendar_color,
            "calendar_name": self.calendar_name,
            "calendar_pk": self.calendar_pk,
        }


class CalendarError(RuntimeError):
    pass


# ----- OAuth flow ----------------------------------------------------------


def _google_creds(user=None):
    """Resolve (client_id, client_secret) for the OAuth flow.

    Single OAuth client per deployment — registered once by the admin via
    ``BKTODO_GOOGLE_CLIENT_ID`` + ``BKTODO_GOOGLE_CLIENT_SECRET``. End
    users never see the credentials; they just click "Connect".
    """
    return (
        current_app.config.get("GOOGLE_CLIENT_ID") or None,
        current_app.config.get("GOOGLE_CLIENT_SECRET") or None,
    )


def google_oauth_authorize_url(redirect_uri: str, state: str, user=None) -> str:
    client_id, _ = _google_creds(user)
    if not client_id:
        raise CalendarError(
            "Google OAuth client ID missing. Paste your client_id and "
            "client_secret in Settings → External calendars first."
        )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",   # force refresh-token issuance on every flow
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def google_oauth_exchange_code(code: str, redirect_uri: str, user=None) -> dict[str, Any]:
    """Trade ``code`` for access + refresh tokens."""
    client_id, client_secret = _google_creds(user)
    if not client_id or not client_secret:
        raise CalendarError("Google OAuth credentials missing.")
    return _post_form(
        GOOGLE_TOKEN_URL,
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )


def google_oauth_refresh(refresh_token: str, user=None) -> dict[str, Any]:
    client_id, client_secret = _google_creds(user)
    if not client_id or not client_secret:
        raise CalendarError("Google OAuth credentials missing.")
    return _post_form(
        GOOGLE_TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )


# ----- Event fetch ---------------------------------------------------------


def fetch_events(calendar, since: datetime, until: datetime, *, user=None) -> list[CalendarEvent]:
    """Public entry point: get events from any provider.

    ``user`` is required because the embedded ``ExternalCalendar``
    can't persist itself — we need to save the owning user so
    ``last_synced_at`` / ``last_error`` / refreshed OAuth tokens
    stick. iCal-only callers can still pass ``user=None``; nothing
    persists in that case (the fields stay in-memory)."""
    try:
        if calendar.provider == "google":
            if user is None:
                raise CalendarError("Google calendar fetch needs the owning user.")
            events = _fetch_google(calendar, user, since, until)
        elif calendar.provider == "ical":
            events = _fetch_ical(calendar, since, until)
        elif calendar.provider == "caldav":
            if user is None:
                raise CalendarError("CalDAV calendar fetch needs the owning user.")
            events = _fetch_caldav(calendar, user, since, until)
        else:
            raise CalendarError(f"Unknown provider: {calendar.provider}")
    except CalendarError as exc:
        calendar.last_error = str(exc)
        if user is not None:
            user.save()
        raise
    if user is not None:
        user.save()
    return events


def _ensure_google_access_token(calendar, user) -> str:
    """Refresh the access token if it's missing or expired.

    ``user`` is required: calendars are embedded on the User document
    now (no back-reference), so the caller has to pass the owner
    explicitly so we can encrypt / decrypt OAuth tokens against the
    right user secret and save updated tokens back."""
    from application.services.crypto import decrypt_for_user, encrypt_for_user

    now = datetime.now(timezone.utc)
    if calendar.oauth_access_token and calendar.oauth_token_expires_at:
        if calendar.oauth_token_expires_at > now + timedelta(seconds=60):
            return decrypt_for_user(user, calendar.oauth_access_token)

    refresh = decrypt_for_user(user, calendar.oauth_refresh_token)
    if not refresh:
        raise CalendarError("No OAuth refresh token stored.")
    data = google_oauth_refresh(refresh, user=user)
    new_access = data["access_token"]
    calendar.oauth_access_token = encrypt_for_user(user, new_access)
    calendar.oauth_token_expires_at = now + timedelta(
        seconds=int(data.get("expires_in", 3600)) - 30
    )
    # Persist the refreshed access token by saving the parent user
    # (embedded docs ride along with the parent ``.save()``).
    user.save()
    return new_access


def google_list_calendars(calendar, user) -> list[dict]:
    """List every calendar visible to the OAuth-connected account.

    Used right after a fresh OAuth grant so we can materialise one
    embedded ``ExternalCalendar`` per real Google calendar (primary,
    shared, secondary, holiday subscriptions, …). Returns the raw
    items from Google's calendarList endpoint.
    """
    token = _ensure_google_access_token(calendar, user)
    url = f"{GOOGLE_API_URL}/users/me/calendarList?showHidden=false&maxResults=250"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise CalendarError(f"Google calendarList {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise CalendarError(f"network error listing calendars: {exc}") from exc
    return payload.get("items", [])


def _fallback_tz() -> str:
    """Server-wide default tz when the user hasn't picked one yet."""
    try:
        tz = current_app.config.get("DEFAULT_TZ")
    except RuntimeError:
        tz = None
    return tz or "UTC"


def _event_body(title: str, when: datetime, all_day: bool, description: str,
                duration_minutes: int = 30, timezone: str | None = None) -> dict:
    if all_day:
        date = when.date().isoformat()
        return {
            "summary": title,
            "description": description or "",
            "start": {"date": date},
            "end": {"date": date},
        }
    end_when = when + timedelta(minutes=duration_minutes)
    if when.tzinfo is not None:
        # Aware → offset in the iso string is enough.
        return {
            "summary": title,
            "description": description or "",
            "start": {"dateTime": when.isoformat()},
            "end": {"dateTime": end_when.isoformat()},
        }
    # Naive → tell Google how to interpret the wall-clock value.
    tz = timezone or _fallback_tz()
    return {
        "summary": title,
        "description": description or "",
        "start": {"dateTime": when.isoformat(), "timeZone": tz},
        "end": {"dateTime": end_when.isoformat(), "timeZone": tz},
    }


def google_create_event(calendar, user, *, title: str, when: datetime,
                        all_day: bool = True, description: str = "",
                        duration_minutes: int = 30,
                        timezone: str | None = None) -> dict:
    """Push a task to a Google calendar as a single all-day or timed event."""
    token = _ensure_google_access_token(calendar, user)
    cal_id = calendar.calendar_id or "primary"
    body = _event_body(title, when, all_day, description, duration_minutes, timezone)
    url = f"{GOOGLE_API_URL}/calendars/{urllib.parse.quote(cal_id)}/events"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", "replace")[:300]
        raise CalendarError(f"Google insert event {exc.code}: {body_txt}") from exc


def google_patch_event(calendar, user, event_id: str, *, title: str, when: datetime,
                       all_day: bool = True, description: str = "",
                       duration_minutes: int = 30,
                       timezone: str | None = None) -> dict:
    """Replace an existing event's title/time. PUT keeps the same id."""
    token = _ensure_google_access_token(calendar, user)
    cal_id = calendar.calendar_id or "primary"
    body = _event_body(title, when, all_day, description, duration_minutes, timezone)
    url = (f"{GOOGLE_API_URL}/calendars/{urllib.parse.quote(cal_id)}"
           f"/events/{urllib.parse.quote(event_id)}")
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", "replace")[:300]
        raise CalendarError(f"Google patch event {exc.code}: {body_txt}") from exc


def google_delete_event(calendar, user, event_id: str) -> None:
    """Best-effort delete. 404/410 (already gone) is treated as success."""
    token = _ensure_google_access_token(calendar, user)
    cal_id = calendar.calendar_id or "primary"
    url = (f"{GOOGLE_API_URL}/calendars/{urllib.parse.quote(cal_id)}"
           f"/events/{urllib.parse.quote(event_id)}")
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return
        body_txt = exc.read().decode("utf-8", "replace")[:300]
        raise CalendarError(f"Google delete event {exc.code}: {body_txt}") from exc


def _fetch_google(calendar, user, since: datetime, until: datetime) -> list[CalendarEvent]:
    token = _ensure_google_access_token(calendar, user)
    cal_id = calendar.calendar_id or "primary"
    params = urllib.parse.urlencode({
        "timeMin": since.isoformat(),
        "timeMax": until.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "100",
    })
    url = f"{GOOGLE_API_URL}/calendars/{urllib.parse.quote(cal_id)}/events?{params}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise CalendarError(f"Google API {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise CalendarError(f"network error: {exc}") from exc

    events: list[CalendarEvent] = []
    for it in payload.get("items", []):
        start_raw = it.get("start", {})
        end_raw = it.get("end", {})
        start, all_day = _parse_google_dt(start_raw)
        end, _ = _parse_google_dt(end_raw)
        if not start or not end:
            continue
        events.append(CalendarEvent(
            uid=str(it.get("id", "")),
            title=it.get("summary", "(no title)"),
            start=start,
            end=end,
            all_day=all_day,
            location=it.get("location", "") or "",
            url=it.get("htmlLink", "") or "",
            calendar_color=calendar.color,
            calendar_name=calendar.name,
            calendar_pk=str(getattr(calendar, "pk_ref", "") or ""),
        ))
    calendar.last_synced_at = datetime.now(timezone.utc)
    calendar.last_error = ""
    # Embedded — persistence happens via the parent ``user.save()`` in
    # ``fetch_events``.
    return events


def _parse_google_dt(raw):
    if "dateTime" in raw:
        return _parse_iso(raw["dateTime"]), False
    if "date" in raw:
        # All-day event: date string YYYY-MM-DD. Treat as midnight UTC.
        return _parse_iso(raw["date"] + "T00:00:00+00:00"), True
    return None, False


def _parse_iso(s: str) -> datetime:
    # Python <3.11 doesn't accept trailing Z; normalise.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


# ----- iCal fetch ----------------------------------------------------------


def _fetch_ical_data(url: str) -> bytes:
    from application.services.http_safety import UnsafeURLError, safe_urlopen
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "bktodo/calendar (+https://localhost)",
        })
        # safe_urlopen validates the URL AND every redirect hop — a bare
        # urlopen would follow a 302 into an internal address.
        with safe_urlopen(req, timeout=15) as resp:
            return resp.read()
    except UnsafeURLError as exc:
        # Don't leak the resolved IP back to the caller — keep the
        # message generic so it's safe to flash to the user.
        raise CalendarError(f"refusing to fetch: {exc}") from exc
    except urllib.error.HTTPError as exc:
        raise CalendarError(f"iCal fetch {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise CalendarError(f"network error: {exc}") from exc


def probe_ical(url: str) -> dict:
    """Fetch + parse just the calendar header. Used by the add-form to
    auto-detect the calendar's display name (``X-WR-CALNAME``) and verify
    the URL works *before* we save it."""
    data = _fetch_ical_data(url)
    try:
        from icalendar import Calendar as ICalendar
    except ImportError as exc:
        raise CalendarError("icalendar library not installed") from exc
    parsed = ICalendar.from_ical(data)
    name = str(parsed.get("X-WR-CALNAME", "") or parsed.get("NAME", "") or "")
    color = str(parsed.get("X-APPLE-CALENDAR-COLOR", "") or parsed.get("COLOR", "") or "")
    return {"name": name.strip(), "color": color.strip(), "event_count": sum(
        1 for _ in parsed.walk("VEVENT")
    )}


def _fetch_ical(calendar, since: datetime, until: datetime) -> list[CalendarEvent]:
    if not calendar.ical_url:
        raise CalendarError("No iCal URL set.")
    data = _fetch_ical_data(calendar.ical_url)
    try:
        from icalendar import Calendar as ICalendar
    except ImportError as exc:
        raise CalendarError("icalendar library not installed") from exc

    parsed = ICalendar.from_ical(data)
    events: list[CalendarEvent] = []
    for comp in parsed.walk("VEVENT"):
        ev = _vevent_to_event(comp, calendar)
        if ev is None or ev.start > until or ev.end < since:
            continue
        events.append(ev)
    calendar.last_synced_at = datetime.now(timezone.utc)
    calendar.last_error = ""
    # Embedded — caller (fetch_events) persists via the parent user.
    events.sort(key=lambda e: e.start)
    return events


def _vevent_to_event(comp, calendar) -> "CalendarEvent | None":
    """Convert one parsed ``VEVENT`` component into a normalised
    ``CalendarEvent`` (shared by the iCal + CalDAV readers). ``None`` when the
    component has no usable start."""
    start = comp.get("DTSTART")
    if not start:
        return None
    end = comp.get("DTEND") or start
    start_dt = _normalise_ical_dt(start.dt)
    end_dt = _normalise_ical_dt(end.dt) if end else start_dt
    all_day = not isinstance(start.dt, datetime)
    return CalendarEvent(
        uid=str(comp.get("UID", "")),
        title=str(comp.get("SUMMARY", "(no title)")),
        start=start_dt,
        end=end_dt,
        all_day=all_day,
        location=str(comp.get("LOCATION", "") or ""),
        url=str(comp.get("URL", "") or ""),
        calendar_color=calendar.color,
        calendar_name=calendar.name,
        calendar_pk=str(getattr(calendar, "pk_ref", "") or ""),
    )


def _normalise_ical_dt(value) -> datetime:
    """Convert a date-or-datetime from icalendar into a tz-aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    # ``date`` (all-day) → midnight UTC
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


# ----- CalDAV (read/write) -------------------------------------------------
#
# A CalDAV calendar is a WebDAV collection of ``.ics`` resources. We talk to it
# with plain ``urllib`` custom-method requests + HTTP Basic auth (the same
# pattern the Google writers use with ``method="PUT"``), and the ``icalendar``
# library for VEVENT build/parse. Events are read with a ``calendar-query``
# REPORT (time-range filtered) and written by PUTting one ``<uid>.ics`` resource
# per event (create/update) or DELETEing it. The remote event id we persist on
# the task is simply the resource UID.

CALDAV_NS = "urn:ietf:params:xml:ns:caldav"

_CALDAV_QUERY_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    '<d:prop><d:getetag/><c:calendar-data/></d:prop>'
    '<c:filter><c:comp-filter name="VCALENDAR">'
    '<c:comp-filter name="VEVENT">'
    '<c:time-range start="{start}" end="{end}"/>'
    '</c:comp-filter></c:comp-filter></c:filter>'
    '</c:calendar-query>'
)

_CALDAV_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
    '<d:prop><d:displayname/><d:resourcetype/><cs:getctag/></d:prop>'
    '</d:propfind>'
)


def _basic_auth(username: str, password: str) -> str:
    import base64
    raw = f"{username or ''}:{password or ''}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _caldav_auth_header(calendar, user) -> str:
    from application.services.crypto import decrypt_for_user
    pw = decrypt_for_user(user, calendar.caldav_password) if calendar.caldav_password else ""
    return _basic_auth(calendar.caldav_username or "", pw or "")


def _caldav_request(method: str, url: str, *, calendar=None, user=None, auth=None,
                    body: str | None = None, headers: dict | None = None,
                    allow_codes: tuple = (), timeout: int = 15):
    """Low-level CalDAV/WebDAV request. Basic auth + SSRF guard. Returns
    ``(status, text)``. HTTP codes in ``allow_codes`` are returned instead of
    raising (used so a DELETE of an already-gone resource is a success)."""
    from application.services.http_safety import UnsafeURLError, safe_urlopen
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", auth or _caldav_auth_header(calendar, user))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        # Every redirect hop is re-validated: this path issues PUT/DELETE/
        # PROPFIND, so a followed 302 would be an arbitrary-method request
        # against an internal service.
        with safe_urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except UnsafeURLError as exc:
        raise CalendarError(f"refusing to reach: {exc}") from exc
    except urllib.error.HTTPError as exc:
        if exc.code in allow_codes:
            return exc.code, ""
        text = exc.read().decode("utf-8", "replace")[:300]
        raise CalendarError(f"CalDAV {method} {exc.code}: {text}") from exc
    except urllib.error.URLError as exc:
        raise CalendarError(f"network error: {exc}") from exc


def _caldav_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _caldav_href(calendar, uid: str) -> str:
    base = calendar.caldav_url or ""
    if not base.endswith("/"):
        base += "/"
    return urllib.parse.urljoin(base, urllib.parse.quote(uid) + ".ics")


def probe_caldav(url: str, username: str, password: str) -> dict:
    """Verify a CalDAV collection URL + credentials before saving, and pick up
    its display name. A ``207 Multi-Status`` (or any 2xx) means the collection
    is reachable and the credentials are accepted."""
    status, text = _caldav_request(
        "PROPFIND", url, auth=_basic_auth(username, password),
        body=_CALDAV_PROPFIND_BODY,
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
    )
    name = ""
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(text)
        dn = root.find(".//{DAV:}displayname")
        if dn is not None and dn.text:
            name = dn.text.strip()
    except Exception:  # noqa: BLE001 — a missing displayname is non-fatal
        pass
    return {"name": name}


def _fetch_caldav(calendar, user, since: datetime, until: datetime) -> list[CalendarEvent]:
    if not calendar.caldav_url:
        raise CalendarError("No CalDAV URL set.")
    try:
        from icalendar import Calendar as ICalendar
    except ImportError as exc:
        raise CalendarError("icalendar library not installed") from exc
    body = _CALDAV_QUERY_BODY.format(start=_caldav_stamp(since), end=_caldav_stamp(until))
    _status, text = _caldav_request(
        "REPORT", calendar.caldav_url, calendar=calendar, user=user, body=body,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
    )
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise CalendarError(f"bad CalDAV response: {exc}") from exc
    events: list[CalendarEvent] = []
    for node in root.iter(f"{{{CALDAV_NS}}}calendar-data"):
        raw = (node.text or "").strip()
        if not raw:
            continue
        try:
            parsed = ICalendar.from_ical(raw)
        except Exception:  # noqa: BLE001 — skip a single unparseable resource
            continue
        for comp in parsed.walk("VEVENT"):
            ev = _vevent_to_event(comp, calendar)
            if ev is None or ev.start > until or ev.end < since:
                continue
            events.append(ev)
    calendar.last_synced_at = datetime.now(timezone.utc)
    calendar.last_error = ""
    events.sort(key=lambda e: e.start)
    return events


def _caldav_event_ics(uid: str, title: str, when: datetime, all_day: bool,
                      description: str, duration_minutes: int,
                      tz_name: str | None) -> str:
    from icalendar import Calendar as ICalendar, Event as IEvent
    cal = ICalendar()
    cal.add("prodid", "-//Cairn//Calendar//EN")
    cal.add("version", "2.0")
    ev = IEvent()
    ev.add("uid", uid)
    ev.add("summary", title or "")
    if description:
        ev.add("description", description)
    if all_day:
        ev.add("dtstart", when.date())
        ev.add("dtend", (when + timedelta(days=1)).date())
    else:
        w = when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
        ev.add("dtstart", w)
        ev.add("dtend", w + timedelta(minutes=max(duration_minutes, 5)))
    ev.add("dtstamp", datetime.now(timezone.utc))
    ev.add("last-modified", datetime.now(timezone.utc))
    cal.add_component(ev)
    return cal.to_ical().decode("utf-8")


def caldav_create_event(calendar, user, *, title: str, when: datetime,
                        all_day: bool = True, description: str = "",
                        duration_minutes: int = 30,
                        timezone: str | None = None) -> dict:
    """Create an event by PUTting a fresh ``<uid>.ics`` resource. Returns
    ``{"id": uid}`` so the caller stores the UID like a Google event id."""
    import uuid
    uid = f"cairn-{uuid.uuid4().hex}"
    ics = _caldav_event_ics(uid, title, when, all_day, description,
                            duration_minutes, timezone)
    _caldav_request("PUT", _caldav_href(calendar, uid), calendar=calendar, user=user,
                    body=ics, headers={"Content-Type": "text/calendar; charset=utf-8",
                                       "If-None-Match": "*"})
    return {"id": uid}


def caldav_patch_event(calendar, user, event_id: str, *, title: str, when: datetime,
                       all_day: bool = True, description: str = "",
                       duration_minutes: int = 30,
                       timezone: str | None = None) -> dict:
    """Update an event by re-PUTting its ``<uid>.ics`` (same UID = replace)."""
    ics = _caldav_event_ics(event_id, title, when, all_day, description,
                            duration_minutes, timezone)
    _caldav_request("PUT", _caldav_href(calendar, event_id), calendar=calendar, user=user,
                    body=ics, headers={"Content-Type": "text/calendar; charset=utf-8"})
    return {"id": event_id}


def caldav_delete_event(calendar, user, event_id: str) -> None:
    """Best-effort delete — 404/410 (already gone) counts as success."""
    _caldav_request("DELETE", _caldav_href(calendar, event_id), calendar=calendar,
                    user=user, allow_codes=(404, 410))


def _post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")[:300]
        raise CalendarError(f"OAuth {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise CalendarError(f"OAuth network error: {exc}") from exc


# ----- Multi-calendar helpers for the Today view ---------------------------
#
# An ALL-DAY event carries a date, not an instant: every reader above encodes
# it as midnight UTC (``_parse_google_dt`` / ``_normalise_ical_dt``). So it must
# never be converted into a zone — in Los Angeles that midnight is 17:00 the
# day before, which would file the event on the wrong day. Read the date
# straight off it; only timed events get ``astimezone``.
#
# That encoding is also why the fetch window has to be widened before it is
# filtered: an all-day event for the user's today sits at 00:00 UTC, which can
# be up to 12h outside the user's local day in either direction.
FETCH_SLACK = timedelta(hours=14)


def event_day(ev: CalendarEvent, tz) -> date:
    """The local date ``ev`` belongs to."""
    return ev.start.date() if ev.all_day else ev.start.astimezone(tz).date()


def events_in_window(events, since: datetime, until: datetime) -> list[CalendarEvent]:
    """Keep the events that belong to the local window ``[since, until)``.

    A timed event belongs to the day it STARTS on — that is the day whose list
    and timeline can render it. An all-day event is matched by date instead,
    over its whole span (``DTEND`` is exclusive), so a multi-day one shows on
    every day it covers.
    """
    first, last = since.date(), (until - timedelta(microseconds=1)).date()
    out = []
    for ev in events:
        if ev.all_day:
            ev_first = ev.start.date()
            ev_last = ev.end.date()
            if ev_last > ev_first:
                ev_last -= timedelta(days=1)
            if ev_first <= last and ev_last >= first:
                out.append(ev)
        elif since <= ev.start < until:
            out.append(ev)
    return out


def collect_next_event(user):
    """Find the next upcoming event across all "in Today" calendars.

    Used to populate the Today empty state with a "nothing today; next
    event …" hint. Looks ahead 30 days max.
    """
    now = datetime.now(timezone.utc)
    until = now + timedelta(days=30)
    best = None
    for cal in (user.calendars or []):
        if not cal.show_in_today:
            continue
        try:
            evs = fetch_events(cal, now, until)
        except CalendarError:
            continue
        for ev in evs:
            if ev.start <= now:
                continue
            if best is None or ev.start < best.start:
                best = ev
    return best
