"""Single source of truth for what "Today" contains.

The Today perspective and the home dashboard's Today count must agree.
They used to compute membership independently and drifted (the dashboard
counted "due within 2 days", Today counted "overdue/due today + flagged +
planned-today + just-unblocked"). Both now go through here.
"""
from datetime import datetime, timezone

from mongoengine import Q

from application.models.item import Item


def _user_tz(user):
    try:
        from zoneinfo import ZoneInfo
        from flask import current_app
        return ZoneInfo(
            user.timezone or current_app.config.get("DEFAULT_TZ") or "UTC"
        )
    except Exception:
        return timezone.utc


def _scope(user):
    return Q(owner=user) | Q(assigned_to=user)


def today_now(user):
    """Return ``(tasks, projects)`` that make up the user's *current* Today.

    Mirrors ``tasks.today`` in its ``is_today_now`` branch:
      * tasks: due/overdue through 23:59 today, OR flagged, OR planned for
        today (or earlier), OR a deferred 'next' task whose defer point has
        passed;
      * projects/sections carrying a due/plan date that lands today.
    Honours the user's persisted ``today_filter_tag``.
    """
    now = datetime.now(timezone.utc)
    today_date = now.astimezone(_user_tz(user)).date()
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)

    active_tag = user.today_filter_tag or ""
    has_tag = bool(active_tag and user.tag_by_name(active_tag))

    task_qs = Item.objects(_scope(user), kind="task", status__nin=["done", "dropped", "someday"])
    if has_tag:
        task_qs = task_qs.filter(tags=active_tag)
    tasks = [
        t for t in task_qs
        if (t.due_date and t.due_date <= today_end)
        or t.flagged
        or (t.plan_date and t.plan_date.date() <= today_date)
        or (t.defer_date and t.defer_date <= now and t.status == "next")
    ]

    # Project-like items (project/section, never plain tasks or folders)
    # carrying a relevant date — milestones that land in Today.
    proj_qs = Item.objects(
        owner=user, kind__in=["project", "section"],
        status__nin=["completed", "dropped"],
    )
    if has_tag:
        proj_qs = proj_qs.filter(tags=active_tag)
    task_pks = {t.pk for t in tasks}
    projects = [
        p for p in proj_qs
        if p.pk not in task_pks
        and ((p.due_date and p.due_date <= today_end)
             or (p.plan_date and p.plan_date.date() <= today_date))
    ]
    return tasks, projects


def today_count(user) -> int:
    """Count shown on the home dashboard. Matches the Today page header,
    which counts the task plate only (projects-with-dates render in their
    own block there, outside the count)."""
    tasks, _projects = today_now(user)
    return len(tasks)
