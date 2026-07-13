"""Template library — a hidden area to build reusable tasks/projects, organised
in folders, then copy them into real work with one click.

Every item here carries ``template=True`` and is therefore invisible to every
normal list (the ``Item.objects`` manager filters it out). We must resolve and
traverse with ``Item.all_objects`` scoped to the owner. Premium-only.
"""
from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request,
    url_for,
)
from flask_login import current_user, login_required

from application.models.item import ChecklistItem, Item
from application.services.permissions import premium_required
from application.services.templates import duplicate_template, use_template

templates_bp = Blueprint("templates", __name__, url_prefix="/templates")

# Kinds a user can hand-create in the library.
_CREATABLE = ("task", "project")


def _user():
    return current_user._get_current_object()


def _q(**kw):
    """Template-scoped queryset: owner + template=True, excluding trash."""
    return Item.all_objects(owner=_user(), template=True, trashed_at=None, **kw)


def _node(item_id: str) -> Item:
    item = _q(id=item_id).first()
    if item is None:
        abort(404)
    return item


def _ancestors(item: Item) -> list:
    """Root→…→parent chain (template nodes only), for breadcrumbs."""
    chain, seen, node = [], set(), item.parent
    while (node is not None and getattr(node, "template", False)
           and node.kind != "folder"):
        if node.id in seen:
            break
        seen.add(node.id)
        chain.append(node)
        node = node.parent
    chain.reverse()
    return chain


def _children(item: Item):
    return _q(parent=item).order_by("+sort_order", "+created_at")


# --------------------------------------------------------------------------
# Navigator
# --------------------------------------------------------------------------

@templates_bp.route("/")
@login_required
@premium_required
def index():
    folders = list(_q(kind="folder").order_by("+sort_order", "+title"))
    by_parent: dict = {}
    for f in folders:
        key = str(f.parent.id) if f.parent else None
        by_parent.setdefault(key, []).append(f)

    current_id = (request.args.get("folder") or "").strip()
    current = _node(current_id) if current_id else None
    if current is not None and current.kind != "folder":
        current = None

    # Task/project templates filed directly in the current folder (or at root).
    items = list(_q(kind__in=_CREATABLE,
                    parent=(current if current else None))
                 .order_by("+sort_order", "+created_at"))

    return render_template(
        "templates/index.html",
        by_parent=by_parent,
        current=current,
        items=items,
    )


@templates_bp.route("/folder", methods=["POST"])
@login_required
@premium_required
def create_folder():
    name = (request.form.get("name") or "").strip()[:200]
    parent_id = (request.form.get("parent") or "").strip()
    if not name:
        flash("Folder name is required.", "warning")
        return redirect(request.referrer or url_for("templates.index"))
    parent = _node(parent_id) if parent_id else None
    if parent is not None and parent.kind != "folder":
        abort(400)
    Item(owner=_user(), kind="folder", folder_domain="tasks",
         template=True, title=name, parent=parent, status="active").save()
    flash("Folder created.", "success")
    dest = str(parent.id) if parent else ""
    return redirect(url_for("templates.index", folder=dest or None))


@templates_bp.route("/create", methods=["POST"])
@login_required
@premium_required
def create():
    kind = (request.form.get("kind") or "task").strip()
    title = (request.form.get("title") or "").strip()[:500]
    folder_id = (request.form.get("folder") or "").strip()
    if kind not in _CREATABLE:
        abort(400)
    if not title:
        flash("Give the template a name.", "warning")
        return redirect(request.referrer or url_for("templates.index"))
    folder = _node(folder_id) if folder_id else None
    if folder is not None and folder.kind != "folder":
        abort(400)
    item = Item(
        owner=_user(), kind=kind, template=True, title=title, parent=folder,
        status="active" if kind == "project" else "open",
        project_type="parallel",
    )
    item.save()
    return redirect(url_for("templates.edit", item_id=str(item.id)))


# --------------------------------------------------------------------------
# Item editor
# --------------------------------------------------------------------------

@templates_bp.route("/item/<item_id>")
@login_required
@premium_required
def edit(item_id):
    item = _node(item_id)
    if item.kind == "folder":
        return redirect(url_for("templates.index", folder=str(item.id)))
    return render_template(
        "templates/edit.html",
        item=item,
        ancestors=_ancestors(item),
        children=list(_children(item)),
        user_tags=[t.name for t in (_user().tags or [])],
    )


@templates_bp.route("/item/<item_id>/edit", methods=["POST"])
@login_required
@premium_required
def save(item_id):
    item = _node(item_id)
    item.title = (request.form.get("title") or item.title).strip()[:500]
    item.note = request.form.get("note", item.note or "")
    raw_tags = request.form.get("tags", "")
    item.tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    prio = (request.form.get("priority") or "").strip()
    item.priority = int(prio) if prio in ("1", "2", "3", "4") else None
    item.flagged = request.form.get("flagged") == "on"
    est = (request.form.get("estimated_minutes") or "").strip()
    item.estimated_minutes = int(est) if est.isdigit() else None
    item.save()
    flash("Template saved.", "success")
    return redirect(url_for("templates.edit", item_id=str(item.id)))


def _is_ajax() -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _subitems_response(parent: Item):
    """Re-render just the sub-items block (for AJAX add/delete), else bounce
    back to the parent's editor."""
    if _is_ajax():
        return render_template("templates/_subitems.html",
                               item=parent, children=list(_children(parent)))
    return redirect(url_for("templates.edit", item_id=str(parent.id)))


@templates_bp.route("/item/<item_id>/subitems")
@login_required
@premium_required
def subitems(item_id):
    parent = _node(item_id)
    return render_template("templates/_subitems.html",
                           item=parent, children=list(_children(parent)))


@templates_bp.route("/item/<item_id>/subtask", methods=["POST"])
@login_required
@premium_required
def add_subtask(item_id):
    parent = _node(item_id)
    title = (request.form.get("title") or "").strip()[:500]
    # A section is only meaningful inside a project; default to a task.
    kind = "section" if (request.form.get("kind") == "section"
                         and parent.kind == "project") else "task"
    if title:
        Item(owner=_user(), kind=kind, template=True, parent=parent, title=title,
             status="active" if kind == "section" else "open").save()
    return _subitems_response(parent)


@templates_bp.route("/item/<item_id>/move", methods=["POST"])
@login_required
@premium_required
def move(item_id):
    item = _node(item_id)
    dest_id = (request.form.get("folder") or "").strip()
    dest = _node(dest_id) if dest_id else None
    if dest is not None and dest.kind != "folder":
        abort(400)
    item.parent = dest
    item.save()
    flash("Moved.", "success")
    return redirect(url_for("templates.edit", item_id=str(item.id)))


def _delete_subtree(item: Item) -> int:
    """Recursively hard-delete a template node and everything under it."""
    n = 0
    for child in list(_q(parent=item)):
        n += _delete_subtree(child)
    item.delete()
    return n + 1


@templates_bp.route("/item/<item_id>/delete", methods=["POST"])
@login_required
@premium_required
def delete(item_id):
    item = _node(item_id)
    parent = item.parent
    _delete_subtree(item)
    parent_is_tpl = (parent is not None and getattr(parent, "template", False)
                     and parent.kind in ("task", "project", "section"))
    # AJAX child-delete from the editor → return the parent's refreshed list.
    if _is_ajax() and parent_is_tpl:
        return render_template("templates/_subitems.html",
                               item=parent, children=list(_children(parent)))
    flash("Deleted.", "success")
    # Back to the editor of the parent template, or the folder navigator.
    if parent_is_tpl and parent.kind in _CREATABLE:
        return redirect(url_for("templates.edit", item_id=str(parent.id)))
    folder = str(parent.id) if (parent and parent.kind == "folder") else ""
    return redirect(url_for("templates.index", folder=folder or None))


@templates_bp.route("/folder/<item_id>/delete", methods=["POST"])
@login_required
@premium_required
def delete_folder(item_id):
    folder = _node(item_id)
    if folder.kind != "folder":
        abort(400)
    parent = folder.parent
    _delete_subtree(folder)
    flash("Folder deleted.", "success")
    dest = str(parent.id) if (parent and parent.kind == "folder") else ""
    return redirect(url_for("templates.index", folder=dest or None))


@templates_bp.route("/item/<item_id>/duplicate", methods=["POST"])
@login_required
@premium_required
def duplicate(item_id):
    item = _node(item_id)
    if item.kind not in _CREATABLE:
        abort(400)
    copy = duplicate_template(item, _user())
    flash("Template duplicated.", "success")
    return redirect(url_for("templates.edit", item_id=str(copy.id)))


@templates_bp.route("/item/<item_id>/use", methods=["POST"])
@login_required
@premium_required
def use(item_id):
    item = _node(item_id)
    if item.kind not in _CREATABLE:
        abort(400)
    new = use_template(item, _user())
    if new.kind == "project":
        flash(f"Created project “{new.title}”.", "success")
        return redirect(url_for("projects.detail", project_id=str(new.id)))
    flash(f"Added “{new.title}” to your Inbox.", "success")
    return redirect(url_for("tasks.inbox"))


# --------------------------------------------------------------------------
# Checklist (reuses static/js/checklist.js — endpoints return the partial)
# --------------------------------------------------------------------------

def _checklist_response(item: Item):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template("templates/_checklist.html", task=item)
    return redirect(url_for("templates.edit", item_id=str(item.id)) + "#checklist")


@templates_bp.route("/item/<item_id>/checklist", methods=["POST"])
@login_required
@premium_required
def checklist_add(item_id):
    item = _node(item_id)
    text = (request.form.get("text") or "").strip()[:500]
    if text:
        item.checklist.append(ChecklistItem(text=text))
        item.save()
    return _checklist_response(item)


@templates_bp.route("/item/<item_id>/checklist/<int:index>/toggle", methods=["POST"])
@login_required
@premium_required
def checklist_toggle(item_id, index):
    item = _node(item_id)
    if 0 <= index < len(item.checklist):
        item.checklist[index].done = not item.checklist[index].done
        item.save()
    return _checklist_response(item)


@templates_bp.route("/item/<item_id>/checklist/<int:index>/delete", methods=["POST"])
@login_required
@premium_required
def checklist_delete(item_id, index):
    item = _node(item_id)
    if 0 <= index < len(item.checklist):
        del item.checklist[index]
        item.save()
    return _checklist_response(item)
