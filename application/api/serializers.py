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
        "exclusive_children": tag.exclusive_children,
        "maps_to_status": tag.maps_to_status or None,
    }


def project(p) -> dict[str, Any]:
    return {
        "id": str(p.pk),
        "name": p.name,
        "note": p.note,
        "color": p.color or None,
        "kind": p.kind,
        "type": p.project_type,
        "status": p.status,
        "is_folder": p.is_folder,
        "parent_id": str(p.parent.pk) if p.parent else None,
        "tags": list(p.tags or []),
        "due_date": iso(p.due_date),
        "plan_date": iso(p.plan_date),
        "review_interval_days": p.review_interval_days,
        "last_reviewed_at": iso(p.last_reviewed_at),
        "next_review_at": iso(p.next_review_at),
        "owner": user_brief(p.owner),
        "members": [user_brief(m) for m in (p.members or [])],
        "external_id": p.external_id or None,
        "external_ref": p.external_ref or None,
        "source": p.source or None,
        "share_enabled": bool(p.share_enabled),
        "share_token": p.share_token or None,
        "created_at": iso(p.created_at),
        "updated_at": iso(p.updated_at),
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
        "color": t.color or None,
        "status": t.status,
        "flagged": t.flagged,
        "priority": t.priority,
        "defer_date": iso(t.defer_date),
        "due_date": iso(t.due_date),
        "plan_date": iso(t.plan_date),
        "estimated_minutes": t.estimated_minutes,
        "repeat_every_days": t.repeat_every_days,
        "repeat_unit": t.repeat_unit,
        "repeat_from_completion": t.repeat_from_completion,
        "is_internal": bool(t.is_internal),
        "project_id": str(t.project.pk) if t.project else None,
        "tags": list(t.tags or []),
        # ``tag_names`` retained as an alias so existing clients keep
        # working — drop after the next major bump.
        "tag_names": list(t.tags or []),
        "external_tags": list(t.external_tags or []),
        "external_id": t.external_id or None,
        "external_ref": t.external_ref or None,
        "source": t.source or None,
        "owner": user_brief(t.owner),
        "assigned_to": user_brief(t.assigned_to),
        "assigned_by": user_brief(t.assigned_by),
        "assigned_at": iso(t.assigned_at),
        "comments": [comment(c) for c in (t.comments or [])],
        "created_at": iso(t.created_at),
        "updated_at": iso(t.updated_at),
        "completed_at": iso(t.completed_at),
    }
