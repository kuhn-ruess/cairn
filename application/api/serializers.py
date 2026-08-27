"""Plain-dict serializers — no extra dependency needed for JSON."""
import re
from typing import Any


def iso(dt):
    return dt.isoformat() if dt else None


# Inline image/file links in note + comment markdown point at the cookie-authed
# WEB route (``/tasks/<id>/note-image|note-file/<token>``). An API client can't
# load those, so rewrite them to the token-authed API download endpoint — the
# same endpoint serves both note_images and note_files. This makes images
# embedded in the note OR in comments fetchable purely over the API.
_INLINE_ATTACH_RE = re.compile(
    r"/tasks/[0-9a-fA-F]{24}/note-(?:image|file)/([A-Za-z0-9_\-]+)")


def rewrite_attachment_urls(markdown, task_pk):
    if not markdown:
        return markdown
    from flask import url_for
    return _INLINE_ATTACH_RE.sub(
        lambda m: url_for("api_v1.get_attachment",
                          task_id=str(task_pk), token=m.group(1)),
        markdown,
    )


def ref_ids(raw) -> list[str]:
    """Stringified ids of a ListField(ReferenceField) read straight from the
    document's raw ``_data`` — so serializing a list row never dereferences
    the refs (one $in per row → N+1; see CLAUDE.md: minimise DB load). Works
    for both undereferenced DBRefs (``.id``) and loaded docs (``.pk``)."""
    out = []
    for r in raw or []:
        rid = getattr(r, "id", None) or getattr(r, "pk", None)
        if rid:
            out.append(str(rid))
    return out


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
        "note": rewrite_attachment_urls(p.note, p.pk),
        "color": p.color or None,
        "kind": p.kind,
        "type": p.project_type,
        "status": p.status,
        "is_folder": p.is_folder,
        "parent_id": ref_ids([p._data.get("parent")])[0] if p._data.get("parent") else None,
        "related_ids": ref_ids(p._data.get("related")),
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
        # Inline attachments/comments — a project note can carry pasted images
        # too, fetchable via the same /tasks/<id>/attachments/<token> endpoint.
        "comments": [comment(c, p.pk) for c in (p.comments or [])],
        "attachments": [attachment(p.pk, f) for f in (p.note_files or [])],
        "note_images": [note_image(p.pk, i) for i in (p.note_images or [])],
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


def comment(c, task_pk=None) -> dict[str, Any]:
    # ``author`` is null for comments mirrored in from an external system
    # (e.g. a Zammad ticket reply). ``author_name`` is always a display
    # string and ``is_external`` flags the non-app-user case for clients.
    # ``body`` has its inline image/file links rewritten to API URLs so a
    # picture pasted into a comment is fetchable over the API.
    return {
        "author": user_brief(c.author),
        "author_name": c.author_name,
        "is_external": c.is_external,
        "body": rewrite_attachment_urls(c.body, task_pk) if task_pk else c.body,
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
        "note": rewrite_attachment_urls(t.note, t.pk),
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
        "assignee_parent_id": t.assignee_filing_id() or None,
        "related_ids": ref_ids(t._data.get("related")),
        "sort_order": t.sort_order,
        "estimated_minutes": t.estimated_minutes,
        "repeat_every_days": t.repeat_every_days,
        "repeat_unit": t.repeat_unit,
        "repeat_weekdays": list(t.repeat_weekdays or []),
        "repeat_monthday": t.repeat_monthday,
        "repeat_from_completion": bool(t.repeat_from_completion),
        "repeating": bool(t.repeat_every_days or t.repeat_weekdays or t.repeat_monthday),
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
        "kind": t.kind,
        "title": t.title,
        "note": rewrite_attachment_urls(t.note, t.pk),
        "color": t.color or None,
        "status": t.status,
        "flagged": t.flagged,
        "priority": t.priority,
        "sort_order": t.sort_order,
        "location": t.location or None,
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
        "repeat_weekdays": list(t.repeat_weekdays or []),
        "repeat_monthday": t.repeat_monthday,
        "repeat_from_completion": t.repeat_from_completion,
        "is_internal": bool(t.is_internal),
        # ``project_id`` and ``parent_id`` are the same field (project is an
        # alias for parent); both are returned — parent_id reads clearer when
        # the parent is another task (a sub-task).
        "project_id": ref_ids([t._data.get("parent")])[0] if t._data.get("parent") else None,
        "parent_id": ref_ids([t._data.get("parent")])[0] if t._data.get("parent") else None,
        "assignee_parent_id": t.assignee_filing_id() or None,
        "related_ids": ref_ids(t._data.get("related")),
        "checklist": [{"text": c.text, "done": bool(c.done)} for c in (t.checklist or [])],
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
        "comments": [comment(c, t.pk) for c in (t.comments or [])],
        "attachments": [attachment(t.pk, f) for f in (t.note_files or [])],
        "note_images": [note_image(t.pk, i) for i in (t.note_images or [])],
        "created_at": iso(t.created_at),
        "updated_at": iso(t.updated_at),
        "completed_at": iso(t.completed_at),
    }


# ---------------- time tracking ----------------
#
# The billing module is team-scoped, not owner-scoped like everything above,
# so these carry ``team_id`` rather than an owner. Money is in **cents** and
# durations in **minutes**, exactly as stored — no float ever touches a rate.


def tt_team(t) -> dict[str, Any]:
    return {"id": str(t.pk), "name": t.name}


def tt_customer(c) -> dict[str, Any]:
    return {
        "id": str(c.pk),
        "team_id": ref_ids([c._data.get("team")])[0] if c._data.get("team") else None,
        "name": c.name,
        "note": c.note or "",
        "archived": bool(c.archived),
        # Every key an importer recognises this customer by. ``external_id`` is
        # the one an importer stamped when it created the record; the rest were
        # adopted from records it turned out to already have.
        "external_id": c.external_id or None,
        "external_ids": list(c.external_ids or []),
        "legal_name": c.legal_name or None,
        "address": c.address or "",
        "vat_id": c.vat_id or None,
        "tax_number": c.tax_number or None,
        "invoice_email": c.invoice_email or None,
        "contact_name": c.contact_name or None,
        "phone": c.phone or None,
        "payment_terms_days": c.payment_terms_days,
        "customer_number": c.customer_number or None,
        "billing_note": c.billing_note or "",
        "created_at": iso(c.created_at),
    }


def tt_project(p) -> dict[str, Any]:
    """A billing project. ``customer_id`` is read raw so listing a team's
    projects never dereferences one customer per row (see ``ref_ids``)."""
    return {
        "id": str(p.pk),
        "team_id": ref_ids([p._data.get("team")])[0] if p._data.get("team") else None,
        "customer_id": (ref_ids([p._data.get("customer")])[0]
                        if p._data.get("customer") else None),
        "name": p.name,
        "note": p.note or "",
        "hourly_rate_cents": p.hourly_rate_cents,
        "budget_value": p.budget_value,
        "budget_type": p.budget_type,
        "archived": bool(p.archived),
        # Where this customer's incoming external time books. At most one
        # project per customer carries it.
        "intake": bool(p.intake),
        "time_mode": p.time_mode,
        "external_id": p.external_id or None,
        "external_ids": list(p.external_ids or []),
        "custom_fields": [{"key": f.key, "label": f.label,
                           "field_type": f.field_type,
                           "default": f.default or "",
                           "options": list(f.options or [])}
                          for f in (p.custom_fields or [])],
        "created_at": iso(p.created_at),
    }


def tt_entry(row: dict) -> dict[str, Any]:
    """One row of ``services.timetracking.build_report_rows``.

    That function is already the deref-free reader every report goes through,
    so the API takes its rows rather than walking the buckets a second way —
    one shape of "a logged entry" for the whole app. ``bucket_id`` plus
    ``entry_id`` is how an entry is addressed; the list index never is."""
    return {
        "entry_id": row["entry_id"],
        "bucket_id": row["bucket_id"],
        "customer_id": row["customer_id"] or None,
        "customer": row["customer"],
        "project_id": row["project_id"],
        "project": row["project"],
        "user_id": row["user_id"] or None,
        "user_name": row["user_name"] or "",
        "external_user": row["external_user"] or None,
        "spent_on": row["spent_on"].isoformat() if row["spent_on"] else None,
        "start_time": row["start_time"] or None,
        "end_time": row["end_time"] or None,
        "minutes": row["minutes"],
        "note": row["note"],
        "description": row["description"],
        "source_label": row["source_label"] or None,
        "service": row["service"] or None,
        "billable": row["billable"],
        "amount_cents": row["amount_cents"],
        "locked": row["locked"],
        "status": row["status"],
        "archived_project": row["archived"],
        "custom": row["custom"],
        "external_ref": row["external_ref"] or None,
        "external_url": row["external_url"] or None,
    }
