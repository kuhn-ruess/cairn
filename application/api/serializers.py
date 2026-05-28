"""Plain-dict serializers — no extra dependency needed for JSON."""
from typing import Any


def iso(dt):
    return dt.isoformat() if dt else None


def user_brief(user) -> dict[str, Any] | None:
    if not user:
        return None
    return {
        "id": str(user.pk),
        "username": user.username,
        "display_name": user.display_name or user.username,
    }


def tag(tag) -> dict[str, Any]:
    # Embedded tags don't have a pk; the name is the user-scoped id.
    return {
        "id": tag.name,
        "name": tag.name,
        "color": tag.color,
        "is_context": tag.is_context,
        "on_hold": tag.on_hold,
        "parent": tag.parent or None,
    }


def project(p) -> dict[str, Any]:
    return {
        "id": str(p.pk),
        "name": p.name,
        "note": p.note,
        "type": p.project_type,
        "status": p.status,
        "is_folder": p.is_folder,
        "parent_id": str(p.parent.pk) if p.parent else None,
        "review_interval_days": p.review_interval_days,
        "last_reviewed_at": iso(p.last_reviewed_at),
        "next_review_at": iso(p.next_review_at),
        "owner": user_brief(p.owner),
        "members": [user_brief(m) for m in (p.members or [])],
        "created_at": iso(p.created_at),
        "completed_at": iso(p.completed_at),
    }


def comment(c) -> dict[str, Any]:
    return {
        "author": user_brief(c.author),
        "body": c.body,
        "created_at": iso(c.created_at),
    }


def task(t) -> dict[str, Any]:
    return {
        "id": str(t.pk),
        "title": t.title,
        "note": t.note,
        "status": t.status,
        "flagged": t.flagged,
        "priority": t.priority,
        "defer_date": iso(t.defer_date),
        "due_date": iso(t.due_date),
        "estimated_minutes": t.estimated_minutes,
        "repeat_every_days": t.repeat_every_days,
        "repeat_from_completion": t.repeat_from_completion,
        "project_id": str(t.project.pk) if t.project else None,
        "tag_names": list(t.tags or []),
        "owner": user_brief(t.owner),
        "assigned_to": user_brief(t.assigned_to),
        "assigned_by": user_brief(t.assigned_by),
        "assigned_at": iso(t.assigned_at),
        "comments": [comment(c) for c in (t.comments or [])],
        "created_at": iso(t.created_at),
        "updated_at": iso(t.updated_at),
        "completed_at": iso(t.completed_at),
    }
