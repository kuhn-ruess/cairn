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
from datetime import datetime, timedelta, timezone
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
    from application.services.http_safety import (
        UnsafeURLError, validate_outbound_url,
    )
    try:
        validate_outbound_url(url)
    except UnsafeURLError as exc:
        # Don't leak the resolved IP back to the caller — keep the
        # message generic so it's safe to flash to the user.
        raise CalendarError(f"refusing to fetch: {exc}") from exc
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "bktodo/calendar (+https://localhost)",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
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
        start = comp.get("DTSTART")
        end = comp.get("DTEND") or start
        if not start:
            continue
        start_dt = _normalise_ical_dt(start.dt)
        end_dt = _normalise_ical_dt(end.dt) if end else start_dt
        all_day = not isinstance(start.dt, datetime)
        if start_dt > until or end_dt < since:
            continue
        events.append(CalendarEvent(
            uid=str(comp.get("UID", "")),
            title=str(comp.get("SUMMARY", "(no title)")),
            start=start_dt,
            end=end_dt,
            all_day=all_day,
            location=str(comp.get("LOCATION", "") or ""),
            url=str(comp.get("URL", "") or ""),
            calendar_color=calendar.color,
            calendar_name=calendar.name,
        ))
    calendar.last_synced_at = datetime.now(timezone.utc)
    calendar.last_error = ""
    # Embedded — caller (fetch_events) persists via the parent user.
    events.sort(key=lambda e: e.start)
    return events


def _normalise_ical_dt(value) -> datetime:
    """Convert a date-or-datetime from icalendar into a tz-aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    # ``date`` (all-day) → midnight UTC
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


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


# ----- Multi-calendar helper for the Today view ----------------------------


def collect_today_events(user) -> list[CalendarEvent]:
    """Fetch events for the user's "today".

    Window is widened from a strict UTC day so events scheduled in
    Europe/America local time around midnight aren't dropped — anything
    starting from 6h before UTC midnight through the end of UTC's "today"
    is in scope.
    """
    now = datetime.now(timezone.utc)
    since = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=6)
    until = since + timedelta(hours=36)

    out: list[CalendarEvent] = []
    for cal in (user.calendars or []):
        if not cal.show_in_today:
            continue
        try:
            out.extend(fetch_events(cal, since, until, user=user))
        except CalendarError as exc:
            log.warning("calendar %s failed: %s", cal.name, exc)
    out.sort(key=lambda e: e.start)
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
