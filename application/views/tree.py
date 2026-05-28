"""Tree view: hierarchical projects + sections + tasks with drag-and-drop."""
from flask import Blueprint, render_template
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
    for arr in by_parent.values():
        arr.sort(key=lambda x: (not x.is_section, x.name.lower()))

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
