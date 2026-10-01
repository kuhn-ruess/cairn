"""Tag views. Tags live embedded on ``User.tags``."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from mongoengine import Q

from application.forms import TagForm
from application.models.tag import Tag
from application.models.item import Item

tags_bp = Blueprint("tags", __name__, url_prefix="/tags")

def _populate_tag_choices(form, user, current=None):
    form.parent.choices = [("", "— no group —")] + [
        (t.name, str(t)) for t in user.tags or []
        if not current or t.name != current.name
    ]
    form.assigns_to.choices = [("", "— nobody —")] + [
        (str(u.pk), str(u)) for u in _assignable(user)
    ]


def _assignable(user):
    """The people this user may hand a task to — themselves plus every
    teammate. One pass, reused for the picker and for the list page."""
    from application.services.permissions import assignable_users
    return assignable_users(user)

def _tag_tree(tags):
    """Depth-first order under parent → list of (depth, tag)."""
    by_parent: dict = {}
    for t in tags:
        by_parent.setdefault(t.parent or None, []).append(t)
    out: list = []

    def walk(parent_name, depth):
        for t in sorted(by_parent.get(parent_name, []), key=lambda x: x.name.lower()):
            out.append((depth, t))
            walk(t.name, depth + 1)

    walk(None, 0)
    return out

@tags_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()
    tags = _tag_tree(user.tags or [])
    # One lookup for the whole page: the rows only need a name to show, and a
    # per-row dereference would be a query inside the loop.
    people = {}
    if any(t.assigns_to for _, t in tags):
        people = {str(u.pk): str(u) for u in _assignable(user)}
    return render_template("tags/list.html", tags=tags, people=people)

@tags_bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    user = current_user._get_current_object()
    form = TagForm()
    _populate_tag_choices(form, user)
    # Allow ?parent=<name> to pre-select a parent (used by the "+ child"
    # inline button on the tag list).
    if request.method == "GET":
        parent_q = request.args.get("parent", "").strip()
        if parent_q and user.tag_by_name(parent_q):
            form.parent.data = parent_q
    if form.validate_on_submit():
        if user.tag_by_name(form.name.data):
            flash("A tag with this name already exists.", "danger")
        else:
            user.tags.append(Tag(
                name=form.name.data,
                color=form.color.data,
                on_hold=form.on_hold.data,
                parent=form.parent.data or None,
                exclusive_children=form.exclusive_children.data,
                maps_to_status=form.maps_to_status.data or None,
                assigns_to=form.assigns_to.data or None,
            ))
            user.save()
            flash("Tag created.", "success")
            return redirect(url_for("tags.index"))
    return render_template("tags/form.html", form=form, tag=None)

@tags_bp.route("/<tag_name>")
@login_required
def detail(tag_name):
    user = current_user._get_current_object()
    tag = user.tag_by_name(tag_name)
    if not tag:
        abort(404)
    from application.services import focus
    tasks = Item.objects(
        focus.restrict(Q(owner=user), user),
        tags=tag.name, status__nin=["done", "dropped"],
    ).order_by("+due_date")
    return render_template("tags/detail.html", tag=tag, tasks=tasks)

@tags_bp.route("/<tag_name>/edit", methods=["GET", "POST"])
@login_required
def edit(tag_name):
    user = current_user._get_current_object()
    tag = user.tag_by_name(tag_name)
    if not tag:
        abort(404)
    form = TagForm(obj=tag)
    _populate_tag_choices(form, user, current=tag)
    if request.method == "GET":
        form.parent.data = tag.parent or ""
        form.maps_to_status.data = tag.maps_to_status or ""
        form.assigns_to.data = tag.assigns_to or ""
    if form.validate_on_submit():
        new_parent = form.parent.data or None
        # Cycle check via parent-chain walk
        seen = set([tag.name])
        cur = new_parent
        depth = 0
        while cur and depth < 20:
            if cur in seen:
                flash("Can't pick a descendant as the parent — would create a cycle.", "danger")
                return render_template("tags/form.html", form=form, tag=tag)
            seen.add(cur)
            parent_tag = user.tag_by_name(cur)
            cur = parent_tag.parent if parent_tag else None
            depth += 1

        old_name = tag.name
        new_name = form.name.data
        tag.name = new_name
        tag.color = form.color.data
        tag.on_hold = form.on_hold.data
        tag.parent = new_parent
        tag.exclusive_children = form.exclusive_children.data
        tag.maps_to_status = form.maps_to_status.data or None
        tag.assigns_to = form.assigns_to.data or None
        # Cascade rename: child tags' parent pointer + every task in the
        # user's set that mentions the old name.
        if old_name != new_name:
            for other in user.tags or []:
                if other.parent == old_name:
                    other.parent = new_name
            for t in Item.objects(owner=user, tags=old_name):
                t.tags = [new_name if x == old_name else x for x in t.tags]
                t.save()
        user.save()
        flash("Tag updated.", "success")
        return redirect(url_for("tags.detail", tag_name=tag.name))
    return render_template("tags/form.html", form=form, tag=tag)

@tags_bp.route("/<tag_name>/move", methods=["POST"])
@login_required
def move(tag_name):
    """Reparent a tag (drag-and-drop). ``parent`` body field is the
    new parent tag name, empty string makes it top-level. Cycles are
    rejected so a tag can't become its own ancestor."""
    from flask import jsonify
    user = current_user._get_current_object()
    tag = user.tag_by_name(tag_name)
    if not tag:
        return jsonify({"error": "not found"}), 404
    new_parent = (request.form.get("parent") or "").strip()
    if new_parent and new_parent == tag.name:
        return jsonify({"error": "tag can't be its own parent"}), 400
    # Cycle check: walk up new_parent's chain, refuse if we hit ``tag``.
    if new_parent:
        cur = user.tag_by_name(new_parent)
        depth = 0
        while cur and depth < 20:
            if cur.name == tag.name:
                return jsonify({"error": "would create a cycle"}), 400
            cur = user.tag_by_name(cur.parent) if cur.parent else None
            depth += 1
    tag.parent = new_parent or None
    user.save()
    return jsonify({"ok": True})


@tags_bp.route("/<tag_name>/delete", methods=["POST"])
@login_required
def delete(tag_name):
    from flask import jsonify
    user = current_user._get_current_object()
    tag = user.tag_by_name(tag_name)
    if not tag:
        abort(404)
    # Remove from all tasks
    Item.objects(owner=user, tags=tag.name).update(pull__tags=tag.name)
    # Detach children
    for other in user.tags or []:
        if other.parent == tag.name:
            other.parent = None
    user.tags = [t for t in user.tags if t.name != tag.name]
    user.save()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    flash("Tag deleted.", "info")
    return redirect(url_for("tags.index"))
