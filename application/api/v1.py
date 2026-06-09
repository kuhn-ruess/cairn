"""REST API v1 — token-authenticated JSON endpoints."""
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request
from mongoengine import Q

from application.api.auth import token_required
from application.api.serializers import project as ser_project
from application.api.serializers import tag as ser_tag
from application.api.serializers import task as ser_task
from application.api.serializers import task_compact as ser_task_compact
from application.api.serializers import perspective as ser_perspective

from application.models.user import User
from application.models.item import (
    Item, ITEM_KINDS, PROJECT_STATUS, PROJECT_TYPES, TASK_STATUS,
)

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

def _scope():
    user = g.current_user
    return Q(owner=user) | Q(assigned_to=user)

def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).rstrip("Z")
    return datetime.fromisoformat(s).astimezone(timezone.utc)

def _bad_request(msg: str, status: int = 400):
    return jsonify({"error": msg}), status

# ---------------- tasks ----------------

@api_v1_bp.get("/tasks")
@token_required
def list_tasks():
    user = g.current_user
    qs = Item.objects(_scope())
    status = request.args.get("status")
    if status:
        if status not in TASK_STATUS:
            return _bad_request(f"invalid status: {status}")
        qs = qs.filter(status=status)
    kind = request.args.get("kind")
    if kind:
        # The query is not kind-filtered by default, so it returns
        # project/folder/section items too. Clients that only want
        # actionable tasks pass ``kind=task``.
        if kind not in ITEM_KINDS:
            return _bad_request(f"invalid kind: {kind}")
        qs = qs.filter(kind=kind)
    project_id = request.args.get("project_id")
    if project_id:
        qs = qs.filter(parent=project_id)
    tag_id = request.args.get("tag_id")
    if tag_id:
        qs = qs.filter(tags=tag_id)
    if request.args.get("flagged") == "true":
        qs = qs.filter(flagged=True)
    if request.args.get("assigned_to_me") == "true":
        qs = qs.filter(assigned_to=user)
    updated_since = request.args.get("updated_since")
    if updated_since:
        # Incremental sync: only rows touched at/after this instant.
        # Pair with ``-updated_at`` ordering so the client can advance
        # its cursor from items[0].updated_at.
        since = _parse_dt(updated_since)
        if not since:
            return _bad_request("invalid updated_since")
        qs = qs.filter(updated_at__gte=since)

    # ``compact=true`` returns the slim projection (see serializers) — the
    # default for sync-heavy mobile clients; full records via /tasks/<id>.
    serialize = ser_task_compact if request.args.get("compact") == "true" else ser_task

    limit = max(1, min(int(request.args.get("limit", 100) or 100), 500))
    offset = max(0, min(int(request.args.get("offset", 0) or 0), 1_000_000))
    qs = qs.order_by("-updated_at").skip(offset).limit(limit)

    return jsonify({"items": [serialize(t) for t in qs], "limit": limit, "offset": offset})

@api_v1_bp.post("/tasks")
@token_required
def create_task():
    user = g.current_user
    data = request.get_json(silent=True) or {}
    if not data.get("title"):
        return _bad_request("title is required")

    task = Item(owner=user, title=data["title"], note=data.get("note", ""))
    _apply_task_payload(task, data, user)
    task.save()
    return jsonify(ser_task(task)), 201

@api_v1_bp.get("/tasks/<task_id>")
@token_required
def get_task(task_id):
    user = g.current_user
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        return _bad_request("not found", 404)
    return jsonify(ser_task(task))

@api_v1_bp.patch("/tasks/<task_id>")
@token_required
def update_task(task_id):
    user = g.current_user
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        return _bad_request("not found", 404)
    data = request.get_json(silent=True) or {}
    _apply_task_payload(task, data, user)
    task.save()
    return jsonify(ser_task(task))

@api_v1_bp.delete("/tasks/<task_id>")
@token_required
def delete_task(task_id):
    user = g.current_user
    task = Item.objects(pk=task_id).first()
    if not task or task.owner != user:
        return _bad_request("not found", 404)
    task.delete()
    return "", 204

@api_v1_bp.post("/tasks/<task_id>/complete")
@token_required
def complete_task(task_id):
    user = g.current_user
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        return _bad_request("not found", 404)
    next_task = task.complete(by_user=user)
    return jsonify(
        {
            "completed": ser_task(task),
            "next_occurrence": ser_task(next_task) if next_task else None,
        }
    )

@api_v1_bp.post("/tasks/<task_id>/assign")
@token_required
def assign_task(task_id):
    from application.services.permissions import resolve_assignable_user
    user = g.current_user
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        return _bad_request("not found", 404)
    data = request.get_json(silent=True) or {}
    assignee_id = data.get("user_id")
    if assignee_id:
        assignee = resolve_assignable_user(user, assignee_id)
        if not assignee:
            return _bad_request("user not assignable", 403)
        task.assigned_to = assignee
        task.assigned_by = user
        task.assigned_at = datetime.now(timezone.utc)
    else:
        task.assigned_to = None
        task.assigned_by = None
        task.assigned_at = None
    task.save()
    return jsonify(ser_task(task))

@api_v1_bp.post("/tasks/bulk")
@token_required
def bulk_tasks():
    """Bulk task operations.

    JSON body:
        {"task_ids": [...], "action": "delete" | "complete" | "reopen"
                                       | "set_status" | "set_project",
         "status": "...",       # required for set_status
         "project_id": "..."}   # required for set_project (or null to clear)
    """
    user = g.current_user
    data = request.get_json(silent=True) or {}
    ids = data.get("task_ids") or []
    action = (data.get("action") or "").strip()
    if not ids:
        return _bad_request("task_ids is required")

    tasks = list(Item.objects(pk__in=ids))
    affected, skipped = [], 0

    if action == "delete":
        for t in tasks:
            if t.owner == user:
                t.delete()
                affected.append(str(t.pk))
            else:
                skipped += 1
    elif action == "complete":
        for t in tasks:
            if t.can_be_edited_by(user):
                t.complete(by_user=user)
                affected.append(str(t.pk))
            else:
                skipped += 1
    elif action == "reopen":
        for t in tasks:
            if t.can_be_edited_by(user):
                t.status = "next"
                t.completed_at = None
                t.save()
                affected.append(str(t.pk))
            else:
                skipped += 1
    elif action == "set_status":
        target = data.get("status")
        if target not in TASK_STATUS:
            return _bad_request(f"invalid status: {target}")
        for t in tasks:
            if t.can_be_edited_by(user):
                t.status = target
                if target != "done":
                    t.completed_at = None
                t.save()
                affected.append(str(t.pk))
            else:
                skipped += 1
    elif action == "set_project":
        from application.services.permissions import resolve_visible_item
        project_id = data.get("project_id")
        project = resolve_visible_item(user, project_id) if project_id else None
        # When the caller passed a project_id we *must* resolve it; a
        # non-visible pk is a 403, not "drop to no project".
        if project_id and not project:
            return _bad_request("destination project not accessible", 403)
        for t in tasks:
            if t.can_be_edited_by(user):
                t.project = project
                t.save()
                affected.append(str(t.pk))
            else:
                skipped += 1
    else:
        return _bad_request(f"unknown action: {action}")

    return jsonify({"action": action, "affected": affected, "skipped": skipped})

@api_v1_bp.post("/projects/bulk")
@token_required
def bulk_projects():
    """Bulk project operations: delete | complete | archive."""
    user = g.current_user
    data = request.get_json(silent=True) or {}
    ids = data.get("project_ids") or []
    action = (data.get("action") or "").strip()
    if not ids:
        return _bad_request("project_ids is required")

    projects = list(Item.objects(pk__in=ids, owner=user))
    skipped = len(ids) - len(projects)
    affected = []

    if action == "delete":
        from application.services.cleanup import delete_project_cascade
        for p in projects:
            delete_project_cascade(p)
            affected.append(str(p.pk))
    elif action in ("complete", "archive"):
        new_status = "completed" if action == "complete" else "dropped"
        for p in projects:
            p.status = new_status
            p.save()
            affected.append(str(p.pk))
    else:
        return _bad_request(f"unknown action: {action}")

    return jsonify({"action": action, "affected": affected, "skipped": skipped})

@api_v1_bp.post("/tasks/<task_id>/comments")
@token_required
def add_comment(task_id):
    user = g.current_user
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        return _bad_request("not found", 404)
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return _bad_request("body is required")
    task.add_comment(user, body)
    return jsonify(ser_task(task)), 201

def _apply_task_payload(task: Item, data: dict, user) -> None:
    # Block mass-assignment of fields that must only be set by the
    # server (owner, share tokens, gcal event ids, creation time, …).
    # Everything else is opt-in per the explicit ``if "..." in data``
    # branches below.
    if "title" in data:
        task.title = data["title"]
    if "note" in data:
        task.note = data["note"] or ""
    if "status" in data:
        if data["status"] not in TASK_STATUS:
            raise ValueError(f"invalid status: {data['status']}")
        task.status = data["status"]
    if "flagged" in data:
        task.flagged = bool(data["flagged"])
    if "priority" in data:
        task.priority = data["priority"] or None
    if "defer_date" in data:
        task.defer_date = _parse_dt(data["defer_date"])
    if "due_date" in data:
        task.due_date = _parse_dt(data["due_date"])
    if "estimated_minutes" in data:
        em = data["estimated_minutes"]
        # Mirror the web /duration clamp so the API can't store
        # nonsensical (or huge) values that break the calendar view.
        if em is None or em == "":
            task.estimated_minutes = None
        else:
            try:
                task.estimated_minutes = max(5, min(1440, int(em)))
            except (TypeError, ValueError):
                raise ValueError("estimated_minutes must be an integer")
    if "plan_date" in data:
        task.plan_date = _parse_dt(data["plan_date"])
    if "repeat_every_days" in data:
        task.repeat_every_days = data["repeat_every_days"]
    if "repeat_unit" in data:
        unit = data["repeat_unit"]
        if unit and unit not in ("day", "weekday", "weekend", "week", "month", "year"):
            raise ValueError(f"invalid repeat_unit: {unit}")
        task.repeat_unit = unit or "day"
    if "repeat_from_completion" in data:
        task.repeat_from_completion = bool(data["repeat_from_completion"])
    if "is_internal" in data:
        task.is_internal = bool(data["is_internal"])
    if "color" in data:
        task.color = (data["color"] or "").strip()
    if "project_id" in data:
        from application.services.permissions import resolve_visible_item
        pid = data["project_id"]
        if pid:
            proj = resolve_visible_item(user, pid)
            if not proj:
                raise ValueError("project_id not accessible")
            task.project = proj
        else:
            task.project = None
    if "tags" in data:
        task.tags = [str(n) for n in (data["tags"] or []) if n]
    elif "tag_names" in data:
        task.tags = [n for n in (data["tag_names"] or []) if n]
    elif "tag_ids" in data:
        # Legacy field name kept for backwards-compat — treat the values
        # as names (clients passing IDs will get unresolved entries).
        task.tags = [str(n) for n in (data["tag_ids"] or [])]
    # Apply tag-driven status rules after any tag mutation.
    if "tags" in data or "tag_names" in data or "tag_ids" in data:
        from application.services.label_rules import apply_label_rules
        apply_label_rules(task, user)
    if "assigned_to_id" in data:
        from application.services.permissions import resolve_assignable_user
        if data["assigned_to_id"]:
            assignee = resolve_assignable_user(user, data["assigned_to_id"])
            if not assignee:
                raise ValueError("assigned_to user not assignable")
            task.assigned_to = assignee
            task.assigned_by = user
            task.assigned_at = datetime.now(timezone.utc)
        else:
            task.assigned_to = None
            task.assigned_by = None
            task.assigned_at = None

# ---------------- projects ----------------

@api_v1_bp.get("/projects")
@token_required
def list_projects():
    user = g.current_user
    qs = Item.objects(Q(owner=user) | Q(members=user)).order_by("+name")
    return jsonify({"items": [ser_project(p) for p in qs]})

@api_v1_bp.post("/projects")
@token_required
def create_project():
    user = g.current_user
    data = request.get_json(silent=True) or {}
    if not data.get("name"):
        return _bad_request("name is required")
    # ``name`` is a property (alias for ``title``), not a constructor field —
    # passing it as a kwarg raises FieldDoesNotExist (500). Build empty, then
    # let _apply_project_payload set the name via the property.
    p = Item(owner=user)
    _apply_project_payload(p, data)
    p.save()
    return jsonify(ser_project(p)), 201

@api_v1_bp.get("/projects/<project_id>")
@token_required
def get_project(project_id):
    user = g.current_user
    p = Item.objects(pk=project_id).first()
    if not p or not p.can_be_viewed_by(user):
        return _bad_request("not found", 404)
    return jsonify(ser_project(p))

@api_v1_bp.patch("/projects/<project_id>")
@token_required
def update_project(project_id):
    user = g.current_user
    p = Item.objects(pk=project_id).first()
    if not p or p.owner != user:
        return _bad_request("not found", 404)
    data = request.get_json(silent=True) or {}
    _apply_project_payload(p, data)
    p.save()
    return jsonify(ser_project(p))

@api_v1_bp.delete("/projects/<project_id>")
@token_required
def delete_project(project_id):
    from application.services.cleanup import delete_project_cascade
    user = g.current_user
    p = Item.objects(pk=project_id).first()
    if not p or p.owner != user:
        return _bad_request("not found", 404)
    delete_project_cascade(p)
    return "", 204

@api_v1_bp.post("/projects/<project_id>/review")
@token_required
def review_project(project_id):
    user = g.current_user
    p = Item.objects(pk=project_id).first()
    if not p or p.owner != user:
        return _bad_request("not found", 404)
    p.mark_reviewed()
    return jsonify(ser_project(p))

def _apply_project_payload(p: Item, data: dict) -> None:
    if "name" in data:
        p.name = data["name"]
    if "note" in data:
        p.note = data["note"] or ""
    if "color" in data:
        p.color = (data["color"] or "").strip()
    if "kind" in data:
        if data["kind"] not in ("project", "folder", "section"):
            raise ValueError(f"invalid kind: {data['kind']}")
        p.kind = data["kind"]
    if "type" in data or "project_type" in data:
        val = data.get("project_type") or data.get("type")
        if val not in PROJECT_TYPES:
            raise ValueError(f"invalid type: {val}")
        p.project_type = val
    if "status" in data:
        if data["status"] not in PROJECT_STATUS:
            raise ValueError(f"invalid status: {data['status']}")
        p.status = data["status"]
    if "parent_id" in data:
        from application.services.permissions import resolve_visible_item
        if data["parent_id"]:
            parent = resolve_visible_item(g.current_user, data["parent_id"])
            if not parent:
                raise ValueError("parent_id not accessible")
            p.parent = parent
        else:
            p.parent = None
    if "review_interval_days" in data:
        p.review_interval_days = data["review_interval_days"] or 0
    if "due_date" in data:
        p.due_date = _parse_dt(data["due_date"])
    if "plan_date" in data:
        p.plan_date = _parse_dt(data["plan_date"])
    if "tags" in data:
        p.tags = [str(n) for n in (data["tags"] or []) if n]
    if "member_ids" in data:
        from application.services.permissions import resolve_assignable_user
        ids = data["member_ids"] or []
        members = []
        for mid in ids:
            m = resolve_assignable_user(g.current_user, mid)
            if not m:
                raise ValueError(f"member {mid} not shareable")
            members.append(m)
        p.members = members

# ---------------- tags (embedded on user) ----------------

@api_v1_bp.get("/tags")
@token_required
def list_tags():
    user = g.current_user
    sorted_tags = sorted(user.tags or [], key=lambda t: t.name.lower())
    return jsonify({"items": [ser_tag(t) for t in sorted_tags]})

@api_v1_bp.post("/tags")
@token_required
def create_tag():
    from application.models.tag import Tag as _Tag
    user = g.current_user
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return _bad_request("name is required")
    if user.tag_by_name(name):
        return _bad_request("tag with that name exists", 409)
    t = _Tag(
        name=name,
        color=data.get("color", "#6c757d"),
        is_context=data.get("is_context", True),
        on_hold=data.get("on_hold", False),
        parent=data.get("parent") or None,
        exclusive_children=data.get("exclusive_children", False),
        maps_to_status=(data.get("maps_to_status") or None),
    )
    user.tags.append(t)
    user.save()
    return jsonify(ser_tag(t)), 201

@api_v1_bp.patch("/tags/<tag_name>")
@token_required
def update_tag(tag_name):
    user = g.current_user
    tag = user.tag_by_name(tag_name)
    if not tag:
        return _bad_request("not found", 404)
    data = request.get_json(silent=True) or {}
    if "color" in data:
        tag.color = data["color"]
    if "is_context" in data:
        tag.is_context = bool(data["is_context"])
    if "on_hold" in data:
        tag.on_hold = bool(data["on_hold"])
    if "parent" in data:
        tag.parent = data["parent"] or None
    if "exclusive_children" in data:
        tag.exclusive_children = bool(data["exclusive_children"])
    if "maps_to_status" in data:
        tag.maps_to_status = data["maps_to_status"] or None
    if "name" in data and data["name"] and data["name"] != tag.name:
        # Cascade rename
        old_name = tag.name
        new_name = data["name"]
        tag.name = new_name
        for other in user.tags or []:
            if other.parent == old_name:
                other.parent = new_name
        from application.models.item import Item as Item
        for ts in Item.objects(owner=user, tags=old_name):
            ts.tags = [new_name if x == old_name else x for x in ts.tags]
            ts.save()
    user.save()
    return jsonify(ser_tag(tag))

@api_v1_bp.delete("/tags/<tag_name>")
@token_required
def delete_tag(tag_name):
    user = g.current_user
    tag = user.tag_by_name(tag_name)
    if not tag:
        return _bad_request("not found", 404)
    Item.objects(owner=user, tags=tag.name).update(pull__tags=tag.name)
    for other in user.tags or []:
        if other.parent == tag.name:
            other.parent = None
    user.tags = [t for t in user.tags if t.name != tag.name]
    user.save()
    return "", 204

# ---------------- perspectives ----------------

@api_v1_bp.get("/perspectives")
@token_required
def list_perspectives():
    """Sidebar layout + the user's custom perspective definitions.

    Built-in perspectives are evaluated client-side from their well-known
    keys; custom ones ship their full filter tree so a client can apply
    the same filtering/grouping/sort offline."""
    from application.services.perspectives import (
        ALL_PERSPECTIVES, visible_keys,
    )
    user = g.current_user
    return jsonify(
        {
            "sidebar": visible_keys(user),
            "builtins": [
                {"key": p.key, "label": p.label, "icon": p.icon}
                for p in ALL_PERSPECTIVES
            ],
            "custom": [ser_perspective(p) for p in (user.custom_perspectives or [])],
        }
    )


# ---------------- misc ----------------

@api_v1_bp.get("/me")
@token_required
def me():
    user = g.current_user
    return jsonify(
        {
            "id": str(user.pk),
            "username": user.username,
            "email": user.email,
            "display_name": user.display_name or user.username,
            "is_admin": user.is_admin,
        }
    )

# ---------------- braindump ----------------

@api_v1_bp.get("/braindump")
@token_required
def get_braindump():
    return jsonify({"text": g.current_user.braindump or ""})


@api_v1_bp.put("/braindump")
@token_required
def save_braindump():
    data = request.get_json(silent=True) or {}
    user = g.current_user
    user.braindump = data.get("text") or ""
    user.save()
    return jsonify({"text": user.braindump})


@api_v1_bp.post("/braindump/convert")
@token_required
def convert_braindump():
    """Parse the Markdown dump and create/update projects + tasks. Mirrors
    the web blueprint; see application.services.braindump."""
    from application.services.braindump import apply_braindump
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return _bad_request("Nothing to convert.")
    stats = apply_braindump(text, g.current_user)
    return jsonify({"ok": True, "stats": stats})


@api_v1_bp.errorhandler(ValueError)
def _value_error(exc):
    return jsonify({"error": str(exc)}), 400


# ---------------- docs ----------------

@api_v1_bp.get("/openapi.json")
def openapi_spec():
    """Public OpenAPI 3.0 spec describing every /api/v1 endpoint."""
    from application.api.openapi import build_openapi_spec
    return jsonify(build_openapi_spec())


@api_v1_bp.get("/docs")
def swagger_ui():
    """Swagger UI rendered from the spec above. Loaded from the
    public CDN — no extra Python dependency for the docs page."""
    from flask import render_template_string
    html = """<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\">
<title>Cairn API · Swagger</title>
<link rel=\"stylesheet\"
      href=\"https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css\">
<style>body{margin:0;background:#ffffff;color:#222;}</style>
</head><body>
<div id=\"swagger-ui\"></div>
<script src=\"https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js\"></script>
<script>
window.addEventListener('load', () => {
  SwaggerUIBundle({
    url: '/api/v1/openapi.json',
    dom_id: '#swagger-ui',
    deepLinking: true,
    persistAuthorization: true,
  });
});
</script></body></html>"""
    return render_template_string(html)
