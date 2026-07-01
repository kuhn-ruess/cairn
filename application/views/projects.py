"""Item views — CRUD plus per-project task list."""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

def _is_inspector():
    return request.args.get("inspector") == "1" or request.headers.get("X-Inspector-Request") == "1"

def _render_project_inspector(project, user, *, just_saved=False):
    from application.services.tz import localise_form_dates
    form = ProjectForm(obj=project)
    localise_form_dates(form, user)
    _populate_choices(form, user, project)
    form.parent.data = str(project.parent.pk) if project.parent else ""
    form.members.data = [str(m.pk) for m in (project.members or [])]
    form.shared_teams.data = [str(t.pk) for t in (project.shared_teams or [])]
    form.kind.data = _kind_of(project)
    return render_template(
        "projects/_inspector.html",
        form=form,
        project=project,
        form_action=url_for("projects.edit", project_id=str(project.pk)),
        just_saved=just_saved,
    )
from flask_login import current_user, login_required
from mongoengine import Q

from application.forms import ProjectForm, CommentForm

from application.models.user import User
from application.models.item import Item

projects_bp = Blueprint("projects", __name__, url_prefix="/projects")

def _populate_choices(form: ProjectForm, user: User, current: Item | None = None) -> None:
    """Build the parent + members + tags dropdowns.

    Any owned project, folder, or section can act as a parent (sections, in
    particular, must live *inside* their project). We exclude the project
    being edited so it can't be its own ancestor.
    """
    # Tag pool — same as tasks (embedded on user), DFS-ordered.
    from application.views.tasks import _tag_choices_tree
    form.tags.choices = _tag_choices_tree(user.tags or [])
    # "Inside" picker holds containers only — projects and folders. Sections
    # live inline in a project and shouldn't host sub-projects; tasks aren't
    # containers. Labels carry the full breadcrumb (see services/containers.py).
    from application.services.containers import container_choices
    # ``include_shared`` so a team admin (or anyone shared into a container) can
    # parent a new project/folder under a folder shared with their team — not
    # just under one they own. The save path re-validates the chosen parent.
    form.parent.choices = [("", "— top level —")] + container_choices(
        user, selectable_kinds=("project", "folder"), include_shared=True,
        exclude_pk=current.pk if current else None,
    )
    # Sharing is restricted to team members. Free / solo users see no
    # candidates (the form still renders, but the dropdown is empty so
    # they can't share). Premium and team_admin show their teammates.
    from application.services.permissions import shareable_user_ids, teams_of
    candidate_ids = [pk for pk in shareable_user_ids(user) if pk != str(user.pk)]
    users = User.objects(pk__in=candidate_ids) if candidate_ids else []
    form.members.choices = [(str(u.pk), str(u)) for u in users]
    # Share with a whole team (inherited by children, overridable on a child).
    form.shared_teams.choices = [(str(t.pk), t.name) for t in teams_of(user)]

def _apply(form: ProjectForm, project: Item) -> None:
    from application.services.tz import attach_user_tz
    user = current_user._get_current_object() if current_user.is_authenticated else None
    project.name = form.name.data
    project.note = form.note.data or ""
    project.color = (form.color.data or "").strip()
    project.tags = list(form.tags.data or [])
    project.defer_date = attach_user_tz(form.defer_date.data, user)
    project.due_date = attach_user_tz(form.due_date.data, user)
    project.plan_date = attach_user_tz(form.plan_date.data, user)
    # NB: ``jira_url`` is intentionally NOT set here — it's configured only in
    # the CSV import dialog (see projects.import_csv), so a normal inspector
    # save must leave any previously-imported base URL untouched.
    kind = form.kind.data or "project"
    project.kind = kind
    # Parent and members are user-supplied pks — validate against the
    # caller's visibility / share scope so a tampered POST can't graft
    # the project onto someone else's tree or hand membership to
    # arbitrary accounts.
    from application.services.permissions import (
        resolve_assignable_user, resolve_visible_item,
    )
    project.parent = (
        resolve_visible_item(user, form.parent.data) if form.parent.data else None
    )
    if form.members.data:
        members = []
        for mid in form.members.data:
            m = resolve_assignable_user(user, mid)
            if m:
                members.append(m)
        project.members = members
    else:
        project.members = []
    # Team shares — only teams the user actually participates in (a tampered POST
    # can't share into a stranger's team).
    from application.services.permissions import teams_of
    allowed_team_ids = {str(t.pk): t for t in teams_of(user)}
    project.shared_teams = [
        allowed_team_ids[tid] for tid in (form.shared_teams.data or [])
        if tid in allowed_team_ids
    ]

    # Kind-specific cleanup. Sections share the project_type field
    # (they can be sequential too — the per-section blocking logic
    # honours it). Folders have no execution semantics.
    if kind == "project":
        from datetime import timedelta
        project.project_type = form.project_type.data
        project.status = form.status.data
        project.review_interval_days = form.review_interval_days.data or 0
        project.auto_complete_when_done = bool(form.auto_complete_when_done.data)
        # Keep the next-review clock consistent with the (possibly changed)
        # interval: reviewed → last + interval; never reviewed or interval 0 →
        # None (the review queue treats never-reviewed-with-interval as due).
        iv = project.review_interval_days
        if iv and project.last_reviewed_at:
            project.next_review_at = project.last_reviewed_at + timedelta(days=iv)
        else:
            project.next_review_at = None
    elif kind == "section":
        project.project_type = form.project_type.data or "parallel"
        project.status = "active"
        project.review_interval_days = 0
        project.next_review_at = None
        project.last_reviewed_at = None
        project.auto_complete_when_done = False
    else:  # folder
        project.project_type = "parallel"
        project.status = "active"
        project.review_interval_days = 0
        project.next_review_at = None
        project.last_reviewed_at = None
        project.auto_complete_when_done = False

def _kind_of(project: Item) -> str:
    if project.is_section:
        return "section"
    if project.is_folder:
        return "folder"
    return "project"

@projects_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()
    # Sections live inside a project; they belong on the project detail
    # page, not in the top-level project list.
    show_inactive = request.args.get("show_inactive") == "1"
    # Availability toggle (OmniFocus-style): persisted so the sidebar tree
    # honours it too. ``?show_deferred=0/1`` flips and saves it.
    sd = request.args.get("show_deferred")
    if sd is not None:
        user.show_deferred_projects = (sd == "1")
        user.save()
    show_deferred = bool(user.show_deferred_projects)
    from application.services import focus
    focus_pks = focus.focus_pks(user)
    fq = Q(pk__in=focus_pks) if focus_pks is not None else Q()
    from application.services.permissions import viewable_container_pks
    qs = Item.objects(
        fq & Q(pk__in=list(viewable_container_pks(user))),
        kind__in=["project", "folder"],
    )
    # Folders have no real status; keep them visible. Projects default
    # to "active" only so on_hold ("warte auf antwort"), completed, and
    # dropped don't clutter the overview.
    if not show_inactive:
        qs = qs.filter(Q(kind="folder") | Q(status="active"))
    # Hide projects deferred to a future date unless explicitly shown.
    now = datetime.now(timezone.utc)
    if not show_deferred:
        qs = qs.filter(Q(kind="folder") | Q(defer_date=None) | Q(defer_date__lte=now))
    projects = list(qs.order_by("+name"))

    # Group-by: when the user has a tag with children, projects can be
    # grouped by which child tag they carry. Pick the parent via
    # ``?group_by=<tag>``; if absent, fall back to a tag literally
    # named "Group" so the convention "create a 'Group' tag, drop
    # sub-tags into it" just works out of the box.
    requested_group_by = (request.args.get("group_by") or "").strip()
    group_parent = (requested_group_by
                    or ("Group" if user.tag_by_name("Group") else ""))
    groups = None
    if group_parent:
        # Dedupe: a stray duplicate Tag embedded doc with the same name
        # would otherwise produce two group headers with the same label.
        children_set: set = set()
        children: list[str] = []
        for t in (user.tags or []):
            if t.parent == group_parent and t.name and t.name not in children_set:
                children_set.add(t.name)
                children.append(t.name)
        children.sort(key=str.lower)
        if children:
            buckets: dict[str, list] = {c: [] for c in children}
            ungrouped: list = []
            seen_projects: set = set()
            for p in projects:
                if p.pk in seen_projects:
                    continue  # defensive: never list the same project twice
                seen_projects.add(p.pk)
                placed = False
                for child in children:
                    if child in (p.tags or []):
                        buckets[child].append(p)
                        placed = True
                        break
                if not placed:
                    ungrouped.append(p)
            groups = [(c, buckets[c]) for c in children if buckets[c]]
            if ungrouped:
                groups.append(("Ungrouped", ungrouped))
    # The tag-picker dropdown options: any tag with at least one child.
    group_options = sorted(
        {t.parent for t in (user.tags or []) if t.parent},
        key=str.lower,
    )
    return render_template(
        "projects/list.html",
        projects=_as_tree(projects),
        show_inactive=show_inactive,
        show_deferred=show_deferred,
        now=now,
        groups=groups,
        group_parent=group_parent,
        group_options=group_options,
    )

def _as_tree(projects):
    """Order projects depth-first so children sit right under their parent."""
    by_id = {str(p.pk): p for p in projects}
    children: dict[str | None, list] = {}
    for p in projects:
        key = str(p.parent.pk) if p.parent and str(p.parent.pk) in by_id else None
        children.setdefault(key, []).append(p)

    ordered: list[tuple[int, "Item"]] = []

    def walk(parent_key, depth):
        for p in sorted(children.get(parent_key, []), key=lambda x: x.name.lower()):
            ordered.append((depth, p))
            walk(str(p.pk), depth + 1)

    walk(None, 0)
    return ordered


def project_tree_for_sidebar(user):
    """Active project + folder tree, suitable for the project sidebar.

    Used by ``projects.index``, ``projects.detail``, the review view and
    perspective views — anywhere the user expects an OmniFocus-style
    project navigator on the left. Sections are inline structures
    inside a project, not navigable on their own, so we walk past them
    when computing the nesting key — a project whose parent is a
    section bubbles up under the section's owning project/folder.
    """
    from application.services import focus
    focus_pks = focus.focus_pks(user)
    fq = Q(pk__in=focus_pks) if focus_pks is not None else Q()
    from application.services.permissions import viewable_container_pks
    visible_pks = list(viewable_container_pks(user))
    proj_q = Item.objects(
        fq & Q(pk__in=visible_pks),
        kind__in=["project", "folder"],
        status="active",
        # Notes live in their own folder tree (folder_domain="notes") — keep
        # those folders out of the project navigator.
        folder_domain__ne="notes",
    )
    # Hide deferred projects from the navigator too (unless the user opted in).
    if not getattr(user, "show_deferred_projects", False):
        now = datetime.now(timezone.utc)
        proj_q = proj_q.filter(Q(kind="folder") | Q(defer_date=None) | Q(defer_date__lte=now))
    projects = list(proj_q.order_by("+name"))
    # Need sections to resolve "skip-past" parent links. Fetch them in
    # one query, then build a {section_pk: effective_parent_pk} map.
    sections = list(
        Item.objects(
            fq & Q(pk__in=visible_pks),
            kind="section",
        ).only("pk", "parent")
    )
    section_parent: dict = {}
    for s in sections:
        cur = s.parent
        # Walk through chained sections to the first non-section ancestor.
        seen = {str(s.pk)}
        while cur is not None and cur.kind == "section":
            if str(cur.pk) in seen:
                break
            seen.add(str(cur.pk))
            cur = cur.parent
        section_parent[str(s.pk)] = cur

    projects += _locked_nav_items(user)
    return _as_tree_skipping_sections(projects, section_parent)


def _locked_nav_items(user):
    """Override-locked containers (a child of a visible folder that the user
    can't open) flagged with ``_locked`` so the navigator shows them greyed and
    non-clickable rather than hiding them."""
    from application.services.permissions import locked_container_pks
    locked = locked_container_pks(user)
    if not locked:
        return []
    from bson import ObjectId
    items = list(Item.objects(
        pk__in=[ObjectId(p) for p in locked],
        kind__in=["project", "folder"],
        folder_domain__ne="notes",
    ))
    for it in items:
        it._locked = True
    return items


def _as_tree_skipping_sections(projects, section_parent):
    """Like :func:`_as_tree`, but treats a section parent as transparent.

    ``section_parent`` maps section pk → the section's effective
    (non-section) ancestor item. When a project's ``parent`` is a
    section, we hoist it under that ancestor instead.
    """
    by_id = {str(p.pk): p for p in projects}

    def effective_parent_pk(p):
        par = p.parent
        if par is None:
            return None
        if par.kind == "section":
            anc = section_parent.get(str(par.pk))
            return str(anc.pk) if anc is not None else None
        return str(par.pk)

    children: dict[str | None, list] = {}
    for p in projects:
        key = effective_parent_pk(p)
        if key not in by_id:
            key = None
        children.setdefault(key, []).append(p)

    ordered: list[tuple[int, "Item"]] = []

    def walk(parent_key, depth):
        for p in sorted(children.get(parent_key, []), key=lambda x: x.name.lower()):
            ordered.append((depth, p))
            walk(str(p.pk), depth + 1)

    walk(None, 0)
    return ordered

@projects_bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    user = current_user._get_current_object()
    form = ProjectForm()
    _populate_choices(form, user)

    # Pre-fill from URL hints: ?folder=1 / ?section=1&parent=<id> / ?kind=...
    from application.services.permissions import resolve_visible_item
    if request.method == "GET":
        if request.args.get("folder") == "1":
            form.kind.data = "folder"
        elif request.args.get("section") == "1":
            form.kind.data = "section"
        elif request.args.get("kind") in ("project", "folder", "section"):
            form.kind.data = request.args["kind"]
        parent_id = request.args.get("parent")
        # Prefill with any container the caller may edit — owned OR shared with
        # them (e.g. a team admin adding a folder inside a team-shared folder),
        # not only ones they own.
        if parent_id and resolve_visible_item(user, parent_id):
            form.parent.data = parent_id

    if form.validate_on_submit():
        # A section without a parent is a contradiction — flag and fall back
        # to a regular project so the user keeps their data.
        if form.kind.data == "section" and not form.parent.data:
            flash(
                "A section needs a parent project — saved as a regular project instead.",
                "warning",
            )
            form.kind.data = "project"
        # Free plan: hard-cap on project count (folders/sections excluded).
        from application.services.permissions import (
            FREE_PROJECT_LIMIT, at_project_limit,
        )
        if form.kind.data == "project" and at_project_limit(user):
            msg = (
                f"Free plan limit reached: {FREE_PROJECT_LIMIT} projects. "
                "Ask a global admin to upgrade your account."
            )
            # Inspector caller expects a fragment, not a full page. Drop
            # the message into the form's errors and re-render the
            # inspector body so the user stays in context — without
            # this the full /projects/new page would be injected and
            # the whole app shell appears nested.
            if _is_inspector():
                form.kind.errors = list(form.kind.errors or []) + [msg]
                return render_template(
                    "projects/_inspector.html",
                    form=form, project=None,
                    form_action=url_for("projects.create"),
                )
            flash(msg, "warning")
            return render_template(
                "projects/form.html", form=form, project=None,
            )
        p = Item(owner=user)
        _apply(form, p)
        p.save()
        if _is_inspector():
            return _render_project_inspector(p, user, just_saved=True)
        flash(f"{form.kind.data.title()} created.", "success")
        return redirect(url_for("projects.detail", project_id=str(p.pk)))
    ctx = {
        "form": form,
        "project": None,
        "form_action": url_for("projects.create"),
        # Keep the prefill across the inspector lifecycle by forwarding the
        # parent hint into the form action URL.
        "lock_parent": bool(request.args.get("parent")),
    }
    if _is_inspector():
        return render_template("projects/_inspector.html", **ctx)
    return render_template("projects/form.html", **ctx)

def _nest_subtasks(roots, collapsed_pks, show_completed):
    """Return ``roots`` plus their sub-task descendants in DFS (parent-before-
    children) order, each annotated with ``_depth`` / ``_has_children`` /
    ``_collapsed`` / ``_list_hidden`` — same collapse semantics as the main
    project list (sub-tasks collapsed by default; a pk in collapsed_items
    marks an EXPANDED parent). Lets tasks shown inside a sub-project's inline
    child list reveal their sub-tasks via the chevron too."""
    roots = list(roots)
    if not roots:
        return []
    # BFS down the task→task parent chain to gather every descendant sub-task.
    children_by_task: dict = {}
    frontier = [r.pk for r in roots]
    seen = set(frontier)
    status_filter = None if show_completed else ["done", "dropped"]
    while frontier:
        q = Item.objects(parent__in=frontier, kind="task")
        if status_filter:
            q = q.filter(status__nin=status_filter)
        nxt = []
        for t in q.order_by("+sort_order", "+created_at"):
            if t.pk in seen:
                continue
            seen.add(t.pk)
            children_by_task.setdefault(t.parent.pk, []).append(t)
            nxt.append(t.pk)
        frontier = nxt
    out: list = []

    def _dfs(task, depth, hidden):
        task._depth = depth
        has_kids = bool(children_by_task.get(task.pk))
        task._has_children = has_kids
        task._collapsed = has_kids and (str(task.pk) not in collapsed_pks)
        task._list_hidden = hidden
        out.append(task)
        for c in children_by_task.get(task.pk, []):
            _dfs(c, depth + 1, hidden or task._collapsed)
    for r in roots:
        _dfs(r, 0, False)
    return out

@projects_bp.route("/<project_id>")
@login_required
def detail(project_id):
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project:
        abort(404)
    if not project.can_be_viewed_by(user):
        abort(403)
    # All Items share one collection — a pk that points at a task
    # would otherwise render as an empty "project". Bounce the caller
    # to the right view so the URL never shows the same item twice.
    if project.kind == "task":
        return redirect(url_for("tasks.detail", task_id=project_id))

    grouping = (request.args.get("group") or project.task_grouping or "project").strip()
    if grouping in ("section", "subproject"):
        # Legacy stored prefs → the nested "Project" view.
        grouping = "project"
    show_completed = request.args.get("completed") == "1"
    view_mode = (request.args.get("view") or project.view_mode or "list").strip()
    if view_mode not in ("list", "kanban", "calendar"):
        view_mode = "list"
    # "Project" is the only grouping that keeps the nested section /
    # sub-project structure. Every other list grouping flattens ALL
    # descendant tasks into one place (grouped by the chosen criterion) so
    # nothing stays hidden inside a sub-project — the sub-project becomes
    # just a badge on the task row. Kanban / calendar keep their own
    # collection regardless of the grouping preference.
    flatten = grouping != "project" and view_mode == "list"

    sections = list(
        Item.objects(parent=project, kind="section").order_by("+name")
    )
    # Sub-projects (and folders, but not sections) — rendered as
    # task-like rows inside each section bucket. They live under the
    # project itself OR a specific section. Completed/dropped sub-projects
    # drop out of the plate just like done tasks do (``?completed=1``
    # opts them back in for review). Folders have no real status, so they
    # always stay.
    def _sub_query(parent):
        q = Item.objects(parent=parent, kind__in=["project", "folder"])
        if not show_completed:
            q = q.filter(Q(kind="folder") | Q(status__nin=["completed", "dropped"]))
        return q.order_by("+sort_order", "+name")

    sub_projects_all = list(_sub_query(project))
    for s in sections:
        sub_projects_all.extend(_sub_query(s))
    subprojects_by_section: dict = {None: []}
    for s in sections:
        subprojects_by_section[str(s.pk)] = []
    for sp in sub_projects_all:
        key = str(sp.parent.pk) if sp.parent and str(sp.parent.pk) != str(project.pk) else None
        subprojects_by_section.setdefault(key, []).append(sp)
    # Flat list still used by other call sites.
    sub_projects = sub_projects_all
    # Each sub-project's direct task children — rendered indented under
    # the sub-project row so the unit reads as one cohesive group.
    subproject_children: dict = {}
    if sub_projects_all:
        sp_pks = [sp.pk for sp in sub_projects_all]
        collapsed_pks = set(user.collapsed_items or [])
        # Mark each sub-project's collapse state for the toggle button.
        for sp in sub_projects_all:
            sp._collapsed = str(sp.pk) in collapsed_pks
        direct_by_sp: dict = {}
        kids_q = Item.objects(parent__in=sp_pks, kind="task")
        if not show_completed:
            kids_q = kids_q.filter(status__nin=["done", "dropped"])
        for kid in kids_q.order_by("+sort_order", "+created_at"):
            direct_by_sp.setdefault(kid.parent.pk, []).append(kid)
        # Each sub-project's direct tasks PLUS their sub-task trees nested
        # beneath, so a task inside a sub-project shows the expand chevron and
        # its (default-collapsed) sub-tasks — they were invisible before.
        for sp in sub_projects_all:
            roots = direct_by_sp.get(sp.pk) or []
            subproject_children[sp.pk] = _nest_subtasks(
                roots, collapsed_pks, show_completed)
            # _has_children = has direct kids (kid.parent dereferences to a
            # fresh instance, so writing in the loop above would miss the sp).
            sp._has_children = bool(roots)

    # Default: show only this project's own tasks (incl. sections —
    # sections are inline parts of the project). Sub-projects live on
    # their own pages and are listed as a tree at the top of this view.
    # ``?descendants=1`` opts into the rolled-up view.
    include_descendants = request.args.get("descendants") == "1"
    if flatten or include_descendants:
        project_ids = _descendant_ids(project)
    else:
        project_ids = [project.pk] + [s.pk for s in sections]

    # Iteratively walk the parent chain so sub-tasks (parent=task) and
    # their descendants are included alongside the direct children of
    # the project/section. Same indexed query each pass, bounded by
    # tree depth (usually a handful).
    all_tasks: list = []
    seen: set = set()
    frontier = list(project_ids)
    # Default: hide done / dropped — they belong on the Completed
    # perspective. ``?completed=1`` opts back in for project-level
    # review.
    task_status_filter = None if show_completed else ["done", "dropped"]
    while frontier:
        q = Item.objects(kind="task", parent__in=frontier)
        if task_status_filter:
            q = q.filter(status__nin=task_status_filter)
        batch = list(q.order_by("+sort_order", "+created_at"))
        next_frontier = []
        for t in batch:
            if t.pk in seen:
                continue
            seen.add(t.pk)
            all_tasks.append(t)
            next_frontier.append(t.pk)
        frontier = next_frontier
    # Dual-home: tasks delegated to the viewer that they filed under THIS
    # project (or one of its sections) via ``assignee_parent``. Their canonical
    # ``parent`` lives in the OWNER's tree, so the BFS above never reaches them
    # — pull them in explicitly for the assignee's view. ``assignee_parent`` is
    # always owned by the viewer, so this matches only the viewer's own filing.
    dh_q = Item.objects(kind="task", assigned_to=user,
                        assignee_parent__in=project_ids)
    if task_status_filter:
        dh_q = dh_q.filter(status__nin=task_status_filter)
    for t in dh_q.order_by("+sort_order", "+created_at"):
        if t.pk not in seen:
            seen.add(t.pk)
            all_tasks.append(t)
    # "Available only" (persisted per project): drop every blocked task —
    # deferred, ancestor-deferred, follow-up-pending or sequentially-blocked —
    # from the whole page (list / kanban / calendar) before grouping.
    if project.show_available_only:
        _av_now = datetime.now(timezone.utc)
        all_tasks = [t for t in all_tasks if t.is_available(_av_now)]
    grouping_tag = (project.task_grouping_tag or "").strip()
    subproject_groups: dict = {}
    flat_tag_view = False
    sequential = project.project_type == "sequential" and not project.show_blocked_tasks

    if flatten:
        # Flat / criterion grouping. Every descendant task is a single
        # depth-0 row, gathered into one bucket and split only by the
        # chosen criterion (or a flat list for "none"). No section headers,
        # no sub-project rows — the sub-project a task lives in shows only
        # as the badge on its row, so nothing hides inside a sub-project.
        # Sub-task nesting is dropped too: flat is really flat.
        for t in all_tasks:
            t._depth = 0
            t._has_children = False
            t._collapsed = False
        flat_bucket = _bucket_by_section(all_tasks, [])[None]
        flat_bucket["items"] = []
        flat_bucket["subprojects"] = []
        if grouping == "tag" and grouping_tag:
            flat_bucket["open_groups"] = _group_by_tag_specific(
                flat_bucket["open"], user, grouping_tag,
            )
        elif grouping in ("priority", "status", "tag", "due"):
            flat_bucket["open_groups"] = _group_tasks(flat_bucket["open"], grouping)
        else:  # "none"
            flat_bucket["open_groups"] = None

        # Full ABSOLUTE container path per task so a flattened row shows
        # exactly where the task lives — the whole "Folder / Project / Sub A /
        # Sub B" chain, not just the immediate parent. Resolved in-memory: one
        # query for the descendants plus the project's own ancestors, so the
        # walk reaches the library root.
        containers = Item.objects(
            pk__in=project_ids, kind__in=["project", "section", "folder"]
        ).only("pk", "title", "kind", "parent")
        node_by_pk = {str(c.pk): c for c in containers}
        anc, ga = project.parent, 0
        while anc is not None and ga < 50:   # project's ancestors (folders)
            ga += 1
            node_by_pk.setdefault(str(anc.pk), anc)
            anc = anc.parent
        # Tasks too, so a sub-task's chain resolves through its parent task
        # up to the nearest container.
        for t in all_tasks:
            node_by_pk.setdefault(str(t.pk), t)
        for t in all_tasks:
            crumbs: list = []
            nearest = None
            cur = t.parent
            seen_path: set = set()
            guard = 0
            while cur is not None and guard < 50:
                guard += 1
                cur_pk = str(cur.pk)
                if cur_pk in seen_path:
                    break
                seen_path.add(cur_pk)
                node = node_by_pk.get(cur_pk)
                if node is None:
                    break
                if node.kind in ("project", "section", "folder"):
                    crumbs.append(node.name)
                    if nearest is None:
                        nearest = node
                cur = node.parent
            crumbs.reverse()
            # Attach to the task object (like _depth / _collapsed) so it rides
            # through the task_groups macro's include — a render-context dict
            # doesn't reliably reach a template included inside a macro.
            t._badge = {
                "item": nearest or project,
                "label": " / ".join(crumbs) if crumbs else project.name,
            }
        section_buckets = {None: flat_bucket}
        # The structural data the nested view needs is irrelevant here.
        sections = []
        sub_projects = []
        subprojects_by_section = {None: []}
        subproject_children = {}
    else:
        # "Project" grouping — the ONLY nested view: sections as the outer
        # buckets, sub-projects as rows with their tasks beneath, sub-tasks
        # indented. This is the one place the project tree is shown.
        project_pks_set = {p for p in project_ids}
        # Group by the task's EFFECTIVE container for this viewer: a dual-home
        # task filed here (assignee_parent) must bucket under that container,
        # not under the owner's canonical parent (which isn't in this view) —
        # otherwise it'd be dropped as an orphan. ``home_for`` == ``parent``
        # for everyone but the assignee, so normal tasks are unaffected.
        def _eff_parent_pk(t):
            h = t.home_for(user)
            return h.pk if h is not None else None
        children_by_task: dict = {}
        for t in all_tasks:
            ep = _eff_parent_pk(t)
            if ep is not None and ep not in project_pks_set:
                children_by_task.setdefault(ep, []).append(t)
        # DFS: parents-first order so a task appears immediately before its
        # sub-tasks, each with a ``_depth`` for indentation. Only top-level
        # tasks survive in the bucket lists; descendants attach during DFS.
        top_level = [t for t in all_tasks
                      if _eff_parent_pk(t) in project_pks_set]
        collapsed_pks = set(user.collapsed_items or [])
        flat_ordered: list = []
        task_section_pk: dict = {}  # task.pk → section/project pk
        def _dfs(task, depth, section_pk, hidden):
            task._depth = depth
            has_kids = bool(children_by_task.get(task.pk))
            task._has_children = has_kids
            # Sub-tasks default to collapsed in the project LIST — they only
            # show once you expand the parent. A pk in collapsed_items marks
            # an EXPANDED parent here (inverted from sub-project rows, which
            # default to open). No children → no chevron, nothing to collapse.
            task._collapsed = has_kids and (str(task.pk) not in collapsed_pks)
            # ``_list_hidden`` rides down from a collapsed ancestor: the list
            # skips these rows (see _list_body.html), but they stay in the
            # buckets so kanban / calendar / Today still render them — a
            # sub-task you can schedule never vanishes from a planning view.
            task._list_hidden = hidden
            task_section_pk[task.pk] = section_pk
            flat_ordered.append(task)
            for c in children_by_task.get(task.pk, []):
                _dfs(c, depth + 1, section_pk, hidden or task._collapsed)
        for t in top_level:
            _dfs(t, 0, _eff_parent_pk(t), False)
        all_tasks = flat_ordered

        section_buckets = _bucket_by_section(all_tasks, sections, task_section_pk)
        sections_by_pk = {str(s.pk): s for s in sections}

        # Stitch sub-projects into each container's bucket so they share the
        # sort space with tasks. ``open`` stays task-only; sub-project rows
        # render flat (no secondary grouping — Project view shows structure,
        # not criterion sub-groups).
        for key, bucket in section_buckets.items():
            subs = subprojects_by_section.get(key) or []
            merged = (
                [("p", sp, sp.sort_order or 0) for sp in subs] +
                [("t", t, t.sort_order or 0) for t in bucket["open"]]
            )
            merged.sort(key=lambda x: x[2])
            bucket["items"] = [(kind, obj) for kind, obj, _ in merged]
            bucket["subprojects"] = subs
            bucket["open_groups"] = None

        # Per-container sequential collapse: each section decides
        # independently. show_blocked_tasks turns it off so blocked tasks
        # render greyed out (template handles the .blocked class).
        for key, bucket in section_buckets.items():
            container = project if key is None else sections_by_pk.get(key)
            if not container:
                continue
            if (container.project_type == "sequential"
                    and not container.show_blocked_tasks):
                # Only the first live item (task OR sub-project) in sort
                # order stays visible. Done/dropped sub-projects don't block.
                def _live(item):
                    kind, obj = item
                    if kind == "t":
                        return True
                    return obj.status not in ("completed", "dropped")
                live_items = [it for it in bucket["items"] if _live(it)]
                keep = live_items[:1]
                keep_task_pks = {obj.pk for kind, obj in keep if kind == "t"}
                keep_sp_pks = {obj.pk for kind, obj in keep if kind == "p"}
                bucket["open"] = [t for t in bucket["open"] if t.pk in keep_task_pks]
                bucket["subprojects"] = [sp for sp in bucket["subprojects"]
                                          if sp.pk in keep_sp_pks]
                bucket["items"] = keep

    # Build calendar buckets if needed (current month grid with tasks by day).
    calendar_data = None
    if view_mode == "calendar":
        from application.services.tzutil import resolve_tz
        cal_tz = resolve_tz(user)
        calendar_data = _build_calendar(all_tasks, request.args.get("month"), cal_tz)

    # Notes linked to this project (symmetric back-link from the note side).
    linked_notes = [r for r in project.related_items() if r.kind == "note"]
    editable_note_ids = {n.pk for n in linked_notes if n.can_be_edited_by(user)}

    return render_template(
        "projects/detail.html",
        project=project,
        linked_notes=linked_notes,
        editable_note_ids=editable_note_ids,
        sections=sections,
        sub_projects=sub_projects,
        subprojects_by_section=subprojects_by_section,
        subproject_children=subproject_children,
        subproject_groups=subproject_groups,
        section_buckets=section_buckets,
        flat_tag_view=flat_tag_view,
        flatten=flatten,
        grouping=grouping,
        show_completed=show_completed,
        include_descendants=include_descendants,
        sequential=sequential,
        ancestors=_ancestor_chain(project),
        view_mode=view_mode,
        calendar=calendar_data,
        all_tasks=all_tasks,
        comment_form=CommentForm(),
    )

def _build_calendar(tasks, month_param, tz=timezone.utc):
    """Compute a month grid (list of weeks; each week is 7 days, each day
    is ``{"date": date, "in_month": bool, "tasks": [...]}``).

    Tasks are bucketed by their due/plan date **in the user's timezone** —
    a task planned for midnight Berlin is 22:00 UTC the day before, so
    bucketing by the raw UTC date would drop it onto the previous day."""
    from calendar import Calendar
    from datetime import date as _date

    def _local_date(dt):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).date()

    today = datetime.now(tz).date()
    if month_param:
        try:
            y, m = map(int, month_param.split("-"))
            cursor = _date(y, m, 1)
        except (ValueError, TypeError):
            cursor = _date(today.year, today.month, 1)
    else:
        cursor = _date(today.year, today.month, 1)

    by_day: dict = {}
    no_date: list = []
    for t in tasks:
        if t.due_date:
            by_day.setdefault(_local_date(t.due_date), []).append(t)
        elif t.plan_date:
            by_day.setdefault(_local_date(t.plan_date), []).append(t)
        else:
            no_date.append(t)

    cal = Calendar(firstweekday=0)  # Monday
    weeks = []
    for week in cal.monthdatescalendar(cursor.year, cursor.month):
        weeks.append([
            {
                "date": d,
                "in_month": d.month == cursor.month,
                "is_today": d == today,
                "tasks": by_day.get(d, []),
            }
            for d in week
        ])

    # prev / next month for navigation
    prev_m = (cursor.replace(day=1) - timedelta(days=1)).replace(day=1)
    next_m = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

    return {
        "month": cursor,
        "weeks": weeks,
        "no_date": no_date,
        "prev": f"{prev_m.year}-{prev_m.month:02d}",
        "next": f"{next_m.year}-{next_m.month:02d}",
    }

def _ancestor_chain(project: Item, viewer=None) -> list[Item]:
    """Return the item's ancestors from root → direct container.

    The detail page renders this above the title as a clickable breadcrumb.
    When ``viewer`` is given, the chain starts at the item's home FOR that
    viewer (``home_for``) — so an assignee who filed a delegated task sees
    THEIR own project path (or Inbox), never the owner's invisible chain that
    would 404 on click. Cycles are stopped at 10 hops just in case.
    """
    chain: list[Item] = []
    seen: set = set()
    cur = project.home_for(viewer) if viewer is not None else project.parent
    while cur and cur.pk not in seen and len(chain) < 10:
        chain.append(cur)
        seen.add(cur.pk)
        cur = cur.parent
    return list(reversed(chain))

def _bucket_by_section(all_tasks, sections, section_pk_by_task=None):
    """Section-aware grouping with status sub-buckets per section.

    ``section_pk_by_task`` (optional) maps task.pk → the section/project
    pk that owns it via the project subtree. Used for sub-tasks whose
    ``parent`` is another task — they should bucket with their top-level
    ancestor, not under "no section".
    """
    out = {None: {"open": [], "someday": [], "done": [], "dropped": []}}
    for s in sections:
        out[str(s.pk)] = {"open": [], "someday": [], "done": [], "dropped": []}

    def status_bucket(t):
        if t.status == "done":
            return "done"
        if t.status == "dropped":
            return "dropped"
        if t.status == "someday":
            return "someday"
        return "open"

    for t in all_tasks:
        sid = None
        if section_pk_by_task and t.pk in section_pk_by_task:
            sid = str(section_pk_by_task[t.pk])
        elif t.parent:
            sid = str(t.parent.pk)
        key = sid if sid in out else None
        out[key][status_bucket(t)].append(t)
    return out

def _group_tasks(tasks, mode, keep_empty=False):
    """Return ``(label, icon, [tasks], target_project_id)`` buckets.

    ``target_project_id`` is what the drop container exposes via
    ``data-target-project``:
      * a project pk → dropping there sets the task's project to that pk
      * ``None`` → drops only reorder, never reparent (priority / status /
        tag / due / no-grouping)

    ``keep_empty`` keeps every bucket even when it has no tasks — used by the
    Today "group by priority" view so each priority level stays on-screen as a
    drop target (drag a task onto an empty P1 to promote it).
    """
    from datetime import datetime, timedelta, timezone

    if mode == "priority":
        order = [(1, "P1", "bi-flag-fill text-danger"),
                 (2, "P2", "bi-flag-fill text-warning"),
                 (3, "P3", "bi-flag text-info"),
                 (4, "P4", "bi-flag text-muted"),
                 (None, "No priority", "bi-dash-circle text-muted")]
        buckets = {p: [] for p, _, _ in order}
        for t in tasks:
            key = t.priority if t.priority in buckets else None
            buckets[key].append(t)
        return [(label, icon, buckets[p], None)
                for p, label, icon in order if buckets[p] or keep_empty]

    if mode == "status":
        order = [
            ("open", "Open", "bi-circle"),
            ("next", "Next", "bi-arrow-right-circle"),
            ("waiting", "Waiting", "bi-hourglass-split"),
            ("waiting_reply", "Waiting on reply", "bi-reply"),
            ("scheduled", "Scheduled", "bi-calendar-event"),
            ("someday", "Someday", "bi-cloud"),
            ("done", "Done", "bi-check2-all"),
            ("dropped", "Dropped", "bi-x-circle"),
        ]
        buckets = {s: [] for s, _, _ in order}
        for t in tasks:
            buckets.setdefault(t.status, []).append(t)
        return [(label, icon, buckets[s], None) for s, label, icon in order if buckets[s]]

    if mode == "tag":
        # tasks.tags is now a list of tag NAMES (strings)
        by_tag: dict = {}
        untagged: list = []
        for t in tasks:
            if not t.tags:
                untagged.append(t)
                continue
            for tag_name in t.tags:
                entry = by_tag.setdefault(tag_name, (tag_name, []))
                entry[1].append(t)
        groups = [(name, "bi-tag", lst, None)
                  for name, lst in sorted(by_tag.values(), key=lambda x: x[0].lower())]
        if untagged:
            groups.append(("Untagged", "bi-dash", untagged, None))
        return groups

    if mode == "due":
        now = datetime.now(timezone.utc)
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        week_end = today_end + timedelta(days=6)
        buckets = {
            "overdue": [], "today": [], "this_week": [],
            "later": [], "no_date": [],
        }
        for t in tasks:
            if not t.due_date:
                buckets["no_date"].append(t)
            elif t.due_date < now:
                buckets["overdue"].append(t)
            elif t.due_date <= today_end:
                buckets["today"].append(t)
            elif t.due_date <= week_end:
                buckets["this_week"].append(t)
            else:
                buckets["later"].append(t)
        order = [
            ("overdue", "Overdue", "bi-exclamation-triangle text-danger"),
            ("today", "Today", "bi-calendar-day"),
            ("this_week", "This week", "bi-calendar-week"),
            ("later", "Later", "bi-calendar-plus"),
            ("no_date", "No due date", "bi-dash-circle text-muted"),
        ]
        return [(label, icon, buckets[k], None) for k, label, icon in order if buckets[k]]

    # mode == "none" — flat
    return [("All tasks", "bi-list-ul", tasks, None)] if tasks else []

def _group_by_subproject(tasks, root, descendants):
    """Group tasks by which project/section/sub-project they live in.

    Returns 4-tuples ``(label, icon, tasks, target_project_id)`` where
    ``target_project_id`` is the pk of the sub-project (or root) the group
    represents — drag-and-drop into the group sets the task's project to
    that pk.
    """
    by_id = {str(p.pk): p for p in descendants}

    def path_label(p):
        parts: list[str] = []
        cur = p
        while cur is not None and str(cur.pk) != str(root.pk):
            parts.append(cur.name)
            cur = cur.parent
        return " / ".join(reversed(parts)) if parts else root.name

    def icon_for(p):
        if p.is_section:
            return "bi-bookmark text-info"
        if p.is_folder:
            return "bi-folder2-open text-warning"
        return "bi-folder"

    buckets: dict = {}
    for t in tasks:
        pid = str(t.project.pk) if t.project else None
        buckets.setdefault(pid, []).append(t)

    out: list = []
    for pid, lst in buckets.items():
        if pid and pid in by_id:
            p = by_id[pid]
            out.append((path_label(p), icon_for(p), lst, pid))
        else:
            # Tasks loose under root, or under an unknown project — drop
            # target is the root project itself.
            out.append((root.name, "bi-folder-fill text-warning", lst, str(root.pk)))
    out.sort(key=lambda x: (x[0] == root.name, x[0].lower()))
    return out

def _descendant_ids(project):
    """Return the project's id plus all transitive descendant ids."""
    out = [project.pk]
    frontier = [project]
    while frontier:
        current = frontier.pop()
        children = list(Item.objects(parent=current))
        for c in children:
            out.append(c.pk)
            frontier.append(c)
    return out

def _group_by_tag_specific(tasks, user, target):
    """Bucket ``tasks`` for the "focused tag" grouping.

    If the target has children, one bucket per child (plus
    ``Just @target`` for the parent-only and ``Without`` for the rest).
    Otherwise a simple with / without split.
    """
    if not target:
        return []
    children = sorted(
        (t.name for t in (user.tags or []) if t.parent == target),
        key=str.lower,
    )
    out = []
    if children:
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
    has = [t for t in tasks if target in (t.tags or [])]
    nope = [t for t in tasks if target not in (t.tags or [])]
    out = []
    if has:
        out.append((f"With @{target}", "bi-tag-fill", has, None))
    if nope:
        out.append(("Without", "bi-dash", nope, None))
    return out

def _subproject_tree(project):
    """DFS list of ``(depth, project)`` for the sub-projects beneath ``project``.

    Sections are excluded — they belong inline in the parent project, not
    in the sub-project listing.
    """
    out: list = []

    def walk(parent, depth):
        children = list(
            Item.objects(parent=parent, kind__in=["project", "folder"])
                .order_by("+name")
        )
        for c in children:
            out.append((depth, c))
            walk(c, depth + 1)

    walk(project, 0)
    return out

@projects_bp.route("/<project_id>/edit", methods=["GET", "POST"])
@login_required
def edit(project_id):
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project:
        abort(404)
    # Anyone who may edit it (owner, shared member, or a team admin over the
    # subtree) can open + save the inspector — not only the owner. Mirrors the
    # detail view's visibility gate; save() never reassigns ``owner``.
    if not project.can_be_edited_by(user):
        abort(403)
    form = ProjectForm(obj=project)
    _populate_choices(form, user, project)
    if form.is_submitted() is False:
        from application.services.tz import localise_form_dates
        localise_form_dates(form, user)
        form.parent.data = str(project.parent.pk) if project.parent else ""
        form.members.data = [str(m.pk) for m in (project.members or [])]
        form.shared_teams.data = [str(t.pk) for t in (project.shared_teams or [])]
        # ``kind`` isn't a model attribute — it's a UI abstraction over
        # ``is_folder`` / ``is_section``. Derive it explicitly so the
        # dropdown reflects the project's actual type. Without this the
        # field defaults to "project" and a save would silently flip a
        # section into a regular project.
        form.kind.data = _kind_of(project)
    if form.validate_on_submit():
        _apply(form, project)
        project.save()
        if _is_inspector():
            return _render_project_inspector(project, user, just_saved=True)
        flash("Item updated.", "success")
        return redirect(url_for("projects.detail", project_id=str(project.pk)))
    ctx = {
        "form": form,
        "project": project,
        "form_action": url_for("projects.edit", project_id=str(project.pk)),
    }
    if _is_inspector():
        return render_template("projects/_inspector.html", **ctx)
    return render_template("projects/form.html", **ctx)

@projects_bp.route("/<project_id>/complete", methods=["POST"])
@login_required
def complete(project_id):
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project or project.owner != user:
        abort(404)
    project.status = "completed"
    project.save()
    return redirect(url_for("projects.detail", project_id=project_id))

@projects_bp.route("/<project_id>/show-blocked", methods=["POST"])
@login_required
def toggle_show_blocked(project_id):
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project:
        abort(404)
    if not project.can_be_viewed_by(user):
        abort(403)
    project.show_blocked_tasks = not project.show_blocked_tasks
    project.save()
    # Stay on the page that fired the toggle — so toggling a section
    # from its parent project view doesn't navigate INTO the section.
    return redirect(
        request.referrer or url_for("projects.detail", project_id=project_id)
    )

@projects_bp.route("/<project_id>/available-only", methods=["POST"])
@login_required
def toggle_available_only(project_id):
    """Per-project 'available only' filter — persisted on the project so it
    sticks across reloads/devices. Works for ANY project (not just
    sequential ones): when on, the page hides blocked tasks."""
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project:
        abort(404)
    if not project.can_be_viewed_by(user):
        abort(403)
    project.show_available_only = not project.show_available_only
    project.save()
    # Stay on the page that fired the toggle (keeps ?group/?completed).
    return redirect(
        request.referrer or url_for("projects.detail", project_id=project_id)
    )

@projects_bp.route("/<project_id>/view-mode", methods=["POST"])
@login_required
def set_view_mode(project_id):
    VIEW_MODES = ("list", "kanban", "calendar")

    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project:
        abort(404)
    if not project.can_be_viewed_by(user):
        abort(403)
    mode = (request.form.get("view") or "").strip()
    if mode not in VIEW_MODES:
        flash("Invalid view.", "danger")
    else:
        project.view_mode = mode
        project.save()
    return redirect(url_for("projects.detail", project_id=project_id))

@projects_bp.route("/<project_id>/share", methods=["POST"])
@login_required
def share_toggle(project_id):
    """Enable/disable the public share link; optionally regenerate token."""
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id, owner=user).first()
    if not project:
        abort(404)
    action = request.form.get("action") or "toggle"
    if action == "regenerate":
        project.regenerate_share_token()
        project.share_enabled = True
        flash("New public link generated.", "success")
    elif action == "disable":
        project.share_enabled = False
        flash("Public link disabled.", "info")
    else:  # enable
        if not project.share_token:
            project.regenerate_share_token()
        project.share_enabled = True
        flash("Public link enabled.", "success")
    project.save()
    return redirect(url_for("projects.detail", project_id=project_id))

@projects_bp.route("/<project_id>/rename", methods=["POST"])
@login_required
def rename(project_id):
    """Quick inline rename used by the drag-merge UX."""
    from flask import jsonify
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id, owner=user).first()
    if not project:
        return jsonify({"error": "not found"}), 404
    name = (request.form.get("name") or "").strip()[:200]
    if not name:
        return jsonify({"error": "empty"}), 400
    project.name = name
    project.save()
    return jsonify({"ok": True, "name": name})

@projects_bp.route("/<project_id>/note", methods=["POST"])
@login_required
def update_note(project_id):
    """Inline create/edit of a project's note — no inspector round-trip.
    Returns the rendered markdown so the caller can repaint the note card."""
    from flask import jsonify, current_app
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id, owner=user).first()
    if not project:
        return jsonify({"error": "not found"}), 404
    note = (request.form.get("note") or "").strip()
    project.note = note
    project.save()
    html = current_app.jinja_env.filters["markdown"](note) if note else ""
    return jsonify({"ok": True, "note": note, "html": html})

@projects_bp.route("/<project_id>/move", methods=["POST"])
@login_required
def move(project_id):
    """Reparent + reorder a sub-project — drag-drop entry point.

    Body:
      parent_id    — destination container's pk (empty = top-level)
      sp_siblings[] — sub-project pks in their new order within the
                     destination container; used to renumber sort_order
                     so the order survives a reload.
    """
    from flask import jsonify
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id, owner=user).first()
    if not project:
        return jsonify({"error": "not found"}), 404
    raw_parent = (request.form.get("parent_id") or "").strip()
    if not raw_parent:
        project.parent = None
    else:
        new_parent = Item.objects(pk=raw_parent, owner=user).first()
        # A project's valid parents are project/folder only (per the Item
        # docstring). Sections are inline groupings for tasks — letting
        # a project drop in there breaks the project tree (the sidebar
        # then has to walk past sections, and the project shows in
        # weird places). Reject early instead of fixing it up silently.
        if new_parent and new_parent.kind == "section":
            return jsonify({"error": "projects can't live inside a section"}), 400
        if not new_parent:
            return jsonify({"error": "invalid parent"}), 400
        # Reject cycles: walk new_parent's chain, project must not appear.
        cur = new_parent
        depth = 0
        while cur and depth < 20:
            if cur.pk == project.pk:
                return jsonify({"error": "would create a cycle"}), 400
            cur = cur.parent
            depth += 1
        project.parent = new_parent
    project.save()

    # Renumber sort_order across the destination container's sub-projects.
    sibling_ids = (request.form.getlist("sp_siblings[]")
                   or request.form.getlist("sp_siblings"))
    if sibling_ids:
        siblings = Item.objects(pk__in=sibling_ids, owner=user)
        by_id = {str(s.pk): s for s in siblings}
        for idx, pk in enumerate(sibling_ids):
            sib = by_id.get(pk)
            if sib and sib.sort_order != idx:
                sib.sort_order = idx
                sib.save()
    return jsonify({"ok": True})

@projects_bp.route("/<project_id>/grouping", methods=["POST"])
@login_required
def set_grouping(project_id):
    TASK_GROUPINGS = ("project", "priority", "status", "tag", "due", "none")

    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project:
        abort(404)
    if not project.can_be_viewed_by(user):
        abort(403)
    mode = (request.form.get("group") or "").strip()
    tag = (request.form.get("grouping_tag") or "").strip()
    if mode and mode not in TASK_GROUPINGS:
        flash("Invalid grouping.", "danger")
    else:
        if mode:
            project.task_grouping = mode
        # Persist the focused tag whenever the form sent one (the toolbar
        # dropdown does). Mode != tag clears it so it doesn't linger.
        if "grouping_tag" in request.form:
            project.task_grouping_tag = tag if project.task_grouping == "tag" else ""
        project.save()
    # Don't echo the referrer — its old ``?group=...`` would override the
    # freshly-saved value. Land on the canonical detail URL.
    return redirect(url_for("projects.detail", project_id=project_id))

@projects_bp.route("/<project_id>/convert-to-task", methods=["POST"])
@login_required
def convert_to_task(project_id):
    """Demote a project to a task. Children stay attached — they become
    sub-tasks of the new task (the unified model allows arbitrary
    nesting).

    Status mapping translates the project state into a comparable
    task status so the converted item lands in a sensible perspective:

        active     → open      (actionable — shows in Inbox / Next)
        on_hold    → waiting   (so it doesn't vanish into Dropped)
        completed  → done
        dropped    → dropped

    Anything else (legacy / unknown) falls back to ``open`` rather
    than being silently dropped.

    XHR callers (tree page's one-click convert) and the inspector get a
    JSON ack instead of a redirect, so the caller stays in the current
    view and repaints in place rather than jumping to the new task.
    """
    from flask import jsonify
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project or project.owner != user:
        abort(404)
    status_map = {
        "active": "open",
        "on_hold": "waiting",
        "completed": "done",
        "dropped": "dropped",
    }
    # Demoting to a task means it can no longer own a project's structure:
    # promote the direct children up one level so they sit *beside* the new
    # task (same level), not nested under it. Deeper descendants ride along
    # with each child untouched.
    Item.objects(parent=project).update(set__parent=project.parent)
    project.kind = "task"
    project.status = status_map.get(project.status, "open")
    # ``completed_at`` only makes sense on done/dropped tasks — clear
    # it so a converted active project doesn't show as already-done
    # because of a stale field.
    if project.status not in ("done", "dropped"):
        project.completed_at = None
    project.save()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" \
            or request.headers.get("X-Inspector-Request") == "1":
        return jsonify({"ok": True, "task_id": str(project.pk)})
    flash(f"Converted project into task: {project.title}", "success")
    return redirect(url_for("tasks.detail", task_id=str(project.pk)))

@projects_bp.route("/<section_id>/convert-to-project", methods=["POST"])
@login_required
def convert_section_to_project(section_id):
    """Promote a section into a standalone project — a section already
    holds child tasks, and so does a project, so the children carry over
    untouched. The section's parent is a project (per the Item docstring),
    which is a valid project parent, so it simply becomes a sub-project
    there. Mirrors ``tasks.convert_to_project``: pick sensible project
    defaults that a section never carried.
    """
    from flask import jsonify
    user = current_user._get_current_object()
    section = Item.objects(pk=section_id).first()
    if not section or section.owner != user:
        abort(404)
    section.kind = "project"
    section.project_type = section.project_type or "parallel"
    section.status = "active"
    # Sections are created with review_interval_days=0 (never reviewed);
    # give the new project the standard cadence.
    section.review_interval_days = section.review_interval_days or 7
    section.save()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "project_id": str(section.pk)})
    flash(f"Converted section into project: {section.title}", "success")
    return redirect(url_for("projects.detail", project_id=str(section.pk)))

@projects_bp.route("/<project_id>/sections/new", methods=["POST"])
@login_required
def add_section(project_id):
    """Create a section under ``project_id``.

    XHR callers (the inline "press s" flow on the detail page) get a JSON
    ack instead of a redirect, so the page can repaint in place. They may
    omit the name — an inline-created section starts as "New section" and
    is renamed in place via ``projects.rename``.
    """
    from flask import jsonify
    user = current_user._get_current_object()
    parent = Item.objects(pk=project_id).first()
    if not parent or not parent.can_be_viewed_by(user):
        abort(404)
    is_xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    name = (request.form.get("name") or "").strip()
    if not name:
        if is_xhr:
            name = "New section"
        else:
            flash("Section name is required.", "danger")
            return redirect(url_for("projects.detail", project_id=project_id))
    section = Item(
        owner=user,
        title=name,
        project_type="parallel",
        status="active",
        parent=parent,
        kind="section",
        review_interval_days=0,
    )
    section.save()
    if is_xhr:
        return jsonify({"ok": True, "id": str(section.pk), "name": section.name})
    flash(f"Section '{name}' added.", "success")
    return redirect(url_for("projects.detail", project_id=project_id))

@projects_bp.route("/quick-create", methods=["POST"])
@login_required
def quick_create():
    """Create a bare project or folder from just a name — backs the inline
    "+" create on the Projects page (no inspector round-trip). Returns JSON
    so the caller can refresh in place. Honours the free-plan project cap;
    folders don't count against it.
    """
    from flask import jsonify
    user = current_user._get_current_object()
    kind = (request.form.get("kind") or "project").strip()
    if kind not in ("project", "folder"):
        return jsonify({"error": "invalid kind"}), 400
    name = (request.form.get("name") or "").strip()[:200]
    if not name:
        return jsonify({"error": "empty name"}), 400
    if kind == "project":
        from application.services.permissions import (
            FREE_PROJECT_LIMIT, at_project_limit,
        )
        if at_project_limit(user):
            return jsonify({
                "error": f"Free plan limit reached: {FREE_PROJECT_LIMIT} projects."
            }), 403
    # ``status="active"`` matters for both kinds: the sidebar tree only
    # lists active projects/folders, so a folder left at the default
    # "open" would be created but never show up (looks like it vanished).
    item = Item(owner=user, title=name, kind=kind, status="active")
    if kind == "project":
        item.project_type = "parallel"
        item.review_interval_days = 7
    parent_id = (request.form.get("parent") or "").strip()
    if parent_id:
        parent = Item.objects(pk=parent_id, owner=user).first()
        if parent:
            item.parent = parent
    item.save()
    return jsonify({"ok": True, "id": str(item.pk), "name": item.name, "kind": kind})

@projects_bp.route("/bulk", methods=["POST"])
@login_required
def bulk():
    user = current_user._get_current_object()
    ids = request.form.getlist("project_ids[]") or request.form.getlist("project_ids")
    action = (request.form.get("action") or "").strip()
    if not ids:
        flash("Nothing selected.", "warning")
        return redirect(request.referrer or url_for("projects.index"))

    projects = list(Item.objects(pk__in=ids, owner=user))
    skipped = len(ids) - len(projects)
    count = 0

    if action == "delete":
        from application.services.trash import trash_item
        for p in projects:
            trash_item(p)
            count += 1
    elif action == "complete":
        for p in projects:
            p.status = "completed"
            p.save()
            count += 1
    elif action == "archive":
        for p in projects:
            p.status = "dropped"
            p.save()
            count += 1
    elif action == "set_review":
        try:
            days = int(request.form.get("review_interval_days") or 0)
        except ValueError:
            flash("Invalid review interval.", "danger")
            return redirect(request.referrer or url_for("projects.index"))
        if days < 0:
            days = 0
        for p in projects:
            p.review_interval_days = days
            # Re-arm the next-review clock immediately so the change shows
            # up in the Review perspective right away.
            from datetime import datetime, timedelta, timezone
            if days:
                p.next_review_at = datetime.now(timezone.utc) + timedelta(days=days)
            else:
                p.next_review_at = None
            p.save()
            count += 1
    else:
        flash(f"Unknown action: {action}", "danger")
        return redirect(request.referrer or url_for("projects.index"))

    msg = f"{action.title()}: {count} project(s)"
    if skipped:
        msg += f" — {skipped} skipped (not owner)"
    flash(msg, "success" if count else "warning")
    return redirect(request.referrer or url_for("projects.index"))

@projects_bp.route("/<project_id>/import-csv", methods=["GET", "POST"])
@login_required
def import_csv(project_id):
    """Upload a CSV and turn its rows into tasks under this project.

    Mode ``import`` (default) always creates fresh tasks. Mode ``sync``
    keys rows by the optional ``csv_id`` column — matching tasks are
    updated rather than duplicated, and tasks absent from the CSV can
    be archived.

    Recognised columns (header row, case-insensitive):
      title (required), note, status, priority, flagged, due, tags,
      csv_id.
    """
    import csv as _csv
    from io import StringIO

    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project or project.owner != user:
        abort(404)

    if request.method == "GET":
        return render_template(
            "projects/import_csv.html", project=project,
            inherited_jira_url=project.effective_jira_url(),
        )

    fileobj = request.files.get("file")
    if not fileobj or not fileobj.filename:
        flash("No file uploaded.", "danger")
        return redirect(url_for("projects.import_csv", project_id=project_id))

    raw = fileobj.read().decode("utf-8-sig", errors="replace")
    mode = (request.form.get("mode") or "sync").strip()
    archive_missing = bool(request.form.get("archive_missing"))
    jira_url = (request.form.get("jira_url") or "").strip().rstrip("/")
    # Remember the choices on the project so the form pre-fills them next
    # time, and so Jira ticket numbers stay clickable across re-imports.
    project.csv_import_mode = mode
    project.csv_archive_missing = archive_missing
    project.jira_url = jira_url  # "" = inherit from the parent folder
    project.save()
    # The URL actually used for links: the project's own override, else the
    # nearest ancestor folder's (set once per folder, inherited here).
    effective_jira_url = project.effective_jira_url()

    # Auto-detect the delimiter — Jira CSVs in EU locales use ``;``
    # instead of ``,``. Sniffer picks one based on the first 4 KB.
    sample = raw[:4096]
    try:
        sniffed = _csv.Sniffer().sniff(sample, delimiters=",;\t|")
        # The Sniffer often guesses ``doublequote=False`` on Jira-style
        # exports (where ``""`` inside a quoted field is the standard
        # escape). That mis-parses multi-line cells into dozens of
        # bogus rows. We trust the Sniffer for the delimiter only,
        # otherwise use ``csv.excel``'s well-formed defaults.
        class _D(_csv.excel):
            delimiter = sniffed.delimiter
        dialect = _D
    except _csv.Error:
        dialect = _csv.excel  # default to comma

    # Parse the CSV ourselves rather than via DictReader so we can keep
    # *every* occurrence of a duplicated column. Jira exports Labels and
    # Comments as several columns with the same header — DictReader's
    # last-wins behaviour would silently drop all but one.
    raw_reader = _csv.reader(StringIO(raw), dialect)
    # Normalise headers: lowercase, trim whitespace, and drop trailing
    # punctuation (Jira's German export writes "Vorgangsschlüssel:" — the
    # colon is part of the column name and would otherwise break match).
    def _norm_header(h):
        return (h or "").strip().lower().rstrip(":").strip()
    try:
        headers = [_norm_header(h) for h in next(raw_reader)]
    except StopIteration:
        flash("Empty CSV.", "danger")
        return redirect(url_for("projects.import_csv", project_id=project_id))
    if not any(h for h in headers):
        flash("CSV has no header row.", "danger")
        return redirect(url_for("projects.import_csv", project_id=project_id))

    # Materialise the body so we can both heuristically pick columns
    # and iterate again to import. 100s of rows is fine in memory.
    body = [row for row in raw_reader]

    is_jira = any(h in headers for h in (
        "issue key", "summary", "vorgangsschlüssel", "zusammenfassung",
    ))
    source_tag = "jira" if is_jira else "csv"

    # Detect index lists for multi-value columns up front.
    label_idx = [i for i, h in enumerate(headers)
                 if h in ("labels", "tags", "stichwörter", "schlagworte")]
    comment_idx = [i for i, h in enumerate(headers)
                   if h in ("comment", "comments", "kommentar", "kommentare")]

    # Auto-pick a title column if none of the known aliases is in the
    # header row. Score each column over the (already-loaded) body:
    # +1 per non-empty cell that looks like a title (5..200 chars, not
    # pure digits), +1 if it contains a space (real prose), and a
    # uniqueness bonus so we pick the longish, mostly-distinct column.
    title_aliases = ("title", "name", "summary", "subject",
                     "zusammenfassung", "betreff")
    auto_title_idx = None
    if not any(a in headers for a in title_aliases):
        scores = [0] * len(headers)
        seen: list[set] = [set() for _ in headers]
        for r in body[:200]:
            for i, v in enumerate(r[:len(headers)]):
                v = (v or "").strip()
                if not v:
                    continue
                seen[i].add(v)
                n = len(v)
                if n < 5 or n > 200 or v.isdigit():
                    continue
                scores[i] += 2 if " " in v else 1
        # Multiply by uniqueness fraction so a high-cardinality column
        # (the real title) beats a low-cardinality one (status etc.)
        adjusted = [
            int(s * (len(seen[i]) / max(1, sum(1 for r in body if i < len(r) and r[i].strip()))))
            if s else 0
            for i, s in enumerate(scores)
        ]
        if any(adjusted):
            auto_title_idx = adjusted.index(max(adjusted))

    def _row_get(values, *keys):
        """First non-empty value matching any of the given header names."""
        for k in keys:
            for i, h in enumerate(headers):
                if h == k and i < len(values) and (values[i] or "").strip():
                    return values[i].strip()
        return ""

    # Index existing items by csv_id — applied in both ``import`` and
    # ``sync`` mode, so re-importing the same file always updates the
    # matching rows instead of creating duplicates. Scope is all of
    # the user's items (not just this project), so a row whose task
    # has since been moved to another project still matches. Include
    # projects too: the relationship-wiring pass below promotes a
    # task with children to a project, and we still want the next
    # sync to find and update it instead of duplicating.
    existing_by_csv_id: dict = {}
    for t in Item.objects(owner=user,
                          kind__in=["task", "project", "section"],
                          external_id__startswith="csv:"):
        existing_by_csv_id[t.external_id[4:]] = t

    from application.models.item import Comment as ItemComment
    from application.services.external_tags import merge_external_tags

    # Maps for relationship-wiring pass after all rows are imported.
    #   numeric_to_csv_id : Jira "Vorgangs ID" → csv_id (issue key)
    #   tasks_by_csv_id   : csv_id → Item (this batch + pre-existing)
    #   rel_for_task      : csv_id → {parent_jira_id, child_keys, related_keys}
    numeric_to_csv_id: dict = {}
    tasks_by_csv_id: dict = dict(existing_by_csv_id)
    rel_for_task: dict = {}

    # Column index lookups for the relation columns.
    def _first_idx(*names):
        for n in names:
            for i, h in enumerate(headers):
                if h == n:
                    return i
        return None
    idx_parent = _first_idx("übergeordnete id", "parent link", "parent id")
    idx_jira_num = _first_idx("vorgangs id", "vorgangs-id", "issue id")
    idx_child_in = [i for i, h in enumerate(headers)
                    if "verknüpfung zu eingehendem vorgang (child)" in h]
    idx_relates_in = [i for i, h in enumerate(headers)
                      if "verknüpfung zu eingehendem vorgang" in h and "(child)" not in h]
    idx_relates_out = [i for i, h in enumerate(headers)
                       if "ausgehende vorgangsverknüpfung" in h]

    created = updated = skipped = 0
    seen_csv_ids: set = set()
    for values in body:
        if not any(v.strip() for v in values):
            continue  # blank line
        title = _row_get(values, *title_aliases)
        if not title and auto_title_idx is not None and auto_title_idx < len(values):
            title = (values[auto_title_idx] or "").strip()
        if not title:
            skipped += 1
            continue
        csv_id = _row_get(values, "csv_id", "id", "issue key", "key",
                          "issue id", "ticket id",
                          "vorgangsschlüssel", "vorgangs id", "vorgangs-id")
        if csv_id:
            seen_csv_ids.add(csv_id)
        target = existing_by_csv_id.get(csv_id) if csv_id else None
        if target is None:
            target = Item(owner=user, kind="task", parent=project, status="open")
            created += 1
        else:
            updated += 1
        target.title = title[:500]
        target.source = source_tag
        if is_jira:
            # Denormalise the (possibly inherited) base URL so external_url()
            # can build the /browse/<KEY> deep link without a per-row lookup.
            target.jira_url = effective_jira_url

        note_val = _row_get(values, "note", "description", "beschreibung")
        if note_val:
            target.note = note_val
        status_val = _row_get(values, "status")
        if status_val and not is_jira:
            # For Jira CSV the status ("Offen", "Backlog", "WAITING", …)
            # is surfaced as a tag (see below) so the user can filter
            # by it without polluting Cairn's task-status field. Don't
            # set ``target.status`` here — leave the default — so the
            # value isn't rendered twice (status badge + tag chip).
            target.status = status_val
        prio_val = _row_get(values, "priority", "priorität")
        if prio_val:
            # Jira ships a name like "Highest"/"High"/"Medium"/… in
            # German "Hoch"/"Mittel"/"Niedrig". Map words to 1..4 first,
            # fall back to int parsing for numeric exports.
            prio_map = {
                "highest": 1, "high": 2, "medium": 3, "low": 4, "lowest": 4,
                "blocker": 1, "critical": 1, "major": 2, "minor": 3, "trivial": 4,
                "hoch": 2, "höchste": 1, "mittel": 3, "niedrig": 4, "tief": 4,
                "kritisch": 1, "blockierend": 1,
            }
            mapped = prio_map.get(prio_val.lower())
            if mapped is not None:
                target.priority = mapped
            else:
                try:
                    target.priority = int(prio_val)
                except ValueError:
                    pass
        flagged_val = _row_get(values, "flagged")
        if flagged_val:
            target.flagged = flagged_val.lower() in ("1", "true", "yes", "y")
        due_val = _row_get(values, "due", "due date", "duedate",
                           "fälligkeitsdatum", "fälligkeit")
        if due_val:
            from application.services.tz import attach_user_tz
            try:
                # CSV dates are wall-clock in the user's local time;
                # attach their tz so mongoengine doesn't store them as
                # naive UTC and drift everything by the user's offset.
                target.due_date = attach_user_tz(
                    datetime.fromisoformat(due_val), user,
                )
            except ValueError:
                pass

        # Labels: gather across every same-named column, then split each
        # cell on comma in case there are multiple labels per cell.
        # Always run the merge — an empty new set means "the user
        # removed every label upstream", and we still want to drop the
        # stale external_tags from the previous sync without touching
        # any tags the user added in Cairn.
        #
        # For Jira CSVs the workflow status ("Offen", "Backlog",
        # "WAITING", …) is also surfaced as a label so it shows up on
        # the task row and can be filtered like any other tag.
        from application.services.external_tags import ensure_user_has_tags

        label_values: list[str] = []
        for i in label_idx:
            if i < len(values) and values[i].strip():
                for piece in values[i].split(","):
                    piece = piece.strip()
                    if piece and piece not in label_values:
                        label_values.append(piece)
        # Auto-group new tags so the tag tree stays tidy:
        #   Jira labels (Stichwörter) → "Jira: Label"
        #   Jira workflow status     → "Jira: Status"
        #   Generic CSV labels       → "CSV"
        # Existing tags are never re-parented (see ensure_user_has_tags),
        # so the user can pull a tag out of its group and the next
        # sync respects that.
        if label_values:
            ensure_user_has_tags(
                user, label_values,
                group=("Jira: Label" if is_jira else "CSV"),
            )
        if is_jira and status_val:
            ensure_user_has_tags(user, [status_val], group="Jira: Status")

        csv_tags = list(label_values)
        if is_jira and status_val and status_val not in csv_tags:
            csv_tags.append(status_val)
        if label_idx or (is_jira and status_val):
            target.tags = merge_external_tags(
                target.tags, target.external_tags, csv_tags)
            target.external_tags = list(csv_tags)

        # Comments: each Comment column becomes one ItemComment. Jira's
        # Kommentar/Comment cells use ``timestamp;author;body`` (semicolon-
        # separated, body may carry newlines). Parse so the header reads
        # naturally; fall back to the raw cell if it doesn't have the
        # 3-field shape.
        if comment_idx and not target.comments:
            for i in comment_idx:
                if i >= len(values):
                    continue
                cell = (values[i] or "").strip()
                if not cell:
                    continue
                parts = cell.split(";", 2)
                if len(parts) == 3:
                    ts, author, body = (p.strip() for p in parts)
                elif len(parts) == 2:
                    ts, body = parts
                    ts = ts.strip(); body = body.strip(); author = ""
                else:
                    ts = author = ""
                    body = cell
                body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
                # Imported comments are external: the real author has no Cairn
                # account, so keep their name in ``external_author`` (rendered
                # with an "external" badge) rather than crediting the importing
                # user. Use the row timestamp as created_at when it parses;
                # otherwise keep it visible as a small prefix so nothing is lost.
                # NB: a distinct name from the ``created`` row counter above —
                # reusing ``created`` here clobbered the counter to None and the
                # next row's ``created += 1`` blew up.
                created_dt = None
                if ts:
                    try:
                        created_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        body = f"_{ts}_\n\n{body}" if body else ts
                # A comment the importing user wrote themselves is attributed to
                # their Cairn account (matched by name) but still flagged
                # external; everyone else keeps just their name.
                mine = author and author.strip().lower() in {
                    (user.display_name or "").strip().lower(),
                    (user.username or "").strip().lower(),
                }
                ckwargs = {"external": True, "body": body}
                if mine:
                    ckwargs["author"] = user
                else:
                    ckwargs["external_author"] = author or "Import"
                if created_dt:
                    ckwargs["created_at"] = created_dt
                target.comments.append(ItemComment(**ckwargs))

        if csv_id:
            target.external_id = f"csv:{csv_id}"
            target.external_ref = csv_id
        # Tag-driven status mapping ("Warte auf Kunde → waiting") —
        # applies whether labels came from Stichwörter columns or the
        # Jira Status value we surface as a tag.
        from application.services.label_rules import apply_label_rules
        apply_label_rules(target, user)
        target.save()

        # Record relationship metadata for the second pass.
        if csv_id:
            tasks_by_csv_id[csv_id] = target
            if idx_jira_num is not None and idx_jira_num < len(values):
                num = (values[idx_jira_num] or "").strip()
                if num:
                    numeric_to_csv_id[num] = csv_id
            parent_num = (values[idx_parent] or "").strip() if idx_parent is not None and idx_parent < len(values) else ""
            child_keys = [(values[i] or "").strip() for i in idx_child_in if i < len(values)]
            related_in = [(values[i] or "").strip() for i in idx_relates_in if i < len(values)]
            related_out = [(values[i] or "").strip() for i in idx_relates_out if i < len(values)]
            rel_for_task[csv_id] = {
                "parent_num": parent_num,
                "child_keys": [k for k in child_keys if k],
                "related_keys": [k for k in (related_in + related_out) if k],
            }

    # ---- Relationship wiring (after every row is upserted) -----------
    # Jira lets a row reference other tickets in several ways. We mirror
    # them onto the Cairn items:
    #   * "Übergeordnete ID" (numeric Jira id) → this task's parent is
    #     the imported task whose Vorgangs ID matches.
    #   * "Verknüpfung zu eingehendem Vorgang (Child)" → the referenced
    #     task is a child OF this one; flip the direction.
    #   * Other relates / solution links don't fit Cairn's tree, so we
    #     append them to the note as a small markdown reference list.
    def _resolve_csv_id_for_other_ticket(token: str):
        """Token can be either a numeric Vorgangs ID or a "PROJ-123" key."""
        if not token:
            return None
        if token in numeric_to_csv_id:
            return numeric_to_csv_id[token]
        if token in tasks_by_csv_id:  # already a Vorgangsschlüssel
            return token
        return None

    for csv_id, rels in rel_for_task.items():
        me = tasks_by_csv_id.get(csv_id)
        if not me:
            continue
        # Parent — promote to project so the tree renders the hierarchy
        # (tasks-inside-tasks don't show as containers in the tree view).
        if rels["parent_num"]:
            parent_cid = _resolve_csv_id_for_other_ticket(rels["parent_num"])
            parent_item = tasks_by_csv_id.get(parent_cid) if parent_cid else None
            if parent_item and parent_item.pk != me.pk and me.parent != parent_item:
                parent_item.ensure_container()
                me.parent = parent_item
                me.save()
        # Children — flip the link, and promote myself to a project.
        if rels["child_keys"]:
            promoted = False
            for ck in rels["child_keys"]:
                child = tasks_by_csv_id.get(ck)
                if child and child.pk != me.pk and child.parent != me:
                    if not promoted:
                        me.ensure_container()
                        promoted = True
                    child.parent = me
                    child.save()
        # Other relates → markdown list in the note (skip if already present)
        if rels["related_keys"]:
            link_lines = []
            for rk in rels["related_keys"]:
                marker = f"[{rk}]"
                if marker not in (me.note or ""):
                    link_lines.append(f"- Related: {marker}")
            if link_lines:
                me.note = ((me.note or "").rstrip()
                           + ("\n\n" if me.note else "")
                           + "\n".join(link_lines))
                me.save()

    archived = 0
    if mode == "sync" and archive_missing and seen_csv_ids is not None:
        for cid, t in existing_by_csv_id.items():
            if cid in seen_csv_ids:
                continue
            t.status = "done"
            t.save()
            archived += 1

    parts = [
        f"created {created}",
        f"updated {updated}",
        f"skipped {skipped}",
    ]
    if archived:
        parts.append(f"archived {archived}")
    parts.append(f"delimiter={dialect.delimiter!r}")
    if any(h in headers for h in title_aliases):
        title_col = next(h for h in title_aliases if h in headers)
        parts.append(f"title column={title_col!r}")
    elif auto_title_idx is not None:
        parts.append(f"title column (auto)={headers[auto_title_idx]!r}")
    else:
        parts.append("title column=none")
    flash("CSV processed — " + ", ".join(parts) + ".",
          "success" if (created or updated) else "warning")
    return redirect(url_for("projects.detail", project_id=project_id))


@projects_bp.route("/<project_id>/unwrap", methods=["POST"])
@login_required
def unwrap(project_id):
    """Delete a container but keep its contents — children get
    re-parented one level up. Useful for sections: ditch the
    grouping wrapper without losing the tasks inside.

    Same JSON / referrer-redirect dance as ``delete`` so the tree
    and list views can call it via the shared item-delete handler.
    """
    from flask import jsonify
    user = current_user._get_current_object()
    item = Item.objects(pk=project_id).first()
    if not item or item.owner != user:
        abort(404)
    new_parent = item.parent
    moved = Item.objects(parent=item).update(set__parent=new_parent)
    item.delete()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "moved": moved})
    flash(f"{item.kind.title()} unwrapped — {moved} child item(s) moved up.",
          "info")
    back = request.referrer or ""
    if not back or f"/projects/{project_id}" in back:
        back = url_for("projects.index")
    return redirect(back)


@projects_bp.route("/<project_id>/delete", methods=["POST"])
@login_required
def delete(project_id):
    from flask import jsonify
    from application.services.trash import trash_item
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project or project.owner != user:
        abort(404)
    # Soft-delete the whole subtree (sub-projects, sections, tasks) into the
    # trash; restorable until purged.
    n = trash_item(project)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "trashed": n})
    flash(f"Moved to trash — {n} item(s) incl. contents.", "info")
    # Stay where the user came from — falling back to the projects
    # list. If they came from the deleted item's own page, bounce up
    # one level (its parent) so the redirect doesn't 404.
    back = request.referrer or ""
    if not back or f"/projects/{project_id}" in back:
        back = url_for("projects.index")
    return redirect(back)
