"""GTD weekly review — OmniFocus-style.

The review is a focused, one-unit-at-a-time walk. The index shows the ordered
queue of projects that need review; opening one shows the full project (all its
open actions) so you can act on them, then "Mark reviewed" stamps the project
(and its tasks) and jumps to the next one in the queue.

A **folder that directly holds open tasks** is a review unit too. The model says
a folder holds projects, not tasks (see the ``Item`` docstring), but tasks land
in one anyway — dragged there, filed by an import, PUT into the folder's CalDAV
list — and such a task has no enclosing project. It was invisible to the entire
review: not in the queue, not pulled in by its own interval, and not in the
Inbox either (that is ``parent=None``). So the review asks the folders as well;
a folder without direct tasks stays out, it is a pure organiser.
"""
from datetime import datetime, timezone

from flask import (
    Blueprint, abort, flash, redirect, render_template, session, url_for,
)
from flask_login import current_user, login_required
from mongoengine import Q
from application.models.item import Item

review_bp = Blueprint("review", __name__, url_prefix="/review")

_SKIP_KEY = "review_skipped"


def _focus_q(user):
    """Q matching items inside the active focus subtree by their **own** pk.

    Review operates on projects (not their child tasks), so the focus filter
    is on the project's identity, not its parent. Empty ``Q()`` (matches all)
    when no focus is set."""
    from application.services import focus
    pks = focus.focus_pks(user)
    return Q(pk__in=pks) if pks is not None else Q()


def _skipped() -> set:
    return set(session.get(_SKIP_KEY, []))


def _task_folders(user, fq):
    """``{pk: Item}`` for the folders that directly hold at least one open
    task — the folders that are review units. Two indexed queries; a folder
    with no direct tasks is left out so the queue isn't padded with organisers.
    Notes folders are excluded, same as in the project navigator."""
    folders = {f.pk: f for f in Item.objects(fq, owner=user, kind="folder",
                                             status="active",
                                             folder_domain__ne="notes")}
    if not folders:
        return {}
    held = set()
    for t in (Item.objects(owner=user, kind="task",
                           status__nin=["done", "dropped"],
                           parent__in=list(folders)).only("parent")):
        # Read the id out of the reference instead of dereferencing it — the
        # parent document is already in ``folders``.
        raw = t._data.get("parent")
        held.add(getattr(raw, "id", None) or getattr(raw, "pk", None))
    return {pk: f for pk, f in folders.items() if pk in held}


def _review_unit(item, folder_pks=frozenset()):
    """Climb to the container this item is reviewed under: the nearest ancestor
    project, or a task-holding folder if that one comes first (skipping
    sections and parent tasks). None if there is nothing above it."""
    node = item.parent
    seen = set()
    while node is not None and node.pk not in seen:
        if node.kind == "project" or node.pk in folder_pks:
            return node
        seen.add(node.pk)
        node = node.parent
    return None


def _due_task_pks(user, now, folder_pks=frozenset()):
    """Pks of tasks whose own (overridden) review interval has fallen due,
    plus the set of review units they pull into the queue."""
    from application.services import focus
    due_pks, pulled = set(), set()
    for t in Item.objects(
        focus.restrict(Q(owner=user), user),
        kind="task", status__nin=["done", "dropped"],
        review_interval_days__ne=None,
    ):
        if not t.is_review_due(now):
            continue
        due_pks.add(t.pk)
        unit = _review_unit(t, folder_pks)
        if unit is not None and unit.status == "active":
            pulled.add(unit.pk)
    return due_pks, pulled


def _review_queue(user, now):
    """Active projects needing review, in **project-tree order** (same order as
    the sidebar) so "next" walks straight down the tree instead of jumping
    around by date. A project is in the queue if it's due by its own clock,
    never reviewed, or holds an independently-due task."""
    from application.views.projects import project_tree_for_sidebar
    # Due by the clock: next_review_at has passed. mark_reviewed keeps this
    # field accurate, so a just-reviewed project (next_review_at in the future,
    # or None when its interval is 0) is excluded here immediately.
    fq = _focus_q(user)
    candidates = {
        p.pk: p
        for p in Item.objects(fq, owner=user, kind="project", status="active",
                              next_review_at__lte=now)
    }
    # Never reviewed yet, but actually on a schedule (positive interval). The
    # last_reviewed_at=None guard is what stops a reviewed-but-interval-0
    # project (next_review_at=None, last_reviewed_at set) from reappearing.
    for p in Item.objects(fq, owner=user, kind="project", status="active",
                          next_review_at=None, last_reviewed_at=None):
        if p.effective_review_interval():
            candidates.setdefault(p.pk, p)
    # Folders that hold tasks directly — the only home those tasks have.
    folders = _task_folders(user, fq)
    for pk, f in folders.items():
        if pk not in candidates and f.is_review_due(now):
            candidates[pk] = f
    _, pulled = _due_task_pks(user, now, set(folders))
    missing = [pk for pk in pulled if pk not in candidates]
    if missing:
        for p in Item.objects(pk__in=missing, status="active"):
            candidates[p.pk] = p
    # Walk the sidebar tree and keep the due projects in that order.
    ordered = []
    for _depth, p in project_tree_for_sidebar(user):
        if p.pk in candidates:
            ordered.append(candidates.pop(p.pk))
    ordered.extend(candidates.values())  # any not in the tree (defensive)
    return ordered


def review_due_count(user, now):
    """Cheap count of projects needing review — for the sidebar badge. Mirrors
    _review_queue minus the (rare) override-task pull and tree ordering."""
    fq = _focus_q(user)
    n = Item.objects(fq, owner=user, kind="project", status="active",
                     next_review_at__lte=now).count()
    for p in Item.objects(fq, owner=user, kind="project", status="active",
                          next_review_at=None, last_reviewed_at=None):
        if p.effective_review_interval():
            n += 1
    for f in _task_folders(user, fq).values():
        if f.is_review_due(now):
            n += 1
    return n


def _next_project(user, now, current_pk, skipped):
    """The next project to review after ``current_pk`` — strictly forward in
    tree order, excluding skipped projects (and the current one). Forward-only
    so reviewing/skipping always walks down the tree and never jumps back to a
    project you already passed or skipped. Returns None at the end of the pass."""
    from application.views.projects import project_tree_for_sidebar
    order = [p.pk for _d, p in project_tree_for_sidebar(user)]
    pos = {pk: i for i, pk in enumerate(order)}
    cur_i = pos.get(current_pk, -1)
    forward = [
        p for p in _review_queue(user, now)
        if p.pk != current_pk
        and str(p.pk) not in skipped
        and pos.get(p.pk, -1) > cur_i
    ]
    return forward[0] if forward else None


def _collect_open_tasks(project, user):
    """Flat, parents-first list of the project's open actions — descending
    through sections and sub-tasks. Section headers are transparent (their
    tasks render at the surrounding depth); each task carries ``_depth`` for
    the row template's indentation. Sub-projects (and folders) are **opaque**:
    they get their own review-queue entry, so descending into them here would
    review their tasks twice (once under this project, once under themselves)."""
    children: dict = {}
    frontier = [project.pk]
    seen = set()
    while frontier:
        nf = []
        for c in (Item.objects(owner=user, parent__in=frontier)
                  .order_by("+sort_order", "+created_at")):
            if c.pk in seen:
                continue
            seen.add(c.pk)
            children.setdefault(c.parent.pk, []).append(c)
            nf.append(c.pk)
        frontier = nf

    out: list = []

    def walk(pk, depth):
        for c in children.get(pk, []):
            if c.kind == "task":
                if c.status in ("done", "dropped"):
                    continue
                c._depth = depth
                c._has_children = any(
                    k.kind == "task" for k in children.get(c.pk, [])
                )
                out.append(c)
                walk(c.pk, depth + 1)
            elif c.kind == "section":
                # section: transparent — its tasks render at this depth.
                walk(c.pk, depth)
            # sub-project / folder: opaque — reviewed via their own queue entry.

    walk(project.pk, 0)
    return out


@review_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()
    now = datetime.now(timezone.utc)
    # Visiting the index starts a fresh review pass — forget earlier skips.
    session.pop(_SKIP_KEY, None)
    queue = _review_queue(user, now)
    due_task_pks, _ = _due_task_pks(user, now, set(_task_folders(user, _focus_q(user))))
    # Open-action + due-task counts per project for the list rows.
    open_counts: dict = {}
    due_counts: dict = {}
    if queue:
        qpks = [p.pk for p in queue]
        for t in Item.objects(owner=user, kind="task", parent__in=qpks,
                              status__nin=["done", "dropped"]).only("pk", "parent"):
            open_counts[t.parent.pk] = open_counts.get(t.parent.pk, 0) + 1
            if t.pk in due_task_pks:
                due_counts[t.parent.pk] = due_counts.get(t.parent.pk, 0) + 1
    upcoming = (Item.objects(_focus_q(user), owner=user, kind="project",
                             status="active", next_review_at__gt=now)
                .order_by("+next_review_at").limit(10))
    return render_template(
        "review/index.html",
        queue=queue,
        open_counts=open_counts,
        due_counts=due_counts,
        upcoming=upcoming,
        affected_pks=[str(p.pk) for p in queue],
    )


@review_bp.route("/<project_id>")
@login_required
def project_review(project_id):
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project or project.owner != user or project.kind not in ("project",
                                                                     "folder"):
        abort(404)
    now = datetime.now(timezone.utc)
    queue = _review_queue(user, now)
    queue_pks = [p.pk for p in queue]
    try:
        pos = queue_pks.index(project.pk)
    except ValueError:
        pos = None
    # Forward-only next, honouring projects skipped earlier in this pass.
    next_project = _next_project(user, now, project.pk, _skipped())

    tasks = _collect_open_tasks(project, user)
    # Sidebar shows only the remaining review queue (+ the current project so
    # it stays visible while you're on it). Reviewed projects drop out.
    affected = [str(p.pk) for p in queue]
    if str(project.pk) not in affected:
        affected.append(str(project.pk))
    return render_template(
        "review/project.html",
        project=project,
        tasks=tasks,
        review_due=project.is_review_due(now),
        position=(pos + 1) if pos is not None else None,
        total=len(queue),
        next_project=next_project,
        affected_pks=affected,
    )


@review_bp.route("/<project_id>/done", methods=["POST"])
@login_required
def mark_reviewed(project_id):
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project or project.owner != user:
        abort(404)
    project.mark_reviewed()  # cascades to the project's tasks
    flash(f"Reviewed: {project.name}", "success")
    # OmniFocus-style: jump forward to the next project still due, skipping
    # anything skipped earlier in this pass.
    now = datetime.now(timezone.utc)
    nxt = _next_project(user, now, project.pk, _skipped())
    if nxt is not None:
        return redirect(url_for("review.project_review", project_id=str(nxt.pk)))
    return redirect(url_for("review.index"))


@review_bp.route("/<project_id>/skip")
@login_required
def skip(project_id):
    """Move past this project without reviewing it — remember the skip for
    this pass so the flow never circles back to it as 'next'."""
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project or project.owner != user:
        abort(404)
    skipped = session.get(_SKIP_KEY, [])
    if str(project.pk) not in skipped:
        skipped.append(str(project.pk))
        session[_SKIP_KEY] = skipped
    now = datetime.now(timezone.utc)
    nxt = _next_project(user, now, project.pk, set(skipped))
    if nxt is not None:
        return redirect(url_for("review.project_review", project_id=str(nxt.pk)))
    return redirect(url_for("review.index"))
