"""GTD weekly review — OmniFocus-style.

The review is a focused, one-unit-at-a-time walk. The index shows the ordered
queue of projects that need review; opening one shows the full project (all its
open actions) so you can act on them, then "Mark reviewed" stamps the project
(and its tasks) and jumps to the next one in the queue.

A **folder that directly holds open tasks** is a review unit too. A folder may
hold tasks (see the ``Item`` docstring) and such a task has no enclosing
project, so it used to be invisible to the entire review: not in the queue, not
pulled in by its own interval, and not in the Inbox either (that is
``parent=None``). So the review asks the folders as well; a folder without
direct tasks stays out, it is a pure organiser.

**An EMPTY container is not a review unit.** A project or folder with no open
task anywhere below it has nothing to walk through, so it is left out of the
queue (and therefore out of the review sidebar, which is filtered by it) unless
the user asks for it: "Show empty" on the review index, remembered per user in
``User.review_show_empty`` — the same shape as the project list's "Show
deferred".

Two things that decision needs stating:

- "Empty" is counted over the **whole subtree**, not the direct children, which
  is what makes the nesting work: a folder holding nothing but empty folders
  counts 0 along with them and they all drop out together, while a container
  stays as soon as anything anywhere below it is still open.
- Only OPEN tasks count. A project whose tasks are all done or dropped reads as
  empty and goes — which is exactly what the toggle is there to bring back.
"""
from datetime import datetime, timezone

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, session,
    url_for,
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


def count_open_below(unit_pks, parent_of, open_task_pks):
    """``{pk: n}`` — how many of ``open_task_pks`` sit anywhere below each unit.

    Pure: ``parent_of`` maps a pk to its parent's pk (containers AND open
    tasks, so a sub-task walks home through its parent task), missing = root.
    Every task walks its whole ancestor chain, so a container's count is the
    sum of everything under it and the nested case needs no second pass: a
    folder whose only content is empty folders ends up at 0 like they do.
    ``seen`` guards a parent cycle, same as ``_review_unit``.
    """
    counts = {pk: 0 for pk in unit_pks}
    for t in open_task_pks:
        pk, seen = parent_of.get(t), {t}
        while pk is not None and pk not in seen:
            seen.add(pk)
            if pk in counts:
                counts[pk] += 1
            pk = parent_of.get(pk)
    return counts


def _open_counts_for_units(units):
    """``{pk: open tasks anywhere below}`` for each unit — 0 means empty.

    Cost: one query per tree level below the units (not per unit, and never one
    per row), plus nothing else — the parent chain is read out of the raw
    reference, so no document is dereferenced. A done/dropped task is not
    followed, so an open sub-task hanging under a finished one doesn't count;
    that matches ``_collect_open_tasks``, which the review page renders.
    """
    unit_pks = [u.pk for u in units]
    if not unit_pks:
        return {}
    parent_of: dict = {}
    open_task_pks: list = []
    seen = set(unit_pks)
    frontier = list(unit_pks)
    while frontier:
        kids = list(Item.objects(parent__in=frontier)
                    .only("pk", "parent", "kind", "status"))
        frontier = []
        for c in kids:
            if c.pk in seen:
                continue
            if c.kind == "task" and c.status in ("done", "dropped"):
                continue
            seen.add(c.pk)
            # Read the parent out of the reference instead of dereferencing it.
            raw = c._data.get("parent")
            parent_of[c.pk] = getattr(raw, "id", None) or getattr(raw, "pk", None)
            frontier.append(c.pk)
            if c.kind == "task":
                open_task_pks.append(c.pk)
    return count_open_below(unit_pks, parent_of, open_task_pks)


def _drop_empty(user, units):
    """Stamp ``_open_below`` on every unit and drop the empty ones — unless the
    user deliberately switched them on (``User.review_show_empty``)."""
    counts = _open_counts_for_units(units)
    for u in units:
        u._open_below = counts.get(u.pk, 0)
    if getattr(user, "review_show_empty", False):
        return units
    return [u for u in units if u._open_below]


def _candidate_units(user, now):
    """``{pk: Item}`` of the projects/folders whose review cadence is up —
    unordered, and before the empty ones are dropped.

    The queue and the sidebar badge both go through here so the two can't
    disagree about what is due."""
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
    return candidates


def _review_queue(user, now):
    """Active projects needing review, in **project-tree order** (same order as
    the sidebar) so "next" walks straight down the tree instead of jumping
    around by date. A project is in the queue if it's due by its own clock,
    never reviewed, or holds an independently-due task — and if it isn't empty
    (see the module docstring). Each returned item carries ``_open_below``, the
    open-task count the queue row prints."""
    from application.views.projects import project_tree_for_sidebar
    candidates = _candidate_units(user, now)
    # Walk the sidebar tree and keep the due projects in that order.
    ordered = []
    for _depth, p in project_tree_for_sidebar(user):
        if p.pk in candidates:
            ordered.append(candidates.pop(p.pk))
    ordered.extend(candidates.values())  # any not in the tree (defensive)
    return _drop_empty(user, ordered)


def review_due_count(user, now):
    """Count of projects needing review — for the sidebar badge. Same
    candidates and the same empty-container rule as the queue (a badge that
    counts what the page then doesn't list is a bug report), minus the tree
    ordering the badge has no use for."""
    return len(_drop_empty(user, list(_candidate_units(user, now).values())))


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
    # The deliberate opt-in for empty containers: ``?show_empty=0/1`` flips it
    # and persists it on the user, like the project list's "Show deferred".
    se = request.args.get("show_empty")
    if se is not None and bool(user.review_show_empty) != (se == "1"):
        user.review_show_empty = (se == "1")
        user.save()
    show_empty = bool(user.review_show_empty)
    queue = _review_queue(user, now)
    due_task_pks, _ = _due_task_pks(user, now, set(_task_folders(user, _focus_q(user))))
    # Open-action count per row — the whole subtree, the same number the empty
    # rule is decided on, so a listed row never reads "0 open".
    open_counts = {p.pk: p._open_below for p in queue}
    due_counts: dict = {}
    if queue and due_task_pks:
        for t in Item.objects(owner=user, kind="task", parent__in=[p.pk for p in queue],
                              pk__in=list(due_task_pks)).only("pk", "parent"):
            raw = t._data.get("parent")
            pk = getattr(raw, "id", None) or getattr(raw, "pk", None)
            due_counts[pk] = due_counts.get(pk, 0) + 1
    upcoming = list(Item.objects(_focus_q(user), owner=user, kind="project",
                                 status="active", next_review_at__gt=now)
                    .order_by("+next_review_at").limit(25))
    # Same rule for the preview list — an empty project is no more interesting
    # here than in the queue. Fetched wider than shown so the cut still fills it.
    if not show_empty:
        ucounts = _open_counts_for_units(upcoming)
        upcoming = [p for p in upcoming if ucounts.get(p.pk)]
    return render_template(
        "review/index.html",
        queue=queue,
        open_counts=open_counts,
        due_counts=due_counts,
        upcoming=upcoming[:10],
        show_empty=show_empty,
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
