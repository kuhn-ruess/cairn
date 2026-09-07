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

    from application.services.permissions import viewable_container_pks
    projects = list(
        Item.objects(
            pk__in=list(viewable_container_pks(user)),
            status="active",
        )
    )
    by_parent: dict = {}
    shown = {str(p.pk) for p in projects}
    for p in projects:
        key = str(p.parent.pk) if p.parent else None
        # The template walks down from ``None``, so a container filed under a
        # parent that is not itself on the page — deleted, in the trash, shared
        # away, or simply not active — would be reachable from nowhere: it and
        # its whole subtree would disappear from the tree without a trace.
        # Show it at the top instead, where it can be opened, moved or deleted.
        if key not in shown:
            key = None
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
        # Group under the viewer's effective home: an assignee who filed a
        # delegated task under their own project sees it there, not under the
        # owner's (invisible) project.
        home = t.home_for(user)
        key = str(home.pk) if home else None
        # Same for a task whose home is not drawn (see above) — the alternative
        # is an open task that exists and is shown nowhere.
        if key not in shown:
            key = None
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
