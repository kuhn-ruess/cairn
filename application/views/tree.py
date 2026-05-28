"""Tree view: hierarchical projects + sections + tasks with drag-and-drop."""
from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required
from mongoengine import Q
from application.models.item import Item

tree_bp = Blueprint("tree", __name__, url_prefix="/tree")

@tree_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()

    projects = list(
        Item.objects(
            (Q(owner=user) | Q(members=user)),
            status="active",
        )
    )
    by_parent: dict = {}
    for p in projects:
        key = str(p.parent.pk) if p.parent else None
        by_parent.setdefault(key, []).append(p)
    # Sort: sections first within a parent, then sort_order, then name.
    for arr in by_parent.values():
        arr.sort(key=lambda x: (not x.is_section, x.sort_order or 0, x.name.lower()))

    tasks = list(
        Item.objects(
            (Q(owner=user) | Q(assigned_to=user)),
            kind="task",
            status__nin=["done", "dropped"],
        ).order_by("+sort_order", "+created_at")
    )
    tasks_by_project: dict = {None: []}
    for t in tasks:
        key = str(t.project.pk) if t.project else None
        tasks_by_project.setdefault(key, []).append(t)

    return render_template(
        "tree.html",
        by_parent=by_parent,
        tasks_by_project=tasks_by_project,
    )


@tree_bp.route("/api/quick-add", methods=["POST"])
@login_required
def api_quick_add():
    """Tree-page quick-add — create a project or folder without a reload.

    Body (form): name, kind (project|folder), parent_id (optional).
    Returns ``{"ok": true, "id": "<pk>"}``. The client then calls
    ``bktodoRefreshMain()`` to redraw the tree.
    """
    user = current_user._get_current_object()
    name = (request.form.get("name") or "").strip()[:200]
    kind = (request.form.get("kind") or "project").strip()
    parent_id = (request.form.get("parent_id") or "").strip()
    if not name:
        return jsonify({"error": "Name is required."}), 400
    if kind not in ("project", "folder"):
        return jsonify({"error": "Invalid kind."}), 400

    parent = None
    if parent_id:
        parent = Item.objects(pk=parent_id, owner=user).first()
        if not parent:
            return jsonify({"error": "Invalid parent."}), 400
        if parent.kind not in ("project", "folder"):
            return jsonify({"error": "Parent must be a project or folder."}), 400

    if kind == "project":
        from application.services.permissions import (
            FREE_PROJECT_LIMIT, at_project_limit,
        )
        if at_project_limit(user):
            return jsonify({
                "error": f"Free plan limit reached: {FREE_PROJECT_LIMIT} projects.",
            }), 403

    item = Item(
        owner=user,
        kind=kind,
        title=name,
        parent=parent,
        project_type="parallel",
        status="active",
    )
    item.save()
    return jsonify({"ok": True, "id": str(item.pk)})
