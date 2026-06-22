"""Markdown notes — standalone ``kind="note"`` items organised in their own
folder tree (``folder_domain="notes"``), shared with the family like projects,
and cross-linked to tasks (via ``related``) and calendar events (via
``linked_events``).

The model work all lives on the unified ``Item`` (see models/item.py), so notes
inherit hierarchy (``parent``), sharing (``members`` + services/permissions),
soft-delete (``trashed_at`` + services/trash) and inline image paste
(``note_images``) for free — this module is just the views.
"""
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint, Response, abort, current_app, flash, jsonify, redirect,
    render_template, request, url_for,
)
from flask_login import current_user, login_required
from mongoengine import Q

from application.models.item import Item, LinkedEvent, NoteImage
from application.models.user import User

notes_bp = Blueprint("notes", __name__, url_prefix="/notes")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------- tree ----------

def notes_tree_for_sidebar(user):
    """Notes + notes-folders the user owns or is a member of, as a depth-first
    ``list[(depth, item)]`` for the notes navigator. Mirrors
    ``project_tree_for_sidebar`` but for the ``notes`` folder domain."""
    items = list(
        Item.objects(
            (Q(owner=user) | Q(members=user))
            & (Q(kind="note") | Q(kind="folder", folder_domain="notes"))
        ).only("pk", "title", "kind", "parent", "color", "members", "owner")
    )
    by_id = {str(i.pk): i for i in items}
    children: dict = {}
    for i in items:
        par = i.parent
        key = str(par.pk) if par is not None and str(par.pk) in by_id else None
        children.setdefault(key, []).append(i)

    ordered: list = []

    def walk(parent_key, depth):
        # Folders first, then notes; each group alphabetical — matches how the
        # project navigator reads.
        rows = sorted(
            children.get(parent_key, []),
            key=lambda x: (x.kind != "folder", (x.name or "").lower()),
        )
        for i in rows:
            ordered.append((depth, i))
            if i.kind == "folder":
                walk(str(i.pk), depth + 1)

    walk(None, 0)
    return ordered


# ---------- lookups ----------

def _note_or_404(note_id, *, kinds=("note", "folder")):
    """A note/folder the current user may view, else 404."""
    user = current_user._get_current_object()
    item = Item.objects(pk=note_id, kind__in=list(kinds)).first()
    if not item or not item.can_be_viewed_by(user):
        abort(404)
    return item


def _editable_note_or_404(note_id, *, kinds=("note", "folder")):
    user = current_user._get_current_object()
    item = Item.objects(pk=note_id, kind__in=list(kinds)).first()
    if not item or not item.can_be_edited_by(user):
        abort(404)
    return item


def _folder_choices(user, *, exclude_pk=None):
    """``[(pk, "Folder / Sub")]`` of the user's notes folders, for the
    parent picker. Plain breadcrumb labels; cheap in-memory path walk."""
    folders = list(
        Item.objects(owner=user, kind="folder", folder_domain="notes")
        .only("pk", "title", "parent")
    )
    by_id = {str(f.pk): f for f in folders}

    def label(f):
        parts, cur, seen = [], f, set()
        while cur is not None and str(cur.pk) not in seen:
            seen.add(str(cur.pk))
            parts.append(cur.name)
            par = cur.parent
            cur = by_id.get(str(par.pk)) if par else None
        return " / ".join(reversed(parts))

    out = [
        (str(f.pk), label(f))
        for f in folders if exclude_pk is None or f.pk != exclude_pk
    ]
    out.sort(key=lambda x: x[1].lower())
    return out


# ---------- views ----------

@notes_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()
    tree = notes_tree_for_sidebar(user)
    return render_template("notes/list.html", tree=tree, note=None)


@notes_bp.route("/<note_id>")
@login_required
def detail(note_id):
    user = current_user._get_current_object()
    item = _note_or_404(note_id)
    # A folder has no body of its own — land on the index (its children show
    # in the navigator).
    if item.kind == "folder":
        return redirect(url_for("notes.index"))
    tree = notes_tree_for_sidebar(user)
    linked_tasks = [r for r in item.related_items() if r.kind == "task"]
    return render_template(
        "notes/detail.html",
        note=item,
        tree=tree,
        linked_tasks=linked_tasks,
        can_edit=item.can_be_edited_by(user),
        is_owner=(item.owner == user),
        folder_choices=_folder_choices(user, exclude_pk=item.pk),
        member_choices=_member_choices(user),
        selected_members=[str(m.pk) for m in (item.members or [])],
    )


def _member_choices(user):
    from application.services.permissions import shareable_user_ids
    ids = [pk for pk in shareable_user_ids(user) if pk != str(user.pk)]
    users = User.objects(pk__in=ids) if ids else []
    return [(str(u.pk), str(u)) for u in users]


@notes_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    """Create a notes folder (and, with kind=note, a note). The navigator's
    'New note' button POSTs straight here with kind=note and opens the editor;
    folders go through the small form."""
    user = current_user._get_current_object()
    kind = (request.values.get("kind") or "note").strip()
    if kind not in ("note", "folder"):
        kind = "note"
    raw_parent = (request.values.get("parent") or "").strip()
    parent = None
    if raw_parent:
        parent = Item.objects(
            pk=raw_parent, owner=user, kind="folder", folder_domain="notes"
        ).first()

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        if kind == "note":
            item = Item(
                owner=user, kind="note", parent=parent,
                title=title or "Untitled note", note="",
            )
            item.save()
            return redirect(url_for("notes.detail", note_id=str(item.pk)))
        # folder
        if not title:
            flash("A folder needs a name.", "danger")
        else:
            item = Item(
                owner=user, kind="folder", folder_domain="notes",
                parent=parent, title=title, status="active",
            )
            item.save()
            flash("Folder created.", "success")
            return redirect(url_for("notes.index"))

    return render_template(
        "notes/form.html", kind=kind,
        folder_choices=_folder_choices(user),
        parent_id=raw_parent,
    )


@notes_bp.route("/<note_id>/edit", methods=["POST"])
@login_required
def edit(note_id):
    """Save title and/or Markdown body from the inline editor. Returns the
    rendered note partial for AJAX callers (X-Partial), else redirects."""
    item = _editable_note_or_404(note_id, kinds=("note",))
    if "title" in request.form:
        title = (request.form.get("title") or "").strip()
        item.title = title or "Untitled note"
    if "note" in request.form:
        item.note = request.form.get("note", "") or ""
    item.save()
    if request.headers.get("X-Partial") == "1":
        html = current_app.jinja_env.filters["markdown"](item.note)
        return jsonify({"ok": True, "title": item.title, "note": item.note,
                        "html": html})
    return redirect(url_for("notes.detail", note_id=str(item.pk)))


@notes_bp.route("/<note_id>/move", methods=["POST"])
@login_required
def move(note_id):
    """Drag-drop reparent within the notes tree. ``parent_id`` empty = top
    level. Only notes-folders are valid parents; cycles are rejected."""
    user = current_user._get_current_object()
    item = Item.objects(pk=note_id, owner=user, kind__in=["note", "folder"]).first()
    if not item:
        return jsonify({"error": "not found"}), 404
    raw_parent = (request.form.get("parent_id") or "").strip()
    if not raw_parent:
        item.parent = None
    else:
        new_parent = Item.objects(
            pk=raw_parent, owner=user, kind="folder", folder_domain="notes"
        ).first()
        if not new_parent:
            return jsonify({"error": "invalid parent"}), 400
        # Reject cycles (a folder can't move inside its own subtree).
        cur, depth = new_parent, 0
        while cur is not None and depth < 30:
            if cur.pk == item.pk:
                return jsonify({"error": "would create a cycle"}), 400
            cur, depth = cur.parent, depth + 1
        item.parent = new_parent
    item.save()
    return jsonify({"ok": True})


@notes_bp.route("/<note_id>/delete", methods=["POST"])
@login_required
def delete(note_id):
    """Soft-delete (trash) a note or notes-folder + its subtree."""
    user = current_user._get_current_object()
    item = Item.objects(pk=note_id, owner=user, kind__in=["note", "folder"]).first()
    if not item:
        abort(404)
    from application.services.trash import trash_item
    trash_item(item)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    flash("Moved to trash.", "info")
    return redirect(url_for("notes.index"))


@notes_bp.route("/<note_id>/share", methods=["POST"])
@login_required
def share(note_id):
    """Set the note's family/team members. Only the owner may share; each
    member id is validated against the owner's share scope."""
    user = current_user._get_current_object()
    item = Item.objects(pk=note_id, owner=user, kind__in=["note", "folder"]).first()
    if not item:
        abort(404)
    from application.services.permissions import resolve_assignable_user
    members = []
    for mid in request.form.getlist("members"):
        m = resolve_assignable_user(user, mid)
        if m:
            members.append(m)
    item.members = members
    item.save()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "count": len(members)})
    flash("Sharing updated.", "success")
    return redirect(url_for("notes.detail", note_id=str(item.pk)))


# ---------- task links (symmetric, via Item.related) ----------

@notes_bp.route("/<note_id>/link-task", methods=["POST"])
@login_required
def link_task(note_id):
    item = _editable_note_or_404(note_id, kinds=("note",))
    user = current_user._get_current_object()
    task_id = (request.form.get("task_id") or "").strip()
    task = Item.objects(pk=task_id, kind="task").first() if task_id else None
    if task and task.can_be_viewed_by(user):
        item.relate_to(task)
    return jsonify({"ok": True})


@notes_bp.route("/<note_id>/unlink-task/<task_id>", methods=["POST"])
@login_required
def unlink_task(note_id, task_id):
    item = _editable_note_or_404(note_id, kinds=("note",))
    other = Item.all_objects(pk=task_id).first()
    item.unrelate_from(other)
    return jsonify({"ok": True})


# ---------- calendar event links (snapshotted on the note) ----------

@notes_bp.route("/events")
@login_required
def events():
    """Upcoming calendar events across the user's connected calendars, for the
    event-link picker. Snapshots uid + calendar pk so the link can re-resolve."""
    user = current_user._get_current_object()
    from application.services.calendar_sync import fetch_events
    days = max(1, min(int(request.args.get("days", 30) or 30), 120))
    since = _utcnow() - timedelta(hours=6)
    until = since + timedelta(days=days)
    out = []
    for cal in (user.calendars or []):
        try:
            evs = fetch_events(cal, since, until, user=user)
        except Exception:  # noqa: BLE001 — a flaky calendar shouldn't 500 the picker
            continue
        for e in evs:
            out.append({
                "uid": e.uid,
                "calendar_pk": str(getattr(cal, "pk_ref", "") or ""),
                "calendar_name": e.calendar_name or cal.name,
                "title": e.title,
                "start": e.start.isoformat() if e.start else "",
                "all_day": e.all_day,
                "url": e.url or "",
            })
    out.sort(key=lambda x: x["start"])
    return jsonify({"events": out[:200]})


@notes_bp.route("/<note_id>/link-event", methods=["POST"])
@login_required
def link_event(note_id):
    """Attach a calendar event (posted as a snapshot) to the note. De-dupes
    on uid so re-linking the same event is a no-op."""
    item = _editable_note_or_404(note_id, kinds=("note",))
    uid = (request.form.get("uid") or "").strip()
    if not uid:
        return jsonify({"error": "missing event id"}), 400
    if any((le.uid == uid) for le in (item.linked_events or [])):
        return jsonify({"ok": True})  # already linked
    url = (request.form.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = ""  # only emit safe hrefs
    start = None
    raw_start = (request.form.get("start") or "").strip()
    if raw_start:
        try:
            start = datetime.fromisoformat(raw_start)
        except ValueError:
            start = None
    item.linked_events.append(LinkedEvent(
        uid=uid,
        calendar_pk=(request.form.get("calendar_pk") or "").strip() or None,
        title=(request.form.get("title") or "").strip(),
        start=start,
        all_day=(request.form.get("all_day") in ("1", "true", "True", "on")),
        url=url or None,
    ))
    item.save()
    return jsonify({"ok": True})


@notes_bp.route("/<note_id>/unlink-event", methods=["POST"])
@login_required
def unlink_event(note_id):
    item = _editable_note_or_404(note_id, kinds=("note",))
    uid = (request.form.get("uid") or "").strip()
    item.linked_events = [le for le in (item.linked_events or []) if le.uid != uid]
    item.save()
    return jsonify({"ok": True})


# ---------- inline pasted images (mirrors tasks.note_image*) ----------

@notes_bp.route("/<note_id>/note-image", methods=["POST"])
@login_required
def note_image_upload(note_id):
    import secrets
    from application.views.tasks import (
        NOTE_IMAGE_MAX_BYTES, ITEM_MAX_BYTES, ITEM_WARN_BYTES,
        _sniff_image_type, _item_bson_size,
    )
    item = _editable_note_or_404(note_id, kinds=("note",))
    fileobj = request.files.get("file")
    if not fileobj:
        return jsonify({"error": "no file"}), 400
    data = fileobj.read()
    if not data:
        return jsonify({"error": "empty file"}), 400
    if len(data) > NOTE_IMAGE_MAX_BYTES:
        return jsonify({"error": f"image too large (max {NOTE_IMAGE_MAX_BYTES // 1024 // 1024} MB)"}), 413
    ctype = _sniff_image_type(data[:12])
    if not ctype:
        return jsonify({"error": "unsupported image format"}), 415
    token = secrets.token_urlsafe(12)
    item.note_images.append(NoteImage(token=token, content_type=ctype, data=data))
    size = _item_bson_size(item)
    if size > ITEM_MAX_BYTES:
        item.note_images.pop()
        return jsonify({"error": "note is full — remove some images before adding more"}), 413
    item.save()
    payload = {"markdown": f"![pasted image]({url_for('notes.note_image', note_id=str(item.pk), token=token)})"}
    if size > ITEM_WARN_BYTES:
        payload["warning"] = f"This note is getting large ({size / 1024 / 1024:.1f} MB of 16 MB max) — consider removing some images."
    return jsonify(payload)


@notes_bp.route("/<note_id>/note-image/<token>")
@login_required
def note_image(note_id, token):
    user = current_user._get_current_object()
    item = Item.objects(pk=note_id).first()
    if not item or not item.can_be_viewed_by(user):
        abort(404)
    img = next((i for i in item.note_images if i.token == token), None)
    if not img:
        abort(404)
    return Response(bytes(img.data), mimetype=img.content_type,
                    headers={"Cache-Control": "private, max-age=86400"})
