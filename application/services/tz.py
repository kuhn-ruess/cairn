"""Timezone helpers.

The forms use ``<input type="datetime-local">``, which produces a naive
wall-clock string in the *browser's* local time. WTForms parses it as a
naive ``datetime``. We need to attach the user's IANA tz before saving,
otherwise mongoengine (configured with ``tz_aware=True``) interprets the
naive value as UTC and the date drifts by the user's offset.
"""
from datetime import datetime, timezone

from flask import current_app


def user_zone(user):
    """Return a ``tzinfo`` for ``user`` (fallback chain: user → config → UTC)."""
    try:
        from zoneinfo import ZoneInfo
        name = (user and getattr(user, "timezone", None)) or \
            current_app.config.get("DEFAULT_TZ") or "UTC"
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def attach_user_tz(dt, user):
    """If ``dt`` is naive, attach the user's tz; aware values pass through."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=user_zone(user))


def to_user_naive(dt, user):
    """Convert ``dt`` (aware, usually UTC) to a naive datetime in user's tz.

    Used to pre-fill ``<input type="datetime-local">`` fields so the value
    the user sees matches what the field stores back.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(user_zone(user)).replace(tzinfo=None)


def localise_form_dates(form, user, fields=("defer_date", "due_date", "plan_date", "followup_at")):
    """Rewrite tz-aware fields on ``form`` to user-tz naive for rendering."""
    for name in fields:
        f = getattr(form, name, None)
        if f is None:
            continue
        f.data = to_user_naive(f.data, user)
