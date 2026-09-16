"""Effective-timezone resolution.

The browser is the source of truth for "where the user is right now": a tiny
inline script (see ``layout/base.html``) writes the IANA zone from
``Intl.DateTimeFormat().resolvedOptions().timeZone`` into a ``tz`` cookie.
Server-side rendering, drag-to-schedule, and auto-plan all resolve the zone
through here so display and writes stay in one consistent wall-clock.

Priority: browser cookie → user's saved profile timezone → server
``DEFAULT_TZ`` → UTC. The cookie only exists inside a request, so background
jobs (Celery writeback, CLI) fall back to the profile/default automatically.
"""

from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo


def _valid(name: str) -> bool:
    if not name:
        return False
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def resolve_tzname(user=None) -> str:
    """Effective IANA timezone name for the current context."""
    try:
        from flask import request, current_app, has_request_context
    except Exception:
        return "UTC"

    if has_request_context():
        # The browser may percent-encode the "/" in zone names (Europe%2FBerlin);
        # unquote so ZoneInfo can parse it. (Stale encoded cookies live a year.)
        from urllib.parse import unquote
        ck = unquote((request.cookies.get("tz") or "").strip())
        if _valid(ck):
            return ck

    name = (getattr(user, "timezone", "") or "").strip()
    if _valid(name):
        return name

    try:
        default = current_app.config.get("DEFAULT_TZ") or "UTC"
    except Exception:
        default = "UTC"
    return default if _valid(default) else "UTC"


def resolve_tz(user=None):
    """Effective timezone as a tzinfo (never raises; UTC on failure)."""
    try:
        return ZoneInfo(resolve_tzname(user))
    except Exception:
        return timezone.utc


def local_date(value, tz=None):
    """Calendar day of a stored datetime in the user's wall-clock.

    An all-day plan/due date is stored as midnight in the user's zone — in
    Berlin that is 22:00 UTC the day *before*, so ``value.date()`` answers with
    the previous day and everything keyed off it (Today membership, the
    "today's plan" chip, "tomorrow") fires a day early. Every "which day is
    this?" comparison goes through here. Pass ``tz`` when the caller already
    resolved it — a loop over tasks shouldn't re-resolve per row.
    """
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz or resolve_tz()).date()
