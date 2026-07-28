"""Custom user-defined perspectives — list, create, edit, view results."""
import json
import secrets

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required
from mongoengine import Q

from application.models.perspective import (
    FILTER_FIELDS, GROUPING_MODES, OPS_BY_TYPE, SORT_FIELDS,
    CustomPerspective, FilterClause,
)

from application.services.perspective_filter import _decode_value, apply_filter
from application.models.item import Item

perspectives_bp = Blueprint("custom_perspectives", __name__, url_prefix="/perspectives")

def _scope(user):
    # Custom perspectives are personal action lists too — honour the same
    # "show shared work" toggle as the built-in perspectives (personal by
    # default). See permissions.perspective_base_q.
    from application.services import focus
    from application.services.permissions import perspective_base_q
    return focus.restrict(perspective_base_q(user), user)

def _find(user, pid):
    for p in user.custom_perspectives or []:
        if p.pid == pid:
            return p
    return None

# ----- List + delete --------------------------------------------------------

@perspectives_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()
    return render_template(
        "perspectives/list.html",
        perspectives=user.custom_perspectives or [],
        max_perspectives=20,
    )

@perspectives_bp.route("/<pid>/delete", methods=["POST"])
@login_required
def delete(pid):
    user = current_user._get_current_object()
    user.custom_perspectives = [p for p in user.custom_perspectives if p.pid != pid]
    user.save()
    flash("Perspective deleted.", "info")
    return redirect(url_for("custom_perspectives.index"))

# ----- Create / edit --------------------------------------------------------

@perspectives_bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    user = current_user._get_current_object()
    if len(user.custom_perspectives or []) >= 20:
        flash("You already have 20 custom perspectives — delete one first.", "warning")
        return redirect(url_for("custom_perspectives.index"))
    if request.method == "POST":
        p = _build_from_form(request.form, user, pid=secrets.token_urlsafe(8))
        user.custom_perspectives.append(p)
        user.save()
        flash(f"Created “{p.name}”.", "success")
        return redirect(url_for("custom_perspectives.view", pid=p.pid))
    return render_template("perspectives/form.html",
                           perspective=None,
                           **_form_context(user))

@perspectives_bp.route("/<pid>/edit", methods=["GET", "POST"])
@login_required
def edit(pid):
    user = current_user._get_current_object()
    p = _find(user, pid)
    if not p:
        abort(404)
    if request.method == "POST":
        updated = _build_from_form(request.form, user, pid=p.pid)
        idx = next(i for i, x in enumerate(user.custom_perspectives) if x.pid == pid)
        user.custom_perspectives[idx] = updated
        user.save()
        flash(f"Saved “{updated.name}”.", "success")
        return redirect(url_for("custom_perspectives.view", pid=pid))
    return render_template("perspectives/form.html",
                           perspective=p,
                           **_form_context(user))

def _form_context(user):
    return {
        "filter_fields": FILTER_FIELDS,
        "ops_by_type": OPS_BY_TYPE,
        "grouping_modes": GROUPING_MODES,
        "sort_fields": SORT_FIELDS,
        # Depth-first (parent→child) order with depth, so the tag pickers can
        # render the hierarchy with indentation instead of a flat list.
        "tag_tree": [{"name": t.name, "depth": d} for d, t in user.tag_tree()],
        "user_project_choices": [],  # filled by the global context_processor anyway
    }

def _build_from_form(form_data, user, *, pid) -> CustomPerspective:
    """Convert the submitted form into a CustomPerspective.

    The filter builder posts parallel arrays:
      clause_field[]  / clause_op[]  / clause_value[]
    plus root_op for the top-level AND/OR.
    """
    name = (form_data.get("name") or "Untitled").strip()[:80]
    icon = (form_data.get("icon") or "bi-star").strip()[:32]
    color = (form_data.get("color") or "#fabd2f").strip()[:16]
    in_sidebar = form_data.get("in_sidebar") == "1"

    root_op = form_data.get("root_op") or "and"
    if root_op not in ("and", "or"):
        root_op = "and"

    fields = form_data.getlist("clause_field[]")
    ops = form_data.getlist("clause_op[]")
    values = form_data.getlist("clause_value[]")

    clauses = []
    for f, op, raw in zip(fields, ops, values):
        if not f:
            continue
        # Store values as JSON so we keep numeric/bool/string types
        try:
            json.loads(raw or "null")
            encoded = raw
        except (ValueError, TypeError):
            encoded = json.dumps(raw)
        clauses.append(FilterClause(kind="leaf", field=f, op=op or _default_op(f), value=encoded))

    grouping = form_data.get("grouping") or "none"
    if grouping not in GROUPING_MODES:
        grouping = "none"
    grouping_tag = form_data.get("grouping_tag") or ""
    sort = form_data.get("sort") or "default"
    if sort not in SORT_FIELDS:
        sort = "default"

    return CustomPerspective(
        pid=pid,
        name=name,
        icon=icon,
        color=color,
        in_sidebar=in_sidebar,
        root_op=root_op,
        clauses=clauses,
        grouping=grouping,
        grouping_tag=grouping_tag,
        sort=sort,
        include_completed=form_data.get("include_completed") == "1",
        show_blocked=form_data.get("show_blocked") == "1",
    )

def _default_op(field: str) -> str:
    """Pick a reasonable default operator for a freshly-added clause."""
    for key, _, vtype in FILTER_FIELDS:
        if key == field:
            return (OPS_BY_TYPE.get(vtype) or ["is"])[0]
    return "is"

# ----- View results ---------------------------------------------------------

def _referenced_statuses(clauses):
    """Statuses a status clause explicitly selects for (op ``is`` / ``in``).

    Used so the base queryset doesn't pre-hide done/dropped when the user
    is deliberately filtering for them — selecting the status in the filter
    should be enough, without also ticking "include completed".
    """
    found = set()
    for c in clauses or []:
        if c.kind == "group":
            found |= _referenced_statuses(c.children)
        elif c.field == "status" and c.op in ("is", "in"):
            val = _decode_value(c.value)
            if isinstance(val, list):
                found.update(str(v) for v in val)
            elif val is not None:
                found.add(str(val))
    return found

@perspectives_bp.route("/<pid>")
@login_required
def view(pid):
    user = current_user._get_current_object()
    p = _find(user, pid)
    if not p:
        abort(404)

    qs = Item.objects(_scope(user))
    if not p.include_completed:
        # Don't pre-hide the closed statuses the filter explicitly selects for,
        # otherwise a `status is dropped` clause could never match anything.
        hide = {"done", "dropped"} - _referenced_statuses(p.clauses)
        if hide:
            qs = qs.filter(status__nin=list(hide))

    candidates = list(qs)
    # Map each tag to {itself + descendants} so a tag clause on a parent
    # also matches tasks carrying a child tag.
    tag_descendants = {t.name: user.tag_descendants(t.name) for t in (user.tags or [])}
    matched = apply_filter(candidates, p, tag_descendants)

    if not p.show_blocked:
        matched = [t for t in matched if not t.is_blocked()]

    # Sort
    if p.sort == "title":
        matched.sort(key=lambda t: (t.title or "").lower())
    elif p.sort == "due_asc":
        matched.sort(key=lambda t: t.due_date or _far_future())
    elif p.sort == "due_desc":
        matched.sort(key=lambda t: t.due_date or _far_past(), reverse=True)
    elif p.sort == "priority":
        matched.sort(key=lambda t: t.priority or 99)
    elif p.sort == "created":
        matched.sort(key=lambda t: t.created_at or _far_past(), reverse=True)
    elif p.sort == "updated":
        matched.sort(key=lambda t: t.updated_at or _far_past(), reverse=True)

    # Group
    groups = _group_matched(matched, p, user)

    # Project pks whose tasks are in this perspective — used by the
    # sidebar filter. Walk past sections (a task's parent might be a
    # section that lives inside the actual project we want to highlight).
    affected_pks = []
    for t in matched:
        par = t.parent
        if par is not None and par.kind == "section":
            par = par.parent
        if par is not None:
            pk = str(par.pk)
            if pk not in affected_pks:
                affected_pks.append(pk)

    return render_template(
        "perspectives/view.html",
        perspective=p,
        groups=groups,
        total=len(matched),
        affected_pks=affected_pks,
    )

def _far_future():
    from datetime import datetime, timezone
    return datetime(9999, 1, 1, tzinfo=timezone.utc)

def _far_past():
    from datetime import datetime, timezone
    return datetime(1970, 1, 1, tzinfo=timezone.utc)

def _group_matched(tasks, perspective, user):
    """Group ``tasks`` for the perspective's chosen mode."""
    from application.views.projects import _group_tasks
    g = perspective.grouping
    if g == "tag_specific":
        target = perspective.grouping_tag or ""
        children = sorted(
            (t.name for t in (user.tags or []) if t.parent == target),
            key=str.lower,
        )
        if children:
            # Parent tag with children → one bucket per child tag, plus
            # "Just @parent" for tasks tagged with the parent only and
            # "Without" for tasks tagged with neither.
            out = []
            for child in children:
                lst = [t for t in tasks if child in (t.tags or [])]
                if lst:
                    out.append((f"@{child}", "bi-tag-fill", lst, None))
            parent_only = [
                t for t in tasks
                if target in (t.tags or [])
                and not any(c in (t.tags or []) for c in children)
            ]
            if parent_only:
                out.append((f"Just @{target}", "bi-tag", parent_only, None))
            without = [
                t for t in tasks
                if target not in (t.tags or [])
                and not any(c in (t.tags or []) for c in children)
            ]
            if without:
                out.append(("Without", "bi-dash", without, None))
            return out
        # Leaf tag (no children) → simple with/without split.
        has = [t for t in tasks if target in (t.tags or [])]
        nope = [t for t in tasks if target not in (t.tags or [])]
        out = []
        if has:
            out.append((f"With @{target}", "bi-tag-fill", has, None))
        if nope:
            out.append(("Without", "bi-dash", nope, None))
        return out
    if g == "flagged":
        flagged = [t for t in tasks if t.flagged]
        unflagged = [t for t in tasks if not t.flagged]
        out = []
        if flagged:
            out.append(("Flagged", "bi-flag-fill text-warning", flagged, None))
        if unflagged:
            out.append(("Not flagged", "bi-flag", unflagged, None))
        return out
    if g == "project":
        by_proj: dict = {}
        none_lst = []
        for t in tasks:
            if t.project:
                by_proj.setdefault(t.project.name, []).append(t)
            else:
                none_lst.append(t)
        out = [(name, "bi-folder", lst, None)
               for name, lst in sorted(by_proj.items(), key=lambda x: x[0].lower())]
        if none_lst:
            out.append(("(no project)", "bi-dash", none_lst, None))
        return out
    if g == "none":
        return [("Tasks", "bi-list-ul", tasks, None)] if tasks else []
    # Delegate to the shared status/priority/tag/due grouper
    return _group_tasks(tasks, g)
