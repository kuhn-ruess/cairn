"""Push task dates back to the user's chosen Google calendar.

Triggered after a task is saved via the form / quick-add / API. For the
two date fields the user has opted into (``calendar_write_due`` and
``calendar_write_plan``) we mirror task → calendar event with these
rules:

* date present + no event id     → create event, store id on task
* date present + event id stored → patch event title/time
* date cleared + event id stored → delete event, clear id
* date absent + no event id      → noop

Failures (network, OAuth, missing target calendar) are logged and
returned as warnings; they never propagate up to break the task save.
"""
from __future__ import annotations

import logging

from application.services.calendar_sync import (
    CalendarError,
    google_create_event,
    google_delete_event,
    google_patch_event,
)

log = logging.getLogger(__name__)


def _write_calendar(user):
    """Resolve the user's configured write target. ``None`` if unset."""
    target_id = user.calendar_write_target
    if not target_id:
        return None
    cal = user.find_calendar(target_id)
    if cal and cal.provider == "google":
        return cal
    return None


def sync_task_to_calendar(task, user) -> list[str]:
    """Reconcile a task's plan/due dates with the user's calendar.

    Returns a list of human-readable warnings (empty on full success).
    """
    warnings: list[str] = []
    if not (user.calendar_write_due or user.calendar_write_plan):
        return warnings
    cal = _write_calendar(user)
    if not cal:
        # Toggles are on but no calendar picked — warn once, the user can
        # silence it by either turning the toggles off or picking a target.
        return ["Calendar write-back is enabled but no target calendar is selected."]

    duration = max(int(task.estimated_minutes or 30), 5)
    tz = user.timezone or None  # None → calendar_sync falls back to config default

    def _reconcile(kind: str, field_id_attr: str, when, enabled: bool,
                   title_prefix: str):
        if not enabled:
            return
        event_id = getattr(task, field_id_attr) or ""
        title = f"{title_prefix} {task.title}".strip()
        try:
            if when and event_id:
                google_patch_event(cal, user, event_id,
                                    title=title, when=when, all_day=False,
                                    description=_describe(task),
                                    duration_minutes=duration,
                                    timezone=tz)
            elif when and not event_id:
                created = google_create_event(cal, user,
                                               title=title, when=when, all_day=False,
                                               description=_describe(task),
                                               duration_minutes=duration,
                                               timezone=tz)
                setattr(task, field_id_attr, created.get("id") or "")
            elif not when and event_id:
                google_delete_event(cal, user, event_id)
                setattr(task, field_id_attr, "")
            # absent + no id → nothing to do
        except CalendarError as exc:
            log.warning("calendar write-back failed for task %s (%s): %s",
                        task.pk, kind, exc)
            warnings.append(f"{kind.capitalize()} date sync failed: {exc}")

    _reconcile("due", "gcal_due_event_id", task.due_date,
               user.calendar_write_due, title_prefix="Due:")
    _reconcile("plan", "gcal_plan_event_id", task.plan_date,
               user.calendar_write_plan, title_prefix="Plan:")

    # Persist any event-id changes the reconciliation made on the task.
    if warnings or task.gcal_due_event_id is not None or task.gcal_plan_event_id is not None:
        # ``task.save()`` is cheap and idempotent; we always call it so
        # newly-created event ids land in mongo.
        task.save()
    return warnings


def delete_task_calendar_events(task, user) -> None:
    """Best-effort removal of the Google events mirrored from this task.

    Called from task delete paths. Doesn't raise — Google errors are
    logged. Always reads the current calendar target; if the user has
    since unlinked it the call is a no-op.
    """
    if not (task.gcal_due_event_id or task.gcal_plan_event_id):
        return
    cal = _write_calendar(user)
    if not cal:
        return
    for attr in ("gcal_due_event_id", "gcal_plan_event_id"):
        event_id = getattr(task, attr, None)
        if not event_id:
            continue
        try:
            google_delete_event(cal, user, event_id)
        except CalendarError as exc:
            log.warning("calendar cleanup failed for task %s (%s): %s",
                        task.pk, attr, exc)


def _describe(task) -> str:
    """Description body for the Google event — keeps the task reachable."""
    parts = []
    if task.project:
        parts.append(f"Project: {task.project.name}")
    if task.tags:
        parts.append("Tags: " + ", ".join(task.tags))
    if task.note:
        parts.append("")
        parts.append(task.note)
    return "\n".join(parts)
