"""Single source of truth for what "Today" contains.

The Today perspective and the home dashboard's Today count must agree.
They used to compute membership independently and drifted (the dashboard
counted "due within 2 days", Today counted "overdue/due today + flagged +
planned-today + just-unblocked"). Both now go through here.
"""
from datetime import datetime, time, timedelta, timezone

from mongoengine import Q

from application.models.item import Item


def _user_tz(user):
    from application.services.tzutil import resolve_tz
    return resolve_tz(user)


def _scope(user):
    # members keeps re-delegated tasks visible to whoever passed them on.
    return Q(owner=user) | Q(assigned_to=user) | Q(members=user)


def _today_task_query(user, now):
    """Queryset of the user's *current* Today tasks, filtered in Mongo (no
    full-collection Python scan). Membership: due/overdue through end of
    today, OR flagged, OR planned today-or-earlier, OR a 'next' task whose
    defer point has passed. Honours ``today_filter_tag``."""
    tz = _user_tz(user)
    today_date = now.astimezone(tz).date()
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    next_day = datetime.combine(today_date + timedelta(days=1), time(0, 0)).replace(tzinfo=tz)
    qs = Item.objects(_scope(user), kind="task", status__nin=["done", "dropped", "someday"])
    active_tag = user.today_filter_tag or ""
    if active_tag and user.tag_by_name(active_tag):
        qs = qs.filter(tags=active_tag)
    cond = (Q(due_date__ne=None, due_date__lte=today_end)
            | Q(flagged=True)
            | Q(plan_date__ne=None, plan_date__lt=next_day)
            | Q(defer_date__ne=None, defer_date__lte=now, status="next")
            | Q(followup_at__ne=None, followup_at__lte=today_end))
    return qs.filter(cond), today_end, next_day


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
    task_q, today_end, next_day = _today_task_query(user, now)
    tasks = list(task_q)

    # Project-like items (project/section, never plain tasks or folders)
    # carrying a relevant date — milestones that land in Today.
    proj_qs = Item.objects(
        owner=user, kind__in=["project", "section"],
        status__nin=["completed", "dropped"],
    )
    active_tag = user.today_filter_tag or ""
    if active_tag and user.tag_by_name(active_tag):
        proj_qs = proj_qs.filter(tags=active_tag)
    proj_cond = (Q(due_date__ne=None, due_date__lte=today_end)
                 | Q(plan_date__ne=None, plan_date__lt=next_day))
    task_pks = {t.pk for t in tasks}
    projects = [p for p in proj_qs.filter(proj_cond) if p.pk not in task_pks]
    return tasks, projects


def today_count(user) -> int:
    """Count shown on the home dashboard. Matches the Today page header,
    which counts the task plate only (projects-with-dates render in their
    own block there, outside the count)."""
    task_q, _end, _next = _today_task_query(user, datetime.now(timezone.utc))
    return task_q.count()
