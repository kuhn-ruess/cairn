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
from dataclasses import dataclass, replace
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
    # Does this event make its owner UNAVAILABLE? A calendar says so itself —
    # iCalendar's ``TRANSP``, Google's ``transparency`` — and says no
    # surprisingly often: an all-day entry is created as "free" by default,
    # which is what a birthday, a holiday or a "Homeoffice" marker is. Only
    # the booking page asks; everything else shows the event either way.
    busy: bool = True
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
            "busy": self.busy,
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
                duration_minutes: int = 30, timezone: str | None = None,
                location: str = "", alarm_minutes: int = 0) -> dict:
    """The Google event. ``location`` is where a meeting link BELONGS: that
    is the field every calendar turns into a join button, on the phone's lock
    screen too. In the description it is a line of text somebody has to find
    and copy.

    ``alarm_minutes`` is Google's spelling of a VALARM: an override replacing
    the calendar's default, because a planned block's reminder is the user's
    setting in Cairn (``services/reminders.py``) and not whatever that calendar
    happens to do to every entry. ``0`` leaves the default alone."""
    body = {"summary": title, "description": description or ""}
    if location:
        body["location"] = location
    if alarm_minutes > 0:
        body["reminders"] = {"useDefault": False,
                             "overrides": [{"method": "popup",
                                            "minutes": int(alarm_minutes)}]}
    if all_day:
        date = when.date().isoformat()
        body["start"] = {"date": date}
        body["end"] = {"date": date}
        return body
    end_when = when + timedelta(minutes=duration_minutes)
    if when.tzinfo is not None:
        # Aware → offset in the iso string is enough.
        body["start"] = {"dateTime": when.isoformat()}
        body["end"] = {"dateTime": end_when.isoformat()}
        return body
    # Naive → tell Google how to interpret the wall-clock value.
    tz = timezone or _fallback_tz()
    body["start"] = {"dateTime": when.isoformat(), "timeZone": tz}
    body["end"] = {"dateTime": end_when.isoformat(), "timeZone": tz}
    return body


def google_create_event(calendar, user, *, title: str, when: datetime,
                        all_day: bool = True, description: str = "",
                        duration_minutes: int = 30,
                        timezone: str | None = None,
                        location: str = "", alarm_minutes: int = 0,
                        uid: str = "", organizer=None, attendees=()) -> dict:
    """Push a task to a Google calendar as a single all-day or timed event.

    A booking passes ``uid`` and ``attendees``: Google keeps the iCalUID the
    guest's invitation carries, so the guest's reply finds this event. The
    organizer is the calendar's owner by definition, and ``sendUpdates=none``
    keeps Google from mailing a second invitation of its own. Should Google
    refuse the iCalUID, the event is written without it rather than not at
    all."""
    token = _ensure_google_access_token(calendar, user)
    cal_id = calendar.calendar_id or "primary"
    body = _event_body(title, when, all_day, description, duration_minutes,
                       timezone, location, alarm_minutes)
    people = [{"email": p["email"], "displayName": p.get("name") or "",
               "responseStatus": "accepted" if p.get("partstat") == "ACCEPTED"
               else "needsAction"} for p in attendees if p.get("email")]
    if people:
        body["attendees"] = people
    url = (f"{GOOGLE_API_URL}/calendars/{urllib.parse.quote(cal_id)}/events"
           "?sendUpdates=none")
    if uid:
        try:
            return _google_insert(url, token, dict(body, iCalUID=uid))
        except CalendarError as exc:
            if " 400:" not in str(exc):
                raise
            log.info("Google refused iCalUID %s, inserting without: %s", uid, exc)
    return _google_insert(url, token, body)


def _google_insert(url: str, token: str, body: dict) -> dict:
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
                       timezone: str | None = None,
                       location: str = "", alarm_minutes: int = 0) -> dict:
    """Replace an existing event's title/time. PUT keeps the same id."""
    token = _ensure_google_access_token(calendar, user)
    cal_id = calendar.calendar_id or "primary"
    body = _event_body(title, when, all_day, description, duration_minutes,
                       timezone, location, alarm_minutes)
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
            busy=(it.get("transparency") != "transparent"
                  and it.get("status") != "cancelled"),
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
    events = [
        ev for ev in expand_vevents(parsed.walk("VEVENT"), calendar, since, until)
        if not (ev.start > until or ev.end < since)
    ]
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
    # TRANSPARENT = "shows me as free"; a cancelled occurrence blocks nothing
    # either. Both are the calendar's own word on it.
    transp = str(comp.get("TRANSP", "") or "").strip().upper()
    status = str(comp.get("STATUS", "") or "").strip().upper()
    return CalendarEvent(
        uid=str(comp.get("UID", "")),
        title=str(comp.get("SUMMARY", "(no title)")),
        start=start_dt,
        end=end_dt,
        all_day=all_day,
        busy=(transp != "TRANSPARENT" and status != "CANCELLED"),
        location=str(comp.get("LOCATION", "") or ""),
        url=str(comp.get("URL", "") or ""),
        calendar_color=calendar.color,
        calendar_name=calendar.name,
        calendar_pk=str(getattr(calendar, "pk_ref", "") or ""),
    )


# A series with no UNTIL and no COUNT is infinite; the window bounds the
# expansion, but a daily-since-2005 rule with a wide window still has to stop
# somewhere. Any real calendar day is far below this.
_MAX_OCCURRENCES = 500


def expand_vevents(comps, calendar, since: datetime,
                   until: datetime) -> list["CalendarEvent"]:
    """Every event the given ``VEVENT`` components contribute to the window.

    A calendar hands over a recurring appointment as ONE component carrying an
    ``RRULE`` — a CalDAV ``calendar-query`` and a subscribed ``.ics`` both do
    this, and neither expands it for us. Read literally, a weekly jour fixe is
    therefore a single event on the day the series began, which every window
    check then drops: the appointment is invisible for as long as it recurs.
    Expanding is what makes it visible on the days it actually falls on.

    A modified single occurrence travels as its own component with a
    ``RECURRENCE-ID``. Such a component is not a series, and it *replaces* the
    occurrence the rule would otherwise generate at that time — so those are
    collected first and the generated one is skipped.

    ``EXDATE`` (a cancelled occurrence) and ``RDATE`` (an extra one) are part
    of the same rule set and honoured alongside the ``RRULE``.
    """
    comps = list(comps)
    overrides: dict[tuple[str, str], object] = {}
    for comp in comps:
        rid = comp.get("RECURRENCE-ID")
        if rid is not None:
            uid = str(comp.get("UID", ""))
            overrides[(uid, _occurrence_key(_normalise_ical_dt(rid.dt)))] = comp

    out: list[CalendarEvent] = []
    for comp in comps:
        base = _vevent_to_event(comp, calendar)
        if base is None:
            continue
        if comp.get("RECURRENCE-ID") is not None:
            # The exception itself: a single event, keyed like the occurrence
            # it stands in for so the two can never both show.
            key = _occurrence_key(_normalise_ical_dt(comp["RECURRENCE-ID"].dt))
            out.append(replace(base, uid=f"{base.uid}#{key}"))
            continue
        starts = _occurrence_starts(comp, base, since, until)
        if starts is None:
            out.append(base)          # not a series
            continue
        duration = base.end - base.start
        for start in starts:
            key = _occurrence_key(start)
            if (base.uid, key) in overrides:
                continue              # a modified occurrence replaces this one
            out.append(replace(base, uid=f"{base.uid}#{key}",
                               start=start, end=start + duration))
    return out


def _occurrence_key(start: datetime) -> str:
    """The id suffix an occurrence is known by — also how a ``RECURRENCE-ID``
    is matched against a generated start, so both go through this."""
    return start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _occurrence_starts(comp, base: "CalendarEvent", since: datetime,
                       until: datetime) -> "list[datetime] | None":
    """The starts a recurring component has inside the window, or ``None``
    when it doesn't recur at all.

    The rule set is assembled from the PARSED properties rather than from the
    component's text: an ``EXDATE;TZID=Europe/Berlin`` re-serialised into a
    rule string loses its zone and comes back naive, which dateutil then
    refuses to compare against the aware occurrences it just generated.
    """
    rrule = comp.get("RRULE")
    rdate = comp.get("RDATE")
    if not rrule and not rdate:
        return None
    from dateutil.rrule import rrulestr, rruleset

    # Expand in the appointment's OWN zone, not in UTC: "every Thursday at
    # 14:00" is a wall-clock rule, and a series expanded in UTC silently moves
    # by an hour the moment the window crosses a daylight-saving change.
    dtstart = base.start.astimezone(_expansion_zone(base.start))
    rset = rruleset()
    try:
        for value in (rrule if isinstance(rrule, list) else [rrule] if rrule else []):
            rset.rrule(rrulestr(f"RRULE:{value.to_ical().decode()}", dtstart=dtstart))
    except (ValueError, TypeError):
        # An unparseable rule is not a reason to lose the appointment itself.
        return None
    for dt in _ical_datetimes(rdate):
        rset.rdate(dt)
    for dt in _ical_datetimes(comp.get("EXDATE")):
        rset.exdate(dt)

    # An occurrence that STARTED before the window can still run into it, so
    # reach back by its own length rather than stopping at the window's edge.
    duration = base.end - base.start
    starts = []
    for occ in rset.between(since - duration, until, inc=True):
        starts.append(occ)
        if len(starts) >= _MAX_OCCURRENCES:
            break
    return starts


def _expansion_zone(start: datetime):
    """A timezone that answers correctly for dates other than ``start``'s own.

    icalendar 5 hands back pytz zones, and a pytz zone carries the offset of
    the moment it was resolved at: build a datetime for another date with that
    same tzinfo and it keeps the old offset, so an autumn occurrence of a
    series that began in summer comes out an hour off — which is exactly what
    generating occurrences does. Re-resolving the zone BY NAME through
    ``zoneinfo`` gives one that computes its offset per date. Falls back to UTC
    for anything nameless (a fixed-offset zone has nothing to recompute).
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    tz = start.tzinfo
    name = getattr(tz, "zone", None) or getattr(tz, "key", None)
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _ical_datetimes(prop) -> "list[datetime]":
    """Flatten an ``RDATE`` / ``EXDATE`` property — one or many, each holding
    one or many values — into aware UTC datetimes."""
    if not prop:
        return []
    out = []
    for entry in (prop if isinstance(prop, list) else [prop]):
        for value in getattr(entry, "dts", []) or []:
            out.append(_normalise_ical_dt(value.dt).astimezone(timezone.utc))
    return out


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

_CALDAV_DISCOVER_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    '<d:prop><d:resourcetype/><d:displayname/>'
    '<d:current-user-principal/><c:calendar-home-set/>'
    '<c:schedule-default-calendar-URL/></d:prop>'
    '</d:propfind>'
)

_CALDAV_LIST_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
    'xmlns:a="http://apple.com/ns/ical/">'
    '<d:prop><d:resourcetype/><d:displayname/>'
    '<c:supported-calendar-component-set/><a:calendar-color/></d:prop>'
    '</d:propfind>'
)

APPLE_NS = "http://apple.com/ns/ical/"

# urllib won't follow a redirect for a method it doesn't know (rightly — that
# would replay PROPFIND against somewhere else), so the hops are walked here.
# Each one still goes through ``safe_urlopen``'s validation.
_REDIRECT_CODES = (301, 302, 307, 308)


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
    raising (used so a DELETE of an already-gone resource is a success); for an
    allowed 3xx ``text`` is the ``Location`` header, a redirect having no body
    worth reading."""
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
            if exc.code in _REDIRECT_CODES:
                return exc.code, (exc.headers.get("Location") or "")
            return exc.code, ""
        text = exc.read().decode("utf-8", "replace")[:300]
        raise CalendarError(f"CalDAV {method} {exc.code}: {text}") from exc
    except urllib.error.URLError as exc:
        raise CalendarError(f"network error: {exc}") from exc


def _with_slash(url: str) -> str:
    url = (url or "").strip()
    return url if url.endswith("/") else url + "/"


def _caldav_propfind(url: str, auth: str, body: str, depth: str) -> "tuple[str, str]":
    """PROPFIND, following redirects ourselves. Returns ``(url that answered,
    body)`` — which URL answered matters, because every ``href`` in a
    multistatus is relative to that one, not to the one we asked."""
    seen = set()
    for _hop in range(4):
        status, text = _caldav_request(
            "PROPFIND", url, auth=auth, body=body,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            allow_codes=_REDIRECT_CODES,
        )
        if status not in _REDIRECT_CODES:
            return url, text
        target = urllib.parse.urljoin(url, text.strip()) if text.strip() else ""
        seen.add(url)
        if not target or target in seen:
            raise CalendarError("CalDAV redirect went in a circle")
        url = target
    raise CalendarError("too many CalDAV redirects")


def _caldav_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _caldav_href(calendar, uid: str) -> str:
    base = calendar.caldav_url or ""
    if not base.endswith("/"):
        base += "/"
    return urllib.parse.urljoin(base, urllib.parse.quote(uid) + ".ics")


def _dav_responses(text: str, base: str) -> "list[tuple[str, list]]":
    """``[(absolute href, [property elements])]`` out of a multistatus body.
    Only properties their ``propstat`` reported as 200 are kept."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise CalendarError(f"bad CalDAV response: {exc}") from exc
    out = []
    for resp in root.iter("{DAV:}response"):
        href_el = resp.find("{DAV:}href")
        href = (href_el.text or "").strip() if href_el is not None else ""
        props: list = []
        for pstat in resp.findall("{DAV:}propstat"):
            status = pstat.findtext("{DAV:}status") or ""
            if status and " 200 " not in f" {status} ":
                continue
            prop = pstat.find("{DAV:}prop")
            if prop is not None:
                props.extend(list(prop))
        out.append((urllib.parse.urljoin(base, href), props))
    return out


def _prop(props, tag):
    for el in props:
        if el.tag == tag:
            return el
    return None


def _prop_text(props, tag: str) -> str:
    el = _prop(props, tag)
    return (el.text or "").strip() if el is not None else ""


def _prop_href(props, tag: str, base: str) -> str:
    el = _prop(props, tag)
    href = el.find("{DAV:}href") if el is not None else None
    if href is None or not (href.text or "").strip():
        return ""
    return urllib.parse.urljoin(base, href.text.strip())


def _is_calendar(props) -> bool:
    rt = _prop(props, "{DAV:}resourcetype")
    return rt is not None and rt.find(f"{{{CALDAV_NS}}}calendar") is not None


def _holds_events(props) -> bool:
    """A task list is a calendar collection too — it just holds VTODOs. Only a
    collection that says VEVENT, or says nothing at all, is one to read."""
    comps = _prop(props, f"{{{CALDAV_NS}}}supported-calendar-component-set")
    if comps is None:
        return True
    names = {c.get("name", "").upper() for c in comps}
    return not names or "VEVENT" in names


def _calendar_entry(url: str, props) -> dict:
    name = _prop_text(props, "{DAV:}displayname")
    if not name:
        name = urllib.parse.unquote(url.rstrip("/").rsplit("/", 1)[-1])
    color = _prop_text(props, f"{{{APPLE_NS}}}calendar-color")
    if len(color) == 9:  # Apple writes #RRGGBBAA; the alpha means nothing here
        color = color[:7]
    if not (len(color) == 7 and color.startswith("#")):
        color = ""
    return {"url": _with_slash(url), "name": name or "Calendar", "color": color}


def caldav_calendars_from_listing(text: str, base: str) -> list[dict]:
    """The event calendars in a ``Depth: 1`` listing of a calendar home.

    Split out from the request so it can be tested against a recorded body:
    the home itself, the scheduling inbox/outbox, subscriptions and task lists
    all arrive in the same multistatus, and none of them is a calendar to read."""
    found = []
    for href, props in _dav_responses(text, base):
        if _with_slash(href) == _with_slash(base):
            continue
        if not _is_calendar(props) or not _holds_events(props):
            continue
        found.append(_calendar_entry(href, props))
    found.sort(key=lambda c: c["name"].lower())
    return found


def _caldav_props(url: str, auth: str) -> "tuple[str, list, str]":
    """PROPFIND ``Depth: 0`` → ``(url that answered, its properties, its href)``."""
    at, text = _caldav_propfind(url, auth, _CALDAV_DISCOVER_BODY, "0")
    responses = _dav_responses(text, at)
    if not responses:
        return at, [], at
    return at, responses[0][1], responses[0][0] or at


def _caldav_home(at: str, props, auth: str) -> "tuple[str, str]":
    """``(calendar home, default calendar)`` for a resource — its own
    ``calendar-home-set``, or the one its principal names.

    The default calendar is the one the server drops an invitation into
    (RFC 6638). Most servers answer it on the principal; the ones that don't
    leave it empty, and then "default" is decided further up."""
    home = _prop_href(props, f"{{{CALDAV_NS}}}calendar-home-set", at)
    default = _prop_href(props, f"{{{CALDAV_NS}}}schedule-default-calendar-URL", at)
    if home:
        return home, default
    principal = _prop_href(props, "{DAV:}current-user-principal", at)
    if not principal:
        return "", default
    p_at, p_props, _href = _caldav_props(principal, auth)
    return (_prop_href(p_props, f"{{{CALDAV_NS}}}calendar-home-set", p_at),
            default or _prop_href(
                p_props, f"{{{CALDAV_NS}}}schedule-default-calendar-URL", p_at))


_CALDAV_ADDRESS_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    '<d:prop><d:current-user-principal/><c:calendar-user-address-set/></d:prop>'
    '</d:propfind>'
)


def _mailtos(props) -> list[str]:
    el = _prop(props, f"{{{CALDAV_NS}}}calendar-user-address-set")
    if el is None:
        return []
    out = []
    for href in el.findall("{DAV:}href"):
        value = (href.text or "").strip()
        if value.lower().startswith("mailto:") and value[7:]:
            out.append(value[7:])
    return out


def caldav_user_address(calendar, user, prefer: str = "") -> str:
    """The mail address the server knows this account by (RFC 6638
    ``calendar-user-address-set`` on the principal), ``""`` when it names none.

    A calendar client decides "I organised this" by comparing the ORGANIZER
    with exactly these addresses. A booking that names the Cairn login address
    instead (``mail@…`` against Nextcloud's ``bastian.kuhn@…``) is somebody
    else's meeting to Apple Calendar, which then cannot apply the guest's
    acceptance. ``prefer`` wins when the server lists it among several."""
    auth = _caldav_auth_header(calendar, user)
    at, text = _caldav_propfind(_with_slash(calendar.caldav_url or ""), auth,
                                _CALDAV_ADDRESS_BODY, "0")
    responses = _dav_responses(text, at)
    props = responses[0][1] if responses else []
    found = _mailtos(props)
    principal = _prop_href(props, "{DAV:}current-user-principal", at)
    if not found and principal:
        p_at, p_text = _caldav_propfind(principal, auth, _CALDAV_ADDRESS_BODY, "0")
        p_resp = _dav_responses(p_text, p_at)
        found = _mailtos(p_resp[0][1]) if p_resp else []
    for addr in found:
        if prefer and addr.lower() == prefer.lower():
            return addr
    return found[0] if found else ""


def _well_known_caldav(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, "/.well-known/caldav", "", ""))


def discover_caldav_calendars(url: str, username: str,
                              password: str) -> "tuple[str, list[dict]]":
    """Every readable calendar behind ``url``.

    Returns ``(root, [{url, name, color, default}, …])``. The ROOT is the
    address the calendars were listed from — the calendar home, or the
    collection itself when that is all ``url`` pointed at. It is worth keeping:
    asking it again later is a refresh, and it is the only part of the walk a
    collection URL can't be re-derived from.

    ``url`` may be one calendar's collection URL, and then that one calendar
    comes back. Anything above it — a principal, a calendar home, or the bare
    server address, which is what people actually have at hand — is walked
    down CalDAV's own discovery chain instead: ``current-user-principal`` →
    ``calendar-home-set`` → the collections under it (RFC 4791), starting at
    ``/.well-known/caldav`` when the address itself isn't a DAV resource
    (RFC 6764). That is how any other client finds them all from one address.

    Exactly one entry is ``default``: the server's scheduling default calendar
    where it names one, else the first. Connecting a dozen calendars shouldn't
    put a dozen into Today, and this is the one of them that means "mine".

    Getting an answer at all is the credential check too, so the caller needs
    no separate probe."""
    auth = _basic_auth(username, password)
    start = _with_slash(url)
    try:
        at, props, href = _caldav_props(start, auth)
    except CalendarError as exc:
        # A bare server address usually isn't a DAV resource at all. If the
        # well-known path doesn't help either, the first failure is the one
        # worth reporting — it names the URL that was actually typed.
        try:
            at, props, href = _caldav_props(_well_known_caldav(start), auth)
        except CalendarError:
            raise exc from None

    if _is_calendar(props):
        one = _calendar_entry(href, props)
        one["default"] = True
        return one["url"], [one]

    home, default = _caldav_home(at, props, auth)
    if not home and _well_known_caldav(start) != at:
        at, props, href = _caldav_props(_well_known_caldav(start), auth)
        if _is_calendar(props):
            one = _calendar_entry(href, props)
            one["default"] = True
            return one["url"], [one]
        home, default = _caldav_home(at, props, auth)
    if not home:
        raise CalendarError("no CalDAV calendars found at that address")

    at, text = _caldav_propfind(_with_slash(home), auth, _CALDAV_LIST_BODY, "1")
    found = caldav_calendars_from_listing(text, at)
    for entry in found:
        entry["default"] = _with_slash(entry["url"]) == _with_slash(default or "")
    if found and not any(e["default"] for e in found):
        found[0]["default"] = True
    return _with_slash(home), found


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
        # One resource holds a whole series: the master with its RRULE plus a
        # component per modified occurrence, which only mean anything together.
        events.extend(
            ev for ev in expand_vevents(parsed.walk("VEVENT"), calendar, since, until)
            if not (ev.start > until or ev.end < since)
        )
    calendar.last_synced_at = datetime.now(timezone.utc)
    calendar.last_error = ""
    events.sort(key=lambda e: e.start)
    return events


def _caldav_event_ics(uid: str, title: str, when: datetime, all_day: bool,
                      description: str, duration_minutes: int,
                      tz_name: str | None, location: str = "",
                      alarm_minutes: int = 0, organizer=None,
                      attendees=()) -> str:
    from icalendar import Calendar as ICalendar, Event as IEvent
    cal = ICalendar()
    cal.add("prodid", "-//Cairn//Calendar//EN")
    cal.add("version", "2.0")
    ev = IEvent()
    ev.add("uid", uid)
    ev.add("summary", title or "")
    if description:
        ev.add("description", description)
    if location:
        # LOCATION is the join button in Apple Calendar, Thunderbird and every
        # phone; URL is what the ones that have a link field read.
        ev.add("location", location)
        ev.add("url", location)
    if all_day:
        ev.add("dtstart", when.date())
        ev.add("dtend", (when + timedelta(days=1)).date())
    else:
        w = when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
        ev.add("dtstart", w)
        ev.add("dtend", w + timedelta(minutes=max(duration_minutes, 5)))
    ev.add("dtstamp", datetime.now(timezone.utc))
    ev.add("last-modified", datetime.now(timezone.utc))
    # The planned block's reminder, relative to DTSTART so it follows the event
    # if it is dragged elsewhere in the calendar. ``0`` adds nothing at all.
    from application.services.reminders import add_alarm
    add_alarm(ev, alarm_minutes, title or "Reminder")
    if organizer:
        # A booking: the host's own copy of the meeting the guest was invited
        # to. SCHEDULE-AGENT=CLIENT, because Cairn mails the invitation and a
        # scheduling server must not send a second one.
        from application.services.booking_ics import add_participants
        add_participants(ev, organizer[0], organizer[1], attendees,
                         schedule_agent="CLIENT")
    cal.add_component(ev)
    return cal.to_ical().decode("utf-8")


def caldav_create_event(calendar, user, *, title: str, when: datetime,
                        all_day: bool = True, description: str = "",
                        duration_minutes: int = 30,
                        timezone: str | None = None,
                        location: str = "", alarm_minutes: int = 0,
                        uid: str = "", organizer=None, attendees=()) -> dict:
    """Create an event by PUTting a fresh ``<uid>.ics`` resource. Returns
    ``{"id": uid}`` so the caller stores the UID like a Google event id.

    ``uid``/``organizer``/``attendees`` are the booking page's: its event must
    carry the UID of the invitation the guest got, or the guest's reply
    matches nothing in the host's calendar."""
    import uuid
    uid = uid or f"cairn-{uuid.uuid4().hex}"
    ics = _caldav_event_ics(uid, title, when, all_day, description,
                            duration_minutes, timezone, location, alarm_minutes,
                            organizer=organizer, attendees=attendees)
    _caldav_request("PUT", _caldav_href(calendar, uid), calendar=calendar, user=user,
                    body=ics, headers={"Content-Type": "text/calendar; charset=utf-8",
                                       "If-None-Match": "*"})
    return {"id": uid}


def caldav_patch_event(calendar, user, event_id: str, *, title: str, when: datetime,
                       all_day: bool = True, description: str = "",
                       duration_minutes: int = 30,
                       timezone: str | None = None,
                       location: str = "", alarm_minutes: int = 0) -> dict:
    """Update an event by re-PUTting its ``<uid>.ics`` (same UID = replace)."""
    ics = _caldav_event_ics(event_id, title, when, all_day, description,
                            duration_minutes, timezone, location, alarm_minutes)
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
