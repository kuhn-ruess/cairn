"""REST API v1 — token-authenticated JSON endpoints."""
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request
from mongoengine import Q

from application.api.auth import token_required
from application.api.serializers import project as ser_project
from application.api.serializers import tag as ser_tag
from application.api.serializers import task as ser_task
from application.api.serializers import task_compact as ser_task_compact
from application.api.serializers import note as ser_note
from application.api.serializers import notification as ser_notification
from application.api.serializers import note_share as ser_note_share
from application.api.serializers import perspective as ser_perspective
from application.api.serializers import tt_customer as ser_tt_customer
from application.api.serializers import tt_entry as ser_tt_entry
from application.api.serializers import tt_project as ser_tt_project
from application.api.serializers import tt_team as ser_tt_team

from application.models.user import User
from application.models.item import (
    Item, ITEM_KINDS, PROJECT_STATUS, PROJECT_TYPES, TASK_STATUS,
)

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

def _scope():
    """Task-list visibility for the API, mirroring ``Item.can_be_viewed_by``:
    items owned by / assigned to / directly shared with the user (the flat
    ``visible_items_q``), PLUS any item filed directly inside a container the
    user may open (``viewable_container_pks`` — owned, shared, or inherited from
    a shared ancestor). Without the container branch a member of a shared
    project saw none of its tasks via the list endpoint, even though single
    ``GET /tasks/<id>`` allowed them (that path already uses can_be_viewed_by).

    Note: matches on the item's *immediate* parent, so a sub-task nested under a
    task inside a shared project isn't picked up by the container branch (its
    parent is a task, not a container); it still lists if directly shared and is
    always reachable via ``GET /tasks/<id>``."""
    from bson import ObjectId
    from application.services.permissions import (
        visible_items_q, viewable_container_pks)
    q = visible_items_q(g.current_user)
    pks = [ObjectId(p) for p in viewable_container_pks(g.current_user)]
    if pks:
        q = q | Q(parent__in=pks)
    return q

def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    # A trailing Z says UTC. Stripping it left a naive value that
    # ``astimezone`` then read as the server's own wall-clock, which moved
    # every imported timestamp by the offset. A value with no zone at all is
    # still read that way — that is what a bare ``YYYY-MM-DD`` filter means.
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def _read_dt(data, key):
    """An optional timestamp from a request body: ``(value, error)``."""
    if key not in data or data.get(key) in (None, ""):
        return None, None
    try:
        return _parse_dt(data.get(key)), None
    except (TypeError, ValueError):
        return None, _bad_request(f"invalid {key}")

def _bad_request(msg: str, status: int = 400):
    return jsonify({"error": msg}), status


# What ``?sort=`` may order by — a whitelist, because the value goes straight
# into the query. A leading "-" reverses.
TASK_SORT_FIELDS = {"updated_at", "created_at", "due_date", "defer_date",
                    "plan_date", "priority", "sort_order", "title"}


def _tag_filtered(qs):
    """``?tag=`` / ``?exclude_tag=`` (both repeatable) applied to a task query.

    ``Item.tags`` is a list of tag NAMES, so both are plain name matches: every
    ``tag`` must be on the task, no ``exclude_tag`` may be. That pair is what a
    working queue is made of — "the ones tagged Cairn that are not already
    waiting for review" is one query, not a fetch and a client-side sieve."""
    tags = [t for t in request.args.getlist("tag") if t]
    if tags:
        qs = qs.filter(tags__all=tags)
    without = [t for t in request.args.getlist("exclude_tag") if t]
    if without:
        qs = qs.filter(tags__nin=without)
    return qs


def _day_end(raw, user):
    """A filter bound from ``YYYY-MM-DD`` or a full timestamp.

    A bare date means the END of that day in the user's own timezone — asking
    for what is due "before tomorrow" is asking about a day, not about
    midnight. Returns ``None`` when the value can't be read."""
    from application.services.tzutil import resolve_tz

    s = (raw or "").strip()
    if not s:
        return None
    try:
        if len(s) == 10:
            day = date.fromisoformat(s)
            end = datetime.combine(day, datetime.max.time())
            return end.replace(tzinfo=resolve_tz(user)).astimezone(timezone.utc)
        return _parse_dt(s)
    except ValueError:
        return None

# ---------------- tasks ----------------

@api_v1_bp.get("/tasks")
@token_required
def list_tasks():
    user = g.current_user
    qs = Item.objects(_scope())
    if request.args.get("mine") == "true":
        # _scope() also hands back what is merely VISIBLE — every task in a
        # project someone shared with you. A client asking for "my list" wants
        # the plate, not the window, which is what own_or_assigned_q draws:
        # owned by me or handed to me, team shares and stale member entries out.
        from application.services.permissions import own_or_assigned_q
        qs = qs.filter(own_or_assigned_q(user))
    status = request.args.get("status")
    if status:
        if status not in TASK_STATUS:
            return _bad_request(f"invalid status: {status}")
        qs = qs.filter(status=status)
    elif request.args.get("open") == "true":
        # "Offene" Aufgaben: alles außer abgeschlossen/verworfen. An den
        # konkreten ``status`` gekoppelt — ein expliziter Status gewinnt.
        qs = qs.filter(status__nin=["done", "dropped"])
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
    qs = _tag_filtered(qs)
    due_before = request.args.get("due_before")
    if due_before:
        bound = _day_end(due_before, user)
        if bound is None:
            return _bad_request("invalid due_before")
        qs = qs.filter(due_date__ne=None, due_date__lte=bound)
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
    # Newest-first stays the default (it is what an incremental sync pages
    # through); a queue is read by due date instead.
    sort = request.args.get("sort") or "-updated_at"
    if sort.lstrip("+-") not in TASK_SORT_FIELDS:
        return _bad_request(f"invalid sort: {sort}")
    qs = qs.order_by(sort).skip(offset).limit(limit)

    # Batch-dereference the page's references (owner/assigned_to/assigned_by/
    # project/comment authors) in a handful of $in queries instead of one
    # lazy fetch per field per row — otherwise serializing N rows costs
    # ~5N round-trips (the API's main latency source on a remote DB).
    items = [serialize(t) for t in qs.select_related(max_depth=1)]
    return jsonify({"items": items, "limit": limit, "offset": offset})


@api_v1_bp.get("/today")
@token_required
def today_debug():
    """Debug mirror of the web Today perspective: the SAME focus-aware scope
    and due/flag/plan/defer filter the UI uses, plus the active focus and a
    per-task ``match_reason`` — so you can see exactly what Today shows and
    why a task is in or out. Tasks only (the UI also folds in dated projects)."""
    from application.services import focus as focus_service
    from application.services.tzutil import local_date, resolve_tz

    from datetime import datetime as _dt, timedelta

    user = g.current_user
    now = datetime.now(timezone.utc)
    tz = resolve_tz(user)
    today_date = now.astimezone(tz).date()
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    # Mirror the UI's window length so this faithfully reflects what the web
    # Today shows (single-day vs multi-day behave differently).
    days = max(1, min(int(user.today_days or 1), 31))
    is_today_now = days == 1
    start_dt = _dt.combine(today_date, _dt.min.time()).replace(tzinfo=tz)
    end_dt = start_dt + timedelta(days=days)

    scoped = Q(owner=user) | Q(assigned_to=user)
    # A focus hides everything outside it — right for the UI, wrong for a
    # client asking "what is due", which would silently come back short.
    if request.args.get("ignore_focus") != "true":
        scoped = focus_service.restrict(scoped, user)
    qs = (Item.objects(scoped, kind="task",
                       status__nin=["done", "dropped", "someday"])
          .order_by("+due_date"))
    qs = _tag_filtered(qs)

    def match_reason(t):
        reasons = []
        if is_today_now:
            if t.due_date and t.due_date <= today_end:
                reasons.append("overdue" if t.due_date < now else "due_today")
            if t.flagged:
                reasons.append("flagged")
            if t.plan_date and local_date(t.plan_date, tz) <= today_date:
                reasons.append("planned")
            if t.defer_date and t.defer_date <= now and t.status == "next":
                reasons.append("next_available")
            if t.followup_at and t.followup_at <= today_end:
                reasons.append("followup_overdue" if t.followup_at < now else "followup_due")
        else:
            # window starts today → no lower bound (overdue stays visible)
            if t.due_date and t.due_date < end_dt:
                reasons.append("overdue" if t.due_date < now else "due_in_window")
            if t.flagged:
                reasons.append("flagged")
            if t.plan_date and t.plan_date < end_dt:
                reasons.append("planned")
            if t.followup_at and t.followup_at < end_dt:
                reasons.append("followup")
        return reasons

    matched = [(t, r) for t in qs for r in [match_reason(t)] if r]
    focus_item = focus_service.current(user)
    return jsonify({
        "now": now.isoformat(),
        "today_date": today_date.isoformat(),
        "today_days": days,
        "window_end": end_dt.isoformat(),
        "timezone": str(tz),
        "focus": {
            "active": focus_item is not None,
            "pk": str(focus_item.pk) if focus_item else None,
            "title": focus_item.title if focus_item else None,
        },
        "count": len(matched),
        "tasks": [{**ser_task_compact(t), "match_reason": r} for t, r in matched],
    })


@api_v1_bp.get("/due")
@token_required
def list_due():
    """Everything that is due: what is late, plus what falls due today.

    The one URL a queue reader needs — no perspective, no focus, no parameter
    archaeology. What is in it: TASKS (a dated project is not something you
    tick off), still open (done and dropped are gone, and so is someday — a
    parked task is not due, whatever date it carries), and carrying a due date
    that has been reached. Nothing else is folded in: a plan date is when you
    meant to work on it and a flag is not a deadline, which is what separates
    this from /today.

    ``days=N`` widens the window to the end of the N-th day from today,
    ``until=YYYY-MM-DD`` names that day outright, ``overdue=true`` cuts it back
    to what is already late. Sorted by due date, the oldest debt first.

    The focus is deliberately ignored: a client asking what is due must not
    come back short because the web session happens to sit inside a project.
    """
    user = g.current_user
    from application.services.permissions import own_or_assigned_q
    from application.services.tzutil import resolve_tz
    now = datetime.now(timezone.utc)
    tz = resolve_tz(user)

    until = request.args.get("until")
    if not until:
        try:
            days = max(0, min(int(request.args.get("days") or 0), 366))
        except ValueError:
            return _bad_request("invalid days")
        until = (now.astimezone(tz).date() + timedelta(days=days)).isoformat()
    bound = _day_end(until, user)
    if bound is None:
        return _bad_request("invalid until")

    qs = (Item.objects(own_or_assigned_q(user), kind="task",
                       status__nin=["done", "dropped", "someday"],
                       due_date__ne=None, due_date__lte=bound)
          .order_by("+due_date"))
    qs = _tag_filtered(qs)
    if request.args.get("overdue") == "true":
        qs = qs.filter(due_date__lt=now)
    try:
        limit = max(1, min(int(request.args.get("limit") or 200), 500))
    except ValueError:
        return _bad_request("invalid limit")

    tasks = [{**ser_task_compact(t), "overdue": t.due_date < now}
             for t in qs.limit(limit).select_related(max_depth=1)]
    return jsonify({
        "now": now.isoformat(),
        "until": bound.isoformat(),
        "timezone": str(tz),
        "count": len(tasks),
        "overdue_count": sum(1 for t in tasks if t["overdue"]),
        "tasks": tasks,
    })


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
    _apply_relations(task, data, user)  # after save: relate_to needs a pk
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
    _apply_relations(task, data, user)  # after save: relate_to needs a pk
    return jsonify(ser_task(task))

@api_v1_bp.delete("/tasks/<task_id>")
@token_required
def delete_task(task_id):
    from application.services.trash import trash_item
    user = g.current_user
    task = Item.objects(pk=task_id).first()
    if not task or task.owner != user:
        return _bad_request("not found", 404)
    # Soft-delete with subtree cascade (same as the web) so sub-tasks ride
    # along and aren't orphaned by a raw delete.
    trash_item(task)
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
        task.assign_to(assignee, by_user=user)  # fires the assignee notification
    else:
        task.assign_to(None)
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
        from application.services.trash import trash_item
        for t in tasks:
            if t.owner == user:
                trash_item(t)  # subtree cascade — no orphaned sub-tasks
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
                # Role-aware: an assignee files into their own project
                # (assignee_parent), never the owner's canonical parent.
                try:
                    t.set_home_for(user, project)
                except ValueError:
                    skipped += 1
                    continue
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

@api_v1_bp.post("/tasks/<task_id>/tags")
@token_required
def edit_tags(task_id):
    """Add and/or remove tags without sending the whole list back.

    ``PATCH /tasks/<id>`` replaces ``tags`` wholesale, so flipping one tag
    means read-modify-write — and two clients doing that at once lose one of
    the edits. Both lists are idempotent: adding a tag that is already there,
    or removing one that isn't, is a no-op."""
    user = g.current_user
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        return _bad_request("not found", 404)
    data = request.get_json(silent=True) or {}
    add = [str(t).strip() for t in (data.get("add") or []) if str(t).strip()]
    remove = {str(t).strip() for t in (data.get("remove") or []) if str(t).strip()}
    if not add and not remove:
        return _bad_request("add or remove is required")
    tags = [t for t in (task.tags or []) if t not in remove]
    for t in add:
        if t not in tags:
            tags.append(t)
    task.tags = tags
    from application.services.label_rules import apply_label_rules
    apply_label_rules(task, user)
    task.save()
    return jsonify(ser_task(task))


@api_v1_bp.get("/tasks/<task_id>/attachments/<token>")
@token_required
def get_attachment(task_id, token):
    """Download a file or image attachment stored inline on a task. The token
    comes from a task's ``attachments[]``/``note_images[]`` (see ser_task) —
    note_files are matched first, then the pasted note_images."""
    user = g.current_user
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        return _bad_request("not found", 404)
    if any(f.token == token for f in (task.note_files or [])):
        # Reuse the web helper so the Content-Disposition / force-download
        # rules for script-capable types stay in one place.
        from application.views.tasks import _note_file_response
        return _note_file_response(task, token)
    img = next((i for i in (task.note_images or []) if i.token == token), None)
    if img:
        from flask import Response
        return Response(
            bytes(img.data),
            mimetype=img.content_type or "image/webp",
            headers={"Cache-Control": "private, max-age=86400"},
        )
    return _bad_request("not found", 404)

def _apply_relations(item: Item, data: dict, user) -> None:
    """Sync the symmetric ``related`` ("see also") links to the id set in
    ``related_ids``. Must run AFTER the item is saved: ``relate_to`` /
    ``unrelate_from`` persist BOTH sides, so the item needs a pk. No-op when
    ``related_ids`` is absent (so a partial PATCH leaves links untouched)."""
    if "related_ids" not in data:
        return
    from application.services.permissions import resolve_visible_item
    desired = {}
    for rid in (data["related_ids"] or []):
        other = resolve_visible_item(user, rid)
        if not other:
            raise ValueError(f"related item not accessible: {rid}")
        if other.pk != item.pk:
            desired[str(other.pk)] = other
    current = {str(r.pk): r for r in item.related_items()}
    for pk, other in desired.items():
        if pk not in current:
            item.relate_to(other)
    for pk, other in current.items():
        if pk not in desired:
            item.unrelate_from(other)


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
    if "location" in data:
        task.location = (data["location"] or "").strip() or None
    if "sort_order" in data:
        try:
            task.sort_order = int(data["sort_order"])
        except (TypeError, ValueError):
            raise ValueError("sort_order must be an integer")
    if "waiting_on" in data:
        task.waiting_on = (data["waiting_on"] or "").strip() or None
    if "followup_at" in data:
        # Same coupling as the form: the defer date follows the follow-up
        # unless it is saying something else (Item.set_followup).
        task.set_followup(_parse_dt(data["followup_at"]))
    if "followup_every_days" in data:
        fed = data["followup_every_days"]
        if fed in (None, ""):
            task.followup_every_days = None
        else:
            try:
                task.followup_every_days = max(1, int(fed))
            except (TypeError, ValueError):
                raise ValueError("followup_every_days must be an integer")
    if "repeat_every_days" in data:
        task.repeat_every_days = data["repeat_every_days"]
    if "repeat_unit" in data:
        unit = data["repeat_unit"]
        if unit and unit not in ("day", "weekday", "weekend", "week", "month", "year"):
            raise ValueError(f"invalid repeat_unit: {unit}")
        task.repeat_unit = unit or "day"
    if "repeat_weekdays" in data:
        days = data["repeat_weekdays"] or []
        try:
            days = sorted({int(d) for d in days})
        except (TypeError, ValueError):
            raise ValueError("repeat_weekdays must be a list of integers")
        if any(d < 0 or d > 6 for d in days):
            raise ValueError("repeat_weekdays values must be 0 (Mon) .. 6 (Sun)")
        task.repeat_weekdays = days
    if "repeat_monthday" in data:
        md = data["repeat_monthday"]
        if md in (None, "", 0):
            task.repeat_monthday = None
        else:
            try:
                md = int(md)
            except (TypeError, ValueError):
                raise ValueError("repeat_monthday must be an integer 1..31")
            if not (1 <= md <= 31):
                raise ValueError("repeat_monthday must be 1..31")
            task.repeat_monthday = md
            task.repeat_weekdays = []  # mutually exclusive (see model)
    if "repeat_from_completion" in data:
        task.repeat_from_completion = bool(data["repeat_from_completion"])
    if "is_internal" in data:
        task.is_internal = bool(data["is_internal"])
    if "color" in data:
        task.color = (data["color"] or "").strip()
    if "checklist" in data:
        from application.models.item import ChecklistItem
        items = []
        for entry in (data["checklist"] or []):
            if isinstance(entry, str):
                entry = {"text": entry}
            text = (entry.get("text") or "").strip()
            if not text:
                raise ValueError("checklist items need text")
            items.append(ChecklistItem(text=text, done=bool(entry.get("done"))))
        task.checklist = items
    # ``parent_id`` and ``project_id`` are aliases (project == parent); accept
    # either. The parent may be another task (→ a sub-task) or a project/folder.
    if "parent_id" in data or "project_id" in data:
        from application.services.permissions import resolve_visible_item
        pid = data.get("parent_id") if "parent_id" in data else data.get("project_id")
        dest = None
        if pid:
            dest = resolve_visible_item(user, pid)
            if not dest:
                raise ValueError("parent_id not accessible")
        # Role-aware (matches the web): an assignee files into their own
        # project (assignee_parent); the owner moves the canonical parent.
        task.set_home_for(user, dest)
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
            # Route through assign_to so member-accumulation, per-user filing
            # cleanup and the assignee notification stay consistent with the web.
            if assignee != task.assigned_to:
                task.assign_to(assignee, by_user=user)
        else:
            task.assign_to(None, by_user=user)
    # Personal filing of a DELEGATED task: the actor must be a non-owner viewer
    # and own the destination. Never touches ``parent``. Handled after
    # ``assigned_to_id`` so a combined payload assigns first.
    if "assignee_parent_id" in data:
        from application.services.permissions import resolve_visible_item
        apid = data["assignee_parent_id"]
        if apid:
            if task.owner == user:
                raise ValueError("assignee_parent only applies to tasks delegated to you")
            cont = resolve_visible_item(user, apid)
            if not cont or cont.owner != user:
                raise ValueError("assignee_parent must be one of your own projects")
            task.set_home_for(user, cont)
        else:
            task.set_home_for(user, None)

# ---------------- projects ----------------

@api_v1_bp.get("/projects")
@token_required
def list_projects():
    user = g.current_user
    qs = Item.objects(Q(owner=user) | Q(members=user)).order_by("+name")
    return jsonify({"items": [ser_project(p) for p in qs.select_related(max_depth=1)]})

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
    _apply_relations(p, data, user)  # after save: relate_to needs a pk
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
    _apply_relations(p, data, user)  # after save: relate_to needs a pk
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
    if p.kind == "folder":
        # The same normalisation the web inspector does (views.projects
        # ._apply_form): a folder has no execution semantics, and the API is
        # the only way these fields could get onto one. Accepting them wrote a
        # state no screen shows and no screen can clear.
        p.project_type = "parallel"
        p.defer_date = None
        p.due_date = None
        p.plan_date = None
        p.auto_complete_when_done = False

# ---------------- notes ----------------
#
# A note and a notes FOLDER are the same document (``kind`` apart): both are
# containers for filing AND pages with a body, which is why one serializer and
# one set of routes covers them. The folder tree they live in is the ``notes``
# one — ``folder_domain``, the field that keeps the notes navigator and the
# project navigator from bleeding into each other.
#
# Writing a body goes through ``services.notes_write``, the same code the
# editor saves through: a body written without it leaves the block-id
# inventory, the backlink index and the derived title behind.

def _notes_q():
    """Notes and notes folders — never the project tree's folders."""
    return Q(kind="note") | Q(kind="folder", folder_domain="notes")


def _note_by_id(note_id, *, own=False):
    """One note or notes folder, by id. ``own`` restricts to the caller's own
    (sharing and deleting are the owner's), otherwise anything they may see —
    the caller still has to be allowed to edit before it is written."""
    from bson.errors import InvalidId
    from mongoengine.errors import ValidationError
    q = {"pk": note_id, "kind__in": ["note", "folder"]}
    if own:
        q["owner"] = g.current_user
    try:
        item = Item.objects(**q).first()
    except (InvalidId, ValidationError):
        return None
    if item and item.kind == "folder" and item.folder_domain != "notes":
        return None          # a project folder: not this tree
    return item


@api_v1_bp.get("/notes")
@token_required
def list_notes():
    """The notes tree, flat. ``parent_id=none`` gives the top level; without
    ``kind`` both notes and folders come back, so one call can draw a tree."""
    qs = Item.objects(_scope() & _notes_q())
    kind = request.args.get("kind")
    if kind:
        if kind not in ("note", "folder"):
            return _bad_request(f"invalid kind: {kind}")
        qs = qs.filter(kind=kind)
    parent_id = (request.args.get("parent_id") or "").strip()
    if parent_id:
        qs = qs.filter(parent=None) if parent_id == "none" \
            else qs.filter(parent=parent_id)
    updated_since = request.args.get("updated_since")
    if updated_since:
        since = _parse_dt(updated_since)
        if not since:
            return _bad_request("invalid updated_since")
        qs = qs.filter(updated_at__gte=since)
    # ``compact=true`` leaves the Markdown out — a client drawing the tree
    # does not need every note's text.
    body = request.args.get("compact") != "true"
    limit = max(1, min(int(request.args.get("limit", 100) or 100), 500))
    offset = max(0, min(int(request.args.get("offset", 0) or 0), 1_000_000))
    qs = qs.order_by("-updated_at").skip(offset).limit(limit)
    items = [ser_note(n, body=body) for n in qs.select_related(max_depth=1)]
    return jsonify({"items": items, "limit": limit, "offset": offset})


@api_v1_bp.post("/notes")
@token_required
def create_note():
    """Create a note or a notes folder — and the endpoint an IMPORTER uses.

    Three things here are for a caller that is filing something which already
    exists somewhere else, and that the editor never needs:

    * ``folder_path`` — say where it goes the way a person would
      (``Archiv/Agenda/2026/Kunden``); the levels that are missing are made.
    * ``created`` / ``updated`` — the note's real age. Without them an archive
      arrives dated today, which is most of what an archive is worth.
    * ``external_id`` — the id it has over there. Sending it again UPDATES
      that note instead of leaving a second copy behind, so an import can be
      re-run, widened or resumed without a bookkeeping file of its own.
    """
    user = g.current_user
    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or "note").strip()
    if kind not in ("note", "folder"):
        return _bad_request(f"invalid kind: {kind}")
    parent = None
    raw_parent = (data.get("parent_id") or "").strip()
    folder_path = (data.get("folder_path") or "").strip().strip("/")
    if raw_parent and folder_path:
        return _bad_request("give parent_id or folder_path, not both")
    if raw_parent:
        parent = _note_by_id(raw_parent, own=True)
        if not parent or parent.kind != "folder":
            return _bad_request("parent_id is not one of your notes folders")
    elif folder_path:
        from application.services.note_folders import ensure_path
        parent = ensure_path(user, folder_path)
    title = (data.get("title") or "").strip()
    if kind == "folder" and not title:
        return _bad_request("a folder needs a title")
    fmt = str(data.get("note_format") or "").strip()
    if fmt:
        from application.services import outline
        if fmt not in outline.FORMATS:
            return _bad_request(f"invalid note_format: {fmt}")
    created, err = _read_dt(data, "created")
    if err:
        return err
    updated, err = _read_dt(data, "updated")
    if err:
        return err

    external_id = (data.get("external_id") or "").strip()
    # The second run of an import lands here, not in a duplicate. Scoped to
    # the caller's own items and to this ``kind`` — a task imported under the
    # same id from the same system is not this note.
    item = (Item.objects(owner=user, kind=kind, external_id=external_id).first()
            if external_id else None)
    fresh = item is None
    if fresh:
        item = Item(owner=user, kind=kind, external_id=external_id or None,
                    source=(data.get("source") or "").strip() or None,
                    title="Untitled note")
    if fresh or raw_parent or folder_path:
        item.parent = parent
    if title:
        item.title = title
        item.title_auto = False
    elif fresh:
        # No name given, so the note's first line becomes it.
        item.title_auto = (kind == "note")
    if "tags" in data:
        item.tags = [str(t) for t in (data.get("tags") or []) if t]
    if fmt:
        item.note_format = fmt
    if kind == "folder":
        item.folder_domain = "notes"
        item.status = "active"
    from application.services.notes_write import apply_body
    if fresh or "note" in data:
        apply_body(item, data.get("note") or "", user)
    item.save()
    # After the save: ``Item.save`` stamps ``updated_at`` itself, so the
    # timestamps go on with an atomic update — the same move
    # ``notes_sync.retime`` makes for a file that brought its own dates.
    fields = {}
    if created:
        item.created_at = created
        fields["set__created_at"] = created
    if updated:
        item.updated_at = updated
        fields["set__updated_at"] = updated
    if fields:
        Item.all_objects(pk=item.pk).update(**fields)
    return jsonify(ser_note(item)), 201 if fresh else 200


@api_v1_bp.get("/notes/<note_id>")
@token_required
def get_note(note_id):
    item = _note_by_id(note_id)
    if not item or not item.can_be_viewed_by(g.current_user):
        return _bad_request("not found", 404)
    return jsonify(ser_note(item))


@api_v1_bp.patch("/notes/<note_id>")
@token_required
def update_note(note_id):
    user = g.current_user
    item = _note_by_id(note_id)
    if not item or not item.can_be_edited_by(user):
        return _bad_request("not found", 404)
    data = request.get_json(silent=True) or {}
    from application.services.notes_write import apply_body, apply_title
    if "title" in data:
        apply_title(item, data["title"])
    if "parent_id" in data:
        raw = (data.get("parent_id") or "").strip()
        if not raw:
            item.parent = None
        else:
            parent = _note_by_id(raw, own=True)
            if not parent or parent.kind != "folder" or parent.pk == item.pk:
                return _bad_request("parent_id is not one of your notes folders")
            item.parent = parent
    if "tags" in data:
        item.tags = [str(t) for t in (data.get("tags") or []) if t]
    if "note" in data:
        apply_body(item, data["note"], user)
    if "note_format" in data:
        # The same choice the note's ⋯ menu and the mirrored file's front
        # matter make. Only the setting: the body arrives as the caller wrote
        # it, and rewriting it under them is not this endpoint's business.
        from application.services import outline
        want = str(data.get("note_format") or "").strip()
        if want not in outline.FORMATS:
            return _bad_request(f"invalid note_format: {want}")
        item.note_format = want
    item.save()
    return jsonify(ser_note(item))


@api_v1_bp.delete("/notes/<note_id>")
@token_required
def delete_note(note_id):
    """Into the trash, with the subtree — the same soft delete the UI does, so
    it can be restored there. ``cleanup-trash`` purges it later."""
    from application.services.trash import trash_item
    item = _note_by_id(note_id, own=True)
    if not item:
        return _bad_request("not found", 404)
    trash_item(item)
    return "", 204


# ---------------- notes: sharing ----------------
#
# Who else may see a note is the one thing the notes tree had no way to say
# over the API, while the project tree has had ``member_ids`` all along.
# Sharing a FOLDER is the useful case: everything filed under it is shared
# with it, and a share set further down overrides the one above.

def _share_user(actor, raw):
    """A share target named by id, username or email — all three, because a
    client that has never listed the team knows only the name. The scope is
    ``resolve_assignable_user``'s either way: someone the actor shares a team
    with, never an arbitrary account."""
    from application.services.permissions import can_share_with, resolve_assignable_user
    target = resolve_assignable_user(actor, raw)
    if target:
        return target
    key = str(raw or "").strip()
    if not key:
        return None
    named = User.objects(Q(username__iexact=key) | Q(email__iexact=key)).first()
    return named if named and can_share_with(actor, named) else None


@api_v1_bp.get("/notes/<note_id>/share")
@token_required
def get_note_share(note_id):
    item = _note_by_id(note_id, own=True)
    if not item:
        return _bad_request("not found", 404)
    return jsonify(ser_note_share(item))


@api_v1_bp.put("/notes/<note_id>/share")
@token_required
def set_note_share(note_id):
    """Replace the share list, the way the dialog does: what is in the body is
    what the note ends up shared with, and an empty body unshares it."""
    from application.services.permissions import teams_of
    user = g.current_user
    item = _note_by_id(note_id, own=True)
    if not item:
        return _bad_request("not found", 404)
    data = request.get_json(silent=True) or {}

    members = []
    for raw in data.get("member_ids") or []:
        target = _share_user(user, raw)
        if not target:
            return _bad_request(f"member {raw} not shareable")
        if not any(m.pk == target.pk for m in members):
            members.append(target)

    allowed = {str(t.pk): t for t in teams_of(user)}
    teams = []
    for raw in data.get("team_ids") or []:
        team = allowed.get(str(raw))
        if not team:
            return _bad_request(f"team {raw} not shareable")
        if not any(t.pk == team.pk for t in teams):
            teams.append(team)

    item.members = members
    item.shared_teams = teams
    item.save()
    return jsonify(ser_note_share(item))


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
        assigns_to=(data.get("assigns_to") or None),
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
    if "assigns_to" in data:
        tag.assigns_to = data["assigns_to"] or None
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


# ---------------- time tracking ----------------
#
# The billing module is **team**-scoped, which is a different axis from the
# owner/assignee ``_scope()`` everything above uses: what a token may read here
# follows the team roles (``reachable_project_ids``), and what it may change
# follows team management. Reusing those two functions rather than restating
# them is deliberate — a second definition of "may see" is how the API and the
# UI drift apart.
#
# Read-only tokens are already barred from every mutating method by
# ``token_required``, so the merge below is closed to them without asking.


class _ApiError(Exception):
    """A failure a helper can raise from anywhere, rendered as the same JSON
    body ``_bad_request`` produces."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


@api_v1_bp.errorhandler(_ApiError)
def _api_error(exc):
    return jsonify({"error": exc.message}), exc.status


def _tt_team():
    """The team this call operates on — mirrors ``views.timetracking._team``.

    ``?team=<id>`` when the caller belongs to it, otherwise their pinned
    default. 403 without a team at all, 404 for a team they are not in: the
    same two answers the web module gives, so a token can never learn that a
    team exists by being told it is forbidden rather than missing."""
    from application.services.timetracking import default_team, user_teams
    teams = user_teams(g.current_user)
    if not teams:
        raise _ApiError("not a member of any team", 403)
    raw = (request.args.get("team") or "").strip()
    if not raw:
        return default_team(g.current_user)
    for t in teams:
        if str(t.pk) == raw:
            return t
    raise _ApiError("unknown team", 404)


def _tt_customer(cid, team):
    from bson import ObjectId
    from application.models.timetracking import Customer
    from application.services.timetracking import to_oid
    c = Customer.objects(pk=to_oid(cid) or ObjectId(), team=team).first()
    if c is None:
        raise _ApiError("not found", 404)
    return c


def _tt_biller(team):
    """Who may shape the team's books over the API — the same answer the web
    module gives (``views.timetracking._billing_manager``): a manager, or a
    member the manager named a biller. A biller already settles the month on
    the whole book; filing that book is the same job."""
    from application.services.timetracking import can_bill
    if not can_bill(g.current_user, team):
        raise _ApiError("team managers and billers only", 403)


@api_v1_bp.get("/time/teams")
@token_required
def tt_list_teams():
    """The teams this token may act in, and which one the endpoints below
    default to when ``?team=`` is omitted."""
    from application.services.timetracking import default_team, user_teams
    teams = user_teams(g.current_user)
    current = default_team(g.current_user) if teams else None
    return jsonify({
        "items": [ser_tt_team(t) for t in
                  sorted(teams, key=lambda t: (t.name or "").lower())],
        "default_team_id": str(current.pk) if current else None,
    })


@api_v1_bp.get("/time/customers")
@token_required
def tt_list_customers():
    """``?q=`` matches the name (case-insensitive substring); archived
    customers are left out unless ``?include_archived=1``."""
    from application.models.timetracking import Customer
    team = _tt_team()
    qs = Customer.objects(team=team)
    if request.args.get("include_archived") != "1":
        qs = qs.filter(archived__ne=True)
    q = (request.args.get("q") or "").strip()
    if q:
        qs = qs.filter(name__icontains=q)
    return jsonify({"items": [ser_tt_customer(c)
                              for c in qs.order_by("+name")]})


@api_v1_bp.get("/time/customers/duplicates")
@token_required
def tt_customer_duplicates():
    """Customers that look like one client filed twice, grouped by name.

    The importers adopt an existing customer by name now, so what this returns
    is what they created *before* they did — records nothing merges on its own,
    because the hours underneath them mean a customer cannot simply be deleted.
    Each group is heaviest-first: the record carrying the history is normally
    the one to merge the others into (``POST /time/customers/<id>/merge``)."""
    from application.services.timetracking import duplicate_customers
    team = _tt_team()
    groups = duplicate_customers(team)
    return jsonify({"team_id": str(team.pk), "items": groups,
                    "count": len(groups)})


@api_v1_bp.get("/time/customers/<cid>")
@token_required
def tt_get_customer(cid):
    team = _tt_team()
    return jsonify(ser_tt_customer(_tt_customer(cid, team)))


@api_v1_bp.post("/time/customers/<cid>/archive")
@token_required
def tt_archive_customer(cid):
    """Archive this customer, or bring it back — ``{"archived": true|false}``.

    Archived means out of the way, not gone: the history stays and still
    bills, but the customer *and every project under it* drop out of the
    pickers. Reversible, which is why it takes a biller rather than a
    manager."""
    team = _tt_team()
    _tt_biller(team)
    c = _tt_customer(cid, team)
    data = request.get_json(silent=True) or {}
    if "archived" not in data:
        return _bad_request("archived is required (true or false)")
    c.archived = bool(data.get("archived"))
    c.save()
    return jsonify(ser_tt_customer(c))


@api_v1_bp.post("/time/customers/<cid>/merge")
@token_required
def tt_merge_customer(cid):
    """Fold this customer into the one named by ``into`` and delete it.

    Everything moves — projects, every logged hour, and the import keys, so the
    next sync goes to the survivor instead of re-creating what was just merged.
    A booking the target already carries arrives cancelled rather than doubling
    the total; one already billed is left alone and reported. See
    ``services.timetracking.merge_customers`` for why in that order.

    Irreversible, so it is closed to anyone below a biller and to read-only
    tokens, and ``into`` must be given explicitly — there is no sensible
    default for which record survives."""
    from application.services.timetracking import merge_customers
    team = _tt_team()
    _tt_biller(team)
    data = request.get_json(silent=True) or {}
    into = (data.get("into") or "").strip()
    if not into:
        return _bad_request("into is required — the customer to merge INTO")
    source = _tt_customer(cid, team)
    target = _tt_customer(into, team)
    if str(source.pk) == str(target.pk):
        return _bad_request("into must be a different customer")
    name = source.name
    stats = merge_customers(source, target)
    return jsonify({"merged": name, "into": ser_tt_customer(target),
                    "stats": stats})


@api_v1_bp.get("/time/projects")
@token_required
def tt_list_projects():
    """The team's billing projects. ``?customer_id=`` narrows to one customer.

    Team-wide, deliberately, and NOT bounded by ``reachable_project_ids``: the
    module's own management page (``views.timetracking.index``) shows every
    customer and project of the team to any member, so bounding this would make
    the API answer a question the UI answers differently. What that scope
    governs is *logged time*, which is why ``/time/entries`` uses it and this
    does not."""
    from bson import ObjectId
    from application.models.timetracking import TimeTrackingProject
    from application.services.timetracking import to_oid
    team = _tt_team()
    qs = TimeTrackingProject.objects(team=team)
    if request.args.get("include_archived") != "1":
        qs = qs.filter(archived__ne=True)
    raw = (request.args.get("customer_id") or "").strip()
    if raw:
        qs = qs.filter(customer=to_oid(raw) or ObjectId())
    return jsonify({"items": [ser_tt_project(p) for p in qs.order_by("+name")]})


@api_v1_bp.get("/time/entries")
@token_required
def tt_list_entries():
    """Logged time, over the same reader and the same filters as the report.

    Range: ``?period=`` (a preset) or ``?from=``/``?to=`` (which *are* the
    period when given without one), defaulting to this month. ``state`` is
    ``active`` (default), ``cancelled`` — deleted time is cancelled and never
    dropped, so it has to stay findable — or ``all``. ``billable`` and
    ``locked`` take ``1``/``0``."""
    from application.services.timetracking import (
        archived_project_ids, build_report_rows, reachable_project_ids,
        resolve_period)
    from application.services.tz import user_zone
    team = _tt_team()
    user = g.current_user
    args = request.args
    today = datetime.now(user_zone(user)).date()
    preset = args.get("period") or (
        "custom" if (args.get("from") or args.get("to")) else "this_month")
    date_from, date_to = resolve_period(
        preset, args.get("from"), args.get("to"), today)

    def _tri(name):
        v = args.get(name)
        return True if v == "1" else False if v == "0" else None

    state = args.get("state")
    rows = build_report_rows(
        team=team, scope_ids=reachable_project_ids(user, team),
        date_from=date_from, date_to=date_to,
        customer_id=args.get("customer_id") or None,
        project_id=args.get("project_id") or None,
        user_id=args.get("user_id") or None,
        billable=_tri("billable"), locked=_tri("locked"),
        state=state if state in ("active", "cancelled", "all") else "active",
        archived_ids=archived_project_ids(team), viewer=user,
    )
    items = [ser_tt_entry(r) for r in rows]
    return jsonify({
        "team_id": str(team.pk),
        "from": date_from.isoformat(), "to": date_to.isoformat(),
        "count": len(items),
        "total_minutes": sum(r["minutes"] for r in rows
                             if r["status"] == "active"),
        "items": items,
    })


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

# ---------------- scratchpad ----------------

@api_v1_bp.get("/scratchpad")
@token_required
def get_scratchpad():
    return jsonify({"text": g.current_user.scratchpad or ""})


@api_v1_bp.put("/scratchpad")
@token_required
def save_scratchpad():
    data = request.get_json(silent=True) or {}
    user = g.current_user
    user.scratchpad = data.get("text") or ""
    user.save()
    return jsonify({"text": user.scratchpad})


@api_v1_bp.post("/scratchpad/convert")
@token_required
def convert_scratchpad():
    """Turn dumped text into tasks — one per line, into ``project_id`` (blank
    = Inbox). See application.services.capture."""
    from application.services import capture
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return _bad_request("Nothing to convert.")
    project = None
    raw = (data.get("project_id") or "").strip()
    if raw:
        project = Item.objects(pk=raw, kind__in=["project", "section", "folder"]).first()
        if not project or not project.can_be_edited_by(g.current_user):
            return _bad_request("Unknown project.")
    stats = capture.create_tasks(text, g.current_user, parent=project)
    return jsonify({"ok": True, "stats": stats})


@api_v1_bp.errorhandler(ValueError)
def _value_error(exc):
    return jsonify({"error": str(exc)}), 400


# ---------------- docs ----------------

@api_v1_bp.get("/notifications")
@token_required
def list_notifications():
    """The caller's own bell entries, newest first.

    This is the one feed that carries both halves of "somebody addressed me" —
    an assignment (``Item.assign_to``) and an ``@name`` in a comment
    (``views.tasks._notify_mentions``) — which is what a poller outside the app
    needs; a mention alone never puts the task in ``_scope()``. ``unread=true``
    narrows it to what nobody has claimed yet, and ``read`` is the claim (see
    POST /notifications/read)."""
    user = g.current_user
    rows = list(reversed(user.notifications or []))   # stored oldest-last
    if request.args.get("unread") == "true":
        rows = [n for n in rows if not n.read]
    kind = request.args.get("kind")
    if kind:
        wanted = {k.strip() for k in kind.split(",") if k.strip()}
        rows = [n for n in rows if n.kind in wanted]
    limit = max(1, min(int(request.args.get("limit", 50) or 50), 200))
    return jsonify({
        "items": [ser_notification(n) for n in rows[:limit]],
        "unread": user.unread_notification_count,
    })


@api_v1_bp.post("/notifications/read")
@token_required
def mark_notifications_read():
    """Mark entries read — with ``ids``, only those; without, all of them (the
    bell's "mark all read"). An agent working the feed should send only the ids
    it actually took, so whatever it left is still waiting for the next reader."""
    data = request.get_json(silent=True) or {}
    ids = data.get("ids")
    if ids is not None:
        ids = [str(i) for i in ids if str(i).strip()]
        if not ids:
            return _bad_request("ids must not be empty")
    user = g.current_user
    if user.mark_notifications_read(ids):
        user.save()
    return jsonify({"ok": True, "unread": user.unread_notification_count})


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
