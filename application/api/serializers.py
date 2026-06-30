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


def _filter_clause(c) -> dict[str, Any]:
    if c.kind == "group":
        return {
            "kind": "group",
            "group_op": c.group_op or "and",
            "children": [_filter_clause(ch) for ch in (c.children or [])],
        }
    return {"kind": "leaf", "field": c.field, "op": c.op, "value": c.value}


def perspective(p) -> dict[str, Any]:
    """Serialize a CustomPerspective (filter tree + grouping/sort) so a
    client can evaluate it locally against its own task cache."""
    return {
        "pid": p.pid,
        "name": p.name,
        "icon": p.icon,
        "color": p.color,
        "in_sidebar": bool(p.in_sidebar),
        "root_op": p.root_op or "and",
        "clauses": [_filter_clause(c) for c in (p.clauses or [])],
        "grouping": p.grouping,
        "grouping_tag": p.grouping_tag or None,
        "sort": p.sort,
        "include_completed": bool(p.include_completed),
        "show_blocked": bool(p.show_blocked),
    }


def comment(c) -> dict[str, Any]:
    # ``author`` is null for comments mirrored in from an external system
    # (e.g. a Zammad ticket reply). ``author_name`` is always a display
    # string and ``is_external`` flags the non-app-user case for clients.
    return {
        "author": user_brief(c.author),
        "author_name": c.author_name,
        "is_external": c.is_external,
        "body": c.body,
        "created_at": iso(c.created_at),
    }


def task_compact(t) -> dict[str, Any]:
    """Slim task projection for list/sync endpoints.

    Drops the heavy / rarely-needed-in-a-list fields (comments, external
    bookkeeping, the ``tag_names`` alias, audit timestamps) so a mobile
    client can pull a large working set — and small incremental deltas —
    cheaply. ``kind`` is included so the caller can tell tasks apart from
    project/folder/section items the unfiltered query may return. Fetch
    ``GET /tasks/<id>`` for the full record (note images, comments, …)."""
    return {
        "id": str(t.pk),
        "kind": t.kind,
        "title": t.title,
        "note": t.note,
        "status": t.status,
        "flagged": t.flagged,
        "priority": t.priority,
        "defer_date": iso(t.defer_date),
        "due_date": iso(t.due_date),
        "plan_date": iso(t.plan_date),
        "followup_at": iso(t.followup_at),
        "followup_last_at": iso(t.followup_last_at),
        "followup_every_days": t.followup_every_days,
        "waiting_on": t.waiting_on or None,
        "project_id": str(t.project.pk) if t.project else None,
        "assignee_parent_id": str(t.assignee_parent.pk) if t.assignee_parent else None,
        "sort_order": t.sort_order,
        "estimated_minutes": t.estimated_minutes,
        "repeat_every_days": t.repeat_every_days,
        "repeat_unit": t.repeat_unit,
        "repeat_from_completion": bool(t.repeat_from_completion),
        "repeating": bool(t.repeat_every_days),
        "tags": list(t.tags or []),
        "assigned_to_id": str(t.assigned_to.pk) if t.assigned_to else None,
        "created_at": iso(t.created_at),
        "updated_at": iso(t.updated_at),
        "completed_at": iso(t.completed_at),
    }


def attachment(task_pk, f) -> dict[str, Any]:
    """A file dragged/pasted onto a task note (``note_files``). The binary is
    not inlined — clients fetch it from ``url`` (the API download endpoint)."""
    from flask import url_for
    return {
        "token": f.token,
        "filename": f.filename,
        "content_type": f.content_type or "application/octet-stream",
        "size": len(f.data) if f.data else 0,
        "created_at": iso(f.created_at),
        "url": url_for("api_v1.get_attachment", task_id=str(task_pk), token=f.token),
    }


def note_image(task_pk, i) -> dict[str, Any]:
    """A pasted image referenced inline from the note markdown
    (``note_images``). Same download endpoint as file attachments."""
    from flask import url_for
    return {
        "token": i.token,
        "content_type": i.content_type or "image/webp",
        "size": len(i.data) if i.data else 0,
        "created_at": iso(i.created_at),
        "url": url_for("api_v1.get_attachment", task_id=str(task_pk), token=i.token),
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
        "followup_at": iso(t.followup_at),
        "followup_last_at": iso(t.followup_last_at),
        "followup_every_days": t.followup_every_days,
        "waiting_on": t.waiting_on or None,
        "estimated_minutes": t.estimated_minutes,
        "repeat_every_days": t.repeat_every_days,
        "repeat_unit": t.repeat_unit,
        "repeat_from_completion": t.repeat_from_completion,
        "is_internal": bool(t.is_internal),
        "project_id": str(t.project.pk) if t.project else None,
        "assignee_parent_id": str(t.assignee_parent.pk) if t.assignee_parent else None,
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
        "attachments": [attachment(t.pk, f) for f in (t.note_files or [])],
        "note_images": [note_image(t.pk, i) for i in (t.note_images or [])],
        "created_at": iso(t.created_at),
        "updated_at": iso(t.updated_at),
        "completed_at": iso(t.completed_at),
    }
