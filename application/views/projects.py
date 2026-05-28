"""Item views — CRUD plus per-project task list."""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

def _is_inspector():
    return request.args.get("inspector") == "1" or request.headers.get("X-Inspector-Request") == "1"

def _render_project_inspector(project, user, *, just_saved=False):
    form = ProjectForm(obj=project)
    _populate_choices(form, user, project)
    form.parent.data = str(project.parent.pk) if project.parent else ""
    form.members.data = [str(m.pk) for m in (project.members or [])]
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

from application.forms import ProjectForm

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
    # "Inside" picker holds containers only — projects (active) and
    # folders. Sections live inline in a project and shouldn't host
    # sub-projects; tasks are obviously not containers.
    parents = Item.objects(
        owner=user, kind__in=["project", "folder"],
    ).order_by("+name")
    form.parent.choices = [("", "— top level —")] + [
        (str(p.pk), _parent_label(p)) for p in parents if not current or p.pk != current.pk
    ]
    # Sharing is restricted to team members. Free / solo users see no
    # candidates (the form still renders, but the dropdown is empty so
    # they can't share). Premium and team_admin show their teammates.
    from application.services.permissions import shareable_user_ids
    candidate_ids = [pk for pk in shareable_user_ids(user) if pk != str(user.pk)]
    users = User.objects(pk__in=candidate_ids) if candidate_ids else []
    form.members.choices = [(str(u.pk), str(u)) for u in users]

def _parent_label(p: Item) -> str:
    """Show a hint about what kind of container this candidate is."""
    if p.is_folder:
        return f"🗂️  {p.name}"
    if p.is_section:
        return f"🔖  {p.name}"
    return f"📁  {p.name}"

def _apply(form: ProjectForm, project: Item) -> None:
    project.name = form.name.data
    project.note = form.note.data or ""
    project.color = (form.color.data or "").strip()
    project.tags = list(form.tags.data or [])
    project.due_date = form.due_date.data
    project.plan_date = form.plan_date.data
    kind = form.kind.data or "project"
    project.kind = kind
    project.parent = (
        Item.objects(pk=form.parent.data).first() if form.parent.data else None
    )
    project.members = (
        list(User.objects(pk__in=form.members.data)) if form.members.data else []
    )

    # Kind-specific cleanup. Sections share the project_type field
    # (they can be sequential too — the per-section blocking logic
    # honours it). Folders have no execution semantics.
    if kind == "project":
        project.project_type = form.project_type.data
        project.status = form.status.data
        project.review_interval_days = form.review_interval_days.data or 0
    elif kind == "section":
        project.project_type = form.project_type.data or "parallel"
        project.status = "active"
        project.review_interval_days = 0
        project.next_review_at = None
        project.last_reviewed_at = None
    else:  # folder
        project.project_type = "parallel"
        project.status = "active"
        project.review_interval_days = 0
        project.next_review_at = None
        project.last_reviewed_at = None

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
    qs = Item.objects(
        (Q(owner=user) | Q(members=user)),
        kind__in=["project", "folder"],
    )
    # Folders have no real status; keep them visible. Projects default
    # to "active" only so on_hold ("warte auf antwort"), completed, and
    # dropped don't clutter the overview.
    if not show_inactive:
        qs = qs.filter(Q(kind="folder") | Q(status="active"))
    projects = list(qs.order_by("+name"))
    return render_template(
        "projects/list.html",
        projects=_as_tree(projects),
        show_inactive=show_inactive,
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
    projects = list(
        Item.objects(
            (Q(owner=user) | Q(members=user)),
            kind__in=["project", "folder"],
            status="active",
        ).order_by("+name")
    )
    # Need sections to resolve "skip-past" parent links. Fetch them in
    # one query, then build a {section_pk: effective_parent_pk} map.
    sections = list(
        Item.objects(
            (Q(owner=user) | Q(members=user)),
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

    return _as_tree_skipping_sections(projects, section_parent)


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
    if request.method == "GET":
        if request.args.get("folder") == "1":
            form.kind.data = "folder"
        elif request.args.get("section") == "1":
            form.kind.data = "section"
        elif request.args.get("kind") in ("project", "folder", "section"):
            form.kind.data = request.args["kind"]
        parent_id = request.args.get("parent")
        if parent_id and Item.objects(pk=parent_id, owner=user).first():
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
            flash(
                f"Free plan limit reached: {FREE_PROJECT_LIMIT} projects. "
                "Ask a global admin to upgrade your account.",
                "warning",
            )
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

@projects_bp.route("/<project_id>")
@login_required
def detail(project_id):
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project or not project.can_be_viewed_by(user):
        abort(404)
    # All Items share one collection — a pk that points at a task
    # would otherwise render as an empty "project". Bounce the caller
    # to the right view so the URL never shows the same item twice.
    if project.kind == "task":
        return redirect(url_for("tasks.detail", task_id=project_id))

    grouping = (request.args.get("group") or project.task_grouping or "section").strip()
    show_completed = request.args.get("completed") == "1"
    view_mode = (request.args.get("view") or project.view_mode or "list").strip()
    if view_mode not in ("list", "kanban", "calendar"):
        view_mode = "list"

    sections = list(
        Item.objects(parent=project, kind="section").order_by("+name")
    )
    # Sub-projects (and folders, but not sections) — rendered as
    # task-like rows inside each section bucket. They live under the
    # project itself OR a specific section.
    sub_projects_all = list(
        Item.objects(parent=project, kind__in=["project", "folder"])
            .order_by("+sort_order", "+name")
    )
    for s in sections:
        sub_projects_all.extend(
            Item.objects(parent=s, kind__in=["project", "folder"])
                .order_by("+sort_order", "+name")
        )
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
        for kid in Item.objects(parent__in=sp_pks, kind="task").order_by(
            "+sort_order", "+created_at"
        ):
            subproject_children.setdefault(kid.parent.pk, []).append(kid)
        # _has_children comes from the dict (kid.parent dereferences to a
        # fresh instance, so writing on it would miss the sp in the loop
        # above — match by pk instead).
        for sp in sub_projects_all:
            sp._has_children = bool(subproject_children.get(sp.pk))

    # Default: show only this project's own tasks (incl. sections —
    # sections are inline parts of the project). Sub-projects live on
    # their own pages and are listed as a tree at the top of this view.
    # ``?descendants=1`` opts into the rolled-up view.
    include_descendants = request.args.get("descendants") == "1"
    if include_descendants:
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
    while frontier:
        batch = list(
            Item.objects(kind="task", parent__in=frontier)
                .order_by("+sort_order", "+created_at")
        )
        next_frontier = []
        for t in batch:
            if t.pk in seen:
                continue
            seen.add(t.pk)
            all_tasks.append(t)
            next_frontier.append(t.pk)
        frontier = next_frontier
    # Build child map for DFS in the renderer.
    project_pks_set = {p for p in project_ids}
    children_by_task: dict = {}
    for t in all_tasks:
        if t.parent and t.parent.pk not in project_pks_set:
            children_by_task.setdefault(t.parent.pk, []).append(t)
    # DFS: replace the flat ``all_tasks`` with parents-first order so a
    # task appears immediately before its sub-tasks. Each task gets a
    # ``_depth`` attribute the template uses for indentation. Only
    # top-level tasks (direct children of project/section) survive in
    # the bucket lists; descendants get attached during DFS render.
    top_level = [t for t in all_tasks
                  if t.parent and t.parent.pk in project_pks_set]
    collapsed_pks = set(user.collapsed_items or [])
    flat_ordered: list = []
    task_section_pk: dict = {}  # task.pk → section/project pk for bucketing
    def _dfs(task, depth, section_pk):
        task._depth = depth
        task._has_children = bool(children_by_task.get(task.pk))
        task._collapsed = str(task.pk) in collapsed_pks
        task_section_pk[task.pk] = section_pk
        flat_ordered.append(task)
        # Stop descent at collapsed nodes — they still render with a
        # chevron the user can flip back open.
        if task._collapsed:
            return
        for c in children_by_task.get(task.pk, []):
            _dfs(c, depth + 1, section_pk)
    for t in top_level:
        _dfs(t, 0, t.parent.pk)
    all_tasks = flat_ordered

    # ----- grouping ---------------------------------------------------
    # Sections are ALWAYS the outer grouping — that's the project's own
    # structure. The user-chosen ``grouping`` is applied INSIDE each
    # section's open bucket (None = flat list of tasks).

    sequential = project.project_type == "sequential" and not project.show_blocked_tasks

    section_buckets = _bucket_by_section(all_tasks, sections, task_section_pk)
    sections_by_pk = {str(s.pk): s for s in sections}

    # Stitch sub-projects into each container's bucket so they share the
    # sort space with tasks. Each bucket gets ``items`` = [(kind, obj),
    # ...] sorted by sort_order — that's what the template renders when
    # the user hasn't picked a secondary grouping. ``open`` stays
    # task-only so grouping helpers and the existing slicing still work.
    for key, bucket in section_buckets.items():
        subs = subprojects_by_section.get(key) or []
        merged = (
            [("p", sp, sp.sort_order or 0) for sp in subs] +
            [("t", t, t.sort_order or 0) for t in bucket["open"]]
        )
        merged.sort(key=lambda x: x[2])
        bucket["items"] = [(kind, obj) for kind, obj, _ in merged]
        bucket["subprojects"] = subs  # used when grouping is on

    # Per-container sequential collapse: each section decides
    # independently. show_blocked_tasks turns it off so blocked tasks
    # render greyed out (template handles the .blocked class).
    for key, bucket in section_buckets.items():
        container = project if key is None else sections_by_pk.get(key)
        if not container:
            continue
        if (container.project_type == "sequential"
                and not container.show_blocked_tasks):
            # Sequential mode: only the first item (task OR sub-project)
            # in sort order remains visible. Done/dropped sub-projects
            # don't block, so consider only "live" ones.
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

    # Apply secondary grouping inside each section's open bucket and,
    # in parallel, inside each sub-project's kid list — so a priority
    # view on the parent page also splits the sub-project's tasks into
    # P1/P2/… groups instead of one flat list.
    # "section" and "subproject" are outer-grouping modes — sections /
    # sub-project rows already provide the structure, so no inner
    # grouping is applied (would otherwise produce a spurious
    # "All tasks" header from the _group_tasks fallback).
    grouping_tag = (project.task_grouping_tag or "").strip()
    subproject_groups: dict = {}
    if grouping == "tag" and grouping_tag:
        # Focused tag: bucket by its child tags (if any) or by has/has-not.
        for bucket in section_buckets.values():
            bucket["open_groups"] = _group_by_tag_specific(
                bucket["open"], user, grouping_tag,
            )
        for sp_pk, kids in subproject_children.items():
            subproject_groups[sp_pk] = _group_by_tag_specific(
                kids, user, grouping_tag,
            )
    elif grouping in ("priority", "status", "tag", "due"):
        for bucket in section_buckets.values():
            bucket["open_groups"] = _group_tasks(bucket["open"], grouping)
        for sp_pk, kids in subproject_children.items():
            subproject_groups[sp_pk] = _group_tasks(kids, grouping)
    else:
        for bucket in section_buckets.values():
            bucket["open_groups"] = None

    # Build calendar buckets if needed (current month grid with tasks by day).
    calendar_data = None
    if view_mode == "calendar":
        calendar_data = _build_calendar(all_tasks, request.args.get("month"))

    return render_template(
        "projects/detail.html",
        project=project,
        sections=sections,
        sub_projects=sub_projects,
        subprojects_by_section=subprojects_by_section,
        subproject_children=subproject_children,
        subproject_groups=subproject_groups,
        section_buckets=section_buckets,
        grouping=grouping,
        show_completed=show_completed,
        include_descendants=include_descendants,
        sequential=sequential,
        ancestors=_ancestor_chain(project),
        view_mode=view_mode,
        calendar=calendar_data,
        all_tasks=all_tasks,
    )

def _build_calendar(tasks, month_param):
    """Compute a month grid (list of weeks; each week is 7 days, each day
    is ``{"date": date, "in_month": bool, "tasks": [...]}``)."""
    from calendar import Calendar
    from datetime import date as _date

    today = datetime.now(timezone.utc).date()
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
            key = t.due_date.date()
            by_day.setdefault(key, []).append(t)
        elif t.plan_date:
            key = t.plan_date.date()
            by_day.setdefault(key, []).append(t)
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

def _ancestor_chain(project: Item) -> list[Item]:
    """Return the project's ancestors from root → direct parent.

    The detail page renders this above the title as a clickable
    breadcrumb. Cycles are stopped at 10 hops just in case.
    """
    chain: list[Item] = []
    seen: set = set()
    cur = project.parent
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

def _group_tasks(tasks, mode):
    """Return ``(label, icon, [tasks], target_project_id)`` buckets.

    ``target_project_id`` is what the drop container exposes via
    ``data-target-project``:
      * a project pk → dropping there sets the task's project to that pk
      * ``None`` → drops only reorder, never reparent (priority / status /
        tag / due / no-grouping)
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
        return [(label, icon, buckets[p], None) for p, label, icon in order if buckets[p]]

    if mode == "status":
        order = [
            ("open", "Open", "bi-circle"),
            ("next", "Next", "bi-arrow-right-circle"),
            ("waiting", "Waiting", "bi-hourglass-split"),
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
    if not project or project.owner != user:
        abort(404)
    form = ProjectForm(obj=project)
    _populate_choices(form, user, project)
    if form.is_submitted() is False:
        form.parent.data = str(project.parent.pk) if project.parent else ""
        form.members.data = [str(m.pk) for m in (project.members or [])]
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
    if not project or not project.can_be_viewed_by(user):
        abort(404)
    project.show_blocked_tasks = not project.show_blocked_tasks
    project.save()
    # Stay on the page that fired the toggle — so toggling a section
    # from its parent project view doesn't navigate INTO the section.
    return redirect(
        request.referrer or url_for("projects.detail", project_id=project_id)
    )

@projects_bp.route("/<project_id>/view-mode", methods=["POST"])
@login_required
def set_view_mode(project_id):
    VIEW_MODES = ("list", "kanban", "calendar")

    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project or not project.can_be_viewed_by(user):
        abort(404)
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
    TASK_GROUPINGS = ("section", "subproject", "priority", "status", "tag", "due", "none")

    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project or not project.can_be_viewed_by(user):
        abort(404)
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
    """
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
    project.kind = "task"
    project.status = status_map.get(project.status, "open")
    # ``completed_at`` only makes sense on done/dropped tasks — clear
    # it so a converted active project doesn't show as already-done
    # because of a stale field.
    if project.status not in ("done", "dropped"):
        project.completed_at = None
    project.save()
    flash(f"Converted project into task: {project.title}", "success")
    return redirect(url_for("tasks.detail", task_id=str(project.pk)))

@projects_bp.route("/<project_id>/sections/new", methods=["POST"])
@login_required
def add_section(project_id):
    user = current_user._get_current_object()
    parent = Item.objects(pk=project_id).first()
    if not parent or not parent.can_be_viewed_by(user):
        abort(404)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Section name is required.", "danger")
        return redirect(url_for("projects.detail", project_id=project_id))
    section = Item(
        owner=user,
        name=name,
        project_type="parallel",
        status="active",
        parent=parent,
        kind="section",
        review_interval_days=0,
    )
    section.save()
    flash(f"Section '{name}' added.", "success")
    return redirect(url_for("projects.detail", project_id=project_id))

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
        from application.services.cleanup import delete_project_cascade
        for p in projects:
            r = delete_project_cascade(p)
            count += r["projects"]
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
        return render_template("projects/import_csv.html", project=project)

    fileobj = request.files.get("file")
    if not fileobj or not fileobj.filename:
        flash("No file uploaded.", "danger")
        return redirect(url_for("projects.import_csv", project_id=project_id))

    raw = fileobj.read().decode("utf-8-sig", errors="replace")
    reader = _csv.DictReader(StringIO(raw))
    mode = (request.form.get("mode") or "import").strip()
    archive_missing = bool(request.form.get("archive_missing"))

    # Index existing tasks by csv_id for sync.
    existing_by_csv_id: dict = {}
    if mode == "sync":
        for t in Item.objects(parent=project, kind="task"):
            if t.external_id and t.external_id.startswith("csv:"):
                existing_by_csv_id[t.external_id[4:]] = t

    created = updated = skipped = 0
    seen_csv_ids: set = set()
    for row in reader:
        row_lower = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        title = row_lower.get("title") or row_lower.get("name") or ""
        if not title:
            skipped += 1
            continue
        csv_id = row_lower.get("csv_id") or row_lower.get("id") or ""
        if csv_id:
            seen_csv_ids.add(csv_id)
        target = existing_by_csv_id.get(csv_id) if csv_id else None
        if target is None:
            target = Item(owner=user, kind="task", parent=project, status="open")
            created += 1
        else:
            updated += 1
        target.title = title[:500]
        if "note" in row_lower:
            target.note = row_lower["note"]
        if row_lower.get("status"):
            target.status = row_lower["status"]
        if row_lower.get("priority"):
            try:
                target.priority = int(row_lower["priority"])
            except ValueError:
                pass
        if row_lower.get("flagged"):
            target.flagged = row_lower["flagged"].lower() in ("1", "true", "yes", "y")
        if row_lower.get("due"):
            from datetime import datetime
            try:
                target.due_date = datetime.fromisoformat(row_lower["due"])
            except ValueError:
                pass
        if row_lower.get("tags"):
            tags = [t.strip() for t in row_lower["tags"].split(",") if t.strip()]
            target.tags = tags
        if csv_id:
            target.external_id = f"csv:{csv_id}"
        target.save()

    archived = 0
    if mode == "sync" and archive_missing and seen_csv_ids is not None:
        for cid, t in existing_by_csv_id.items():
            if cid in seen_csv_ids:
                continue
            t.status = "dropped"
            t.save()
            archived += 1

    flash(
        f"CSV processed — created {created}, updated {updated}, "
        f"skipped {skipped}" + (f", archived {archived}" if archived else "") + ".",
        "success",
    )
    return redirect(url_for("projects.detail", project_id=project_id))


@projects_bp.route("/<project_id>/delete", methods=["POST"])
@login_required
def delete(project_id):
    from application.services.cleanup import delete_project_cascade
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project or project.owner != user:
        abort(404)
    r = delete_project_cascade(project)
    flash(
        f"Item deleted — {r['projects'] - 1} sub-project(s) "
        f"and {r['tasks']} task(s) removed too.",
        "info",
    )
    return redirect(url_for("projects.index"))
