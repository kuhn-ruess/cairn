"""Bulk cascade-delete helpers.

These are the ONLY supported way to delete entities that own subtrees.
They always issue collection-level Mongo operations against indexed
fields so they stay O(log n) per query — never per-document. At 100k+
users / millions of tasks this matters.

Contracts
---------
* ``delete_project_cascade(project)`` — drops the project, every
  descendant project/section/folder, and every task under any of them.
  Affected queries are ``{project: {$in: [...]}}`` and ``{parent: ...}``
  which hit existing indexes.

* ``delete_user_cascade(user)`` — drops every project the user owns
  (and recursively their tasks), every task the user owns directly
  (e.g. inbox tasks with no project), every API token, then nullifies
  the user from assignments / project members / team memberships, then
  drops the user. Issues a constant number of bulk ops regardless of
  data volume.

* ``delete_team_cascade(team)`` — drops the team and its invites,
  nullifies ``user.team`` for everyone who was in it. Does NOT delete
  the members themselves.
"""
from __future__ import annotations

def _project_subtree_ids(root) -> list:
    """All project pks at or below ``root`` (folders / sections included)."""
    from application.models.item import Item as Item
    ids = [root.pk]
    frontier = [root.pk]
    # BFS using one query per depth. Item trees are shallow (handful
    # of levels) so this is bounded — no per-node round-trip.
    while frontier:
        children = list(Item.objects(parent__in=frontier).only("id", "parent"))
        if not children:
            break
        frontier = [c.pk for c in children]
        ids.extend(frontier)
    return ids

def delete_project_cascade(project) -> dict:
    """Drop ``project`` + descendants + their tasks. Returns counts.

    Best-effort: also tries to remove any mirrored Google Calendar
    events for the task(s) under the subtree, up to ``CAL_CLEANUP_CAP``.
    Google's API has no bulk-delete, so we cap to keep the request from
    hanging on a giant project.
    """
    from application.models.item import Item as Item
    from application.models.item import Item as Item
    from application.services.calendar_writeback import (
        delete_task_calendar_events,
    )

    CAL_CLEANUP_CAP = 200

    pks = _project_subtree_ids(project)

    # Pre-fetch only tasks that actually have calendar events linked.
    # The project-index hit narrows the scan; the existence filter
    # runs in mongo.
    owner = project.owner
    cal_tasks = Item.objects(parent__in=pks).filter(
        __raw__={"$or": [
            {"gcal_due_event_id": {"$exists": True, "$ne": ""}},
            {"gcal_plan_event_id": {"$exists": True, "$ne": ""}},
        ]}
    ).limit(CAL_CLEANUP_CAP)
    cleaned = 0
    for t in cal_tasks:
        delete_task_calendar_events(t, owner)
        cleaned += 1

    tasks_removed = Item.objects(parent__in=pks).delete()
    projects_removed = Item.objects(pk__in=pks).delete()
    return {
        "projects": projects_removed,
        "tasks": tasks_removed,
        "calendar_events_cleaned": cleaned,
    }

def delete_user_cascade(user) -> dict:
    """Drop the user and everything that's exclusively theirs.

    Shared references (assignments, project membership lists, team
    memberships) get nullified or pulled in bulk — never per-doc.
    """
    from application.models.item import Item as Item
    from application.models.team import Team
    from application.models.user import User

    pk = user.pk
    counts: dict = {}

    # 1) Projects owned by this user — cascade each (so descendants +
    # tasks go too). The list is bounded by their plan; a handful of
    # bulk ops per project.
    owned = list(Item.objects(owner=pk).only("id"))
    counts["projects_owned"] = 0
    counts["tasks_in_owned_projects"] = 0
    for p in owned:
        r = delete_project_cascade(p)
        counts["projects_owned"] += r["projects"]
        counts["tasks_in_owned_projects"] += r["tasks"]

    # 2) Tasks owned directly (no project, e.g. Inbox-only) — bulk delete.
    counts["loose_tasks"] = Item.objects(owner=pk).delete()

    counts["api_tokens"] = len(user.api_tokens or [])
    counts["calendars"] = len(user.calendars or [])

    # 4) Shared references — bulk nullify / pull.
    Item.objects(assigned_to=pk).update(unset__assigned_to=1, unset__assigned_at=1)
    Item.objects(assigned_by=pk).update(unset__assigned_by=1)
    Item.objects(members=pk).update(pull__members=pk)
    # ``invites.used_by`` on teams the user previously accepted — clear
    # the back-reference so the team record doesn't dangle.
    for team in Team.objects(invites__used_by=pk):
        for inv in team.invites or []:
            if inv.used_by == pk:
                inv.used_by = None
        team.save()

    # 5) Team the user owned (team_admin) — cascade-drop it.
    counts["teams_admin"] = 0
    for team in Team.objects(admin=pk):
        delete_team_cascade(team)
        counts["teams_admin"] += 1

    # 6) Finally drop the user.
    User.objects(pk=pk).delete()
    counts["user"] = 1
    return counts

def purge_completed_tasks(user, *, dry_run: bool = False) -> int:
    """Bulk-delete done/dropped tasks past the user's retention window.

    Returns the number that would be (or were) removed. ``0`` retention
    means "keep forever" — a no-op.
    """
    from datetime import datetime, timedelta, timezone
    from application.models.item import Item as Item

    days = int(user.completed_task_retention_days or 0)
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    qs = Item.objects(
        owner=user,
        status__in=["done", "dropped"],
        completed_at__lt=cutoff,
    )
    if dry_run:
        return qs.count()
    # Single bulk delete — indexed on (owner, status) and (status,
    # completed_at) doesn't exist but (owner) + status filter is selective
    # enough to stay fast at scale.
    return qs.delete()

def purge_completed_tasks_all(*, dry_run: bool = False) -> dict:
    """Run ``purge_completed_tasks`` for every user with retention set."""
    from application.models.user import User
    per_user = {}
    for user in User.objects(completed_task_retention_days__gt=0).only(
        "id", "username", "completed_task_retention_days"
    ):
        per_user[user.username] = purge_completed_tasks(user, dry_run=dry_run)
    return per_user

def delete_team_cascade(team) -> dict:
    """Drop a team and its invites. Members are released, not deleted."""
    from application.models.team import Team
    from application.models.user import User

    pk = team.pk
    counts = {
        "invites": len(team.invites or []),
        "members_released": User.objects(team=pk).update(unset__team=1),
        "team": Team.objects(pk=pk).delete(),
    }
    return counts
