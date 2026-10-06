"""Capture — turn a marked block of text into tasks or into a note.

Both callers hand over the same thing: the lines a user marked with the mouse.
The scratchpad passes the selected part of its scratch text (or all of it),
a note passes what was marked in the rendered body.

* :func:`create_tasks` — **one task per line**, into the project the user
  picked. Indentation makes sub-tasks. Markdown noise a line may carry from
  wherever it was marked (bullet, ``[x]`` box, heading hashes, quote marker) is
  stripped, so marking a rendered checklist works as well as marking raw
  Markdown. Each line still runs through :func:`parse_task_text`, so the
  ``@person +tag #project p1 ! today`` tokens work — ``@bastian`` on a note
  line assigns the task and notifies him, exactly as ``@bastian`` in a comment
  mentions him — and an inline ``#project`` wins over the picked target for
  that one line.
* :func:`create_note` — the text becomes a Markdown note, verbatim.

Creating tasks is idempotent: a task with the same title already in the same
parent is updated instead of duplicated, so converting the same lines twice
doesn't double them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Leading Markdown a marked line may carry: a bullet, a numbered item, heading
# hashes, or a quote marker. Stripped repeatedly (``> - foo``), so the order in
# which they were nested doesn't matter. ``#`` needs the space — ``#project``
# without one is the quick-parse project token and must survive.
_LEAD_RE = re.compile(r"^(?:[-*+•]|\d+[.)]|#{1,6}|>)\s+")
# A checkbox, raw (``[ ]``/``[x]``) or as the rendered body renders it.
_BOX_RE = re.compile(r"^(?:\[[ xX]\]|[☐☑✓])\s*")


@dataclass
class TaskNode:
    title: str
    indent: int
    children: list = field(default_factory=list)


def _strip_markup(line: str) -> str:
    """A marked line reduced to its actual text."""
    s = line.strip()
    prev = None
    while prev != s:
        prev = s
        s = _LEAD_RE.sub("", s)
        s = _BOX_RE.sub("", s)
    return s.strip()


def parse_tasks(text: str) -> list[TaskNode]:
    """Parse marked text into a forest of ``TaskNode``s. Pure — no DB."""
    roots: list[TaskNode] = []
    stack: list[tuple[int, TaskNode]] = []

    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        title = _strip_markup(raw)
        if not title:
            continue
        indent = len(raw.expandtabs(4)) - len(raw.expandtabs(4).lstrip())
        node = TaskNode(title=title, indent=indent)
        # A shallower-or-equal line closes deeper open ones.
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append(node)
        stack.append((indent, node))
    return roots


def suggest_title(text: str, limit: int = 60) -> str:
    """A note title proposal: the first heading, else the first line."""
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        s = _strip_markup(raw)
        if s:
            return s[:limit].strip()
    return ""


def create_tasks(text: str, user, parent=None) -> dict:
    """Create the tasks in ``text`` under ``parent`` (None = Inbox).

    Returns ``{"tasks_created": n, "tasks_updated": n}``.
    """
    stats = {"tasks_created": 0, "tasks_updated": 0}
    for i, node in enumerate(parse_tasks(text)):
        _create_task(node, parent_item=parent, user=user, stats=stats, order=i)
    return stats


def create_note(text: str, user, *, title: str = "", folder=None):
    """Create a Markdown note holding ``text``. Returns the ``Item``."""
    from application.models.item import Item
    from application.services import note_links

    note = Item(
        owner=user, kind="note", parent=folder,
        title=(title or suggest_title(text) or "Scratchpad"),
        note=text or "",
    )
    if "[[" in note.note:
        note_links.rebuild(note, note_links.title_index(user))
    note.save()
    return note


def _create_task(tnode: TaskNode, parent_item, user, stats: dict, order: int,
                 nested: bool = False) -> None:
    from application.models.item import Item
    from application.services.quick_parse import parse_task_text

    parsed = parse_task_text(tnode.title, user)
    title = parsed.title or tnode.title
    tag_names = list(parsed.tags)
    # ``#project`` on the line beats the picked target — but only for a
    # top-level line; a sub-task belongs to the task above it.
    target = parent_item
    if not nested and parsed.project is not None:
        target = parsed.project

    # Converting the same lines twice updates instead of duplicating (see the
    # module docstring). The title match ignores the inline tokens because
    # ``parse_task_text`` already stripped them. Only tasks are candidates —
    # notes, sections and folders share the collection and must never be hit
    # by a conversion. A line that has children may also land on a project,
    # because that is what a container line turns into (``ensure_container``),
    # and because filing them under an existing project of that name is the
    # sensible reading of "Website / - do X".
    existing = Item.objects(
        owner=user, parent=target, title__iexact=title,
        kind__in=(["task", "project"] if tnode.children else ["task"]),
    ).first()

    if existing:
        # A matched project is only reused as the parent for the children —
        # rewriting a real project's date/priority from a dumped line would
        # be a conversion quietly editing something it didn't create.
        if existing.kind == "task":
            existing.due_date = parsed.due_date
            existing.priority = parsed.priority
            existing.flagged = parsed.flagged
            # Tags merge additively — a conversion never strips a tag the
            # user added elsewhere.
            for name in tag_names:
                if name not in existing.tags:
                    existing.tags.append(name)
            if parsed.assignee:
                existing.assign_to(parsed.assignee, by_user=user)
            existing.save()
        task = existing
        stats["tasks_updated"] += 1
    else:
        task = Item(
            owner=user,
            kind="task",
            title=title,
            parent=target,
            sort_order=order,
            tags=tag_names,
            priority=parsed.priority,
            due_date=parsed.due_date,
            flagged=parsed.flagged,
            status="open",
        )
        if parsed.assignee:
            task.assign_to(parsed.assignee, by_user=user)
        task.save()
        stats["tasks_created"] += 1

    if tnode.children:
        # Flip the parent task into a container so it can hold the sub-tasks.
        task.ensure_container()
        for i, child in enumerate(tnode.children):
            _create_task(child, parent_item=task, user=user, stats=stats,
                         order=i, nested=True)


# --- Project sketch --------------------------------------------------------
#
# The project page's Markdown view: the project typed as a nested list. The
# same parser as a capture, but a different contract for what already exists.
# A capture UPDATES a matching task (it re-applies the line's date, priority
# and tags); a sketch does not. A line whose title matches an item already
# sitting at that place in the tree is an anchor: the lines indented under it
# are filed beneath it, and its fields stay as they are, except that an
# ``@person`` typed on the line assigns the task and a ``+tag`` adds the tag
# (``_sketch_tokens``, only adding, never removing).
#
# The sketch does change WHERE a task is and WHETHER it is open:
#
# * A line of an open task moved to another place in the sketch (other
#   section, other parent) is that task, and saving MOVES it there, its
#   sub-tasks with it. Only a task whose line is NOT still at its own place
#   counts as moved, so the same title typed a second time elsewhere is a new
#   task. Sub-projects and folders are anchors only, never moved.
# * A task whose line the sketch showed when it was opened and that is gone
#   on save is CLOSED (``Item.complete``, never deleted). Only the ids the
#   page was opened with (``shown``) are candidates, so a task someone added
#   meanwhile, or a closed one the sketch never listed, is never touched.
#   Closing a task closes its open sub-tasks too (the model's cascade).
#
# The view runs a plan first (``apply=False``, nothing written) and asks for
# confirmation whenever a save would move or close anything.
# Nesting makes SUB-TASKS (the parent stays a task, the same rule as the
# inspector's "add subtask"), not sub-projects.

# What a sketch line may land on. Notes share the collection and are never a
# match; sections and sub-projects are, so a sketch can extend them.
_SKETCH_KINDS = ["task", "project", "section", "folder"]
_SKETCH_CLOSED = ["done", "dropped", "completed"]


class _Draft:
    """A task or section a plan would create: no pk, nothing under it yet."""

    def __init__(self, title, parent=None):
        self.title, self.parent, self.pk, self.kind = title, parent, None, "task"


def sketch_outline(project, shown: list | None = None) -> str:
    """The open part of ``project`` as a nested Markdown list.

    What the sketch view starts with, so a new line can be typed where it
    belongs. One query per tree depth. ``shown`` collects the pks of the
    tasks written as lines: the snapshot a save closes missing lines against.
    """
    from application.models.item import Item

    lines: list[str] = []
    children: dict = {}
    frontier = [project.pk]
    seen = set(frontier)
    while frontier:
        nxt = []
        for item in (Item.objects(parent__in=frontier, kind__in=_SKETCH_KINDS,
                                  status__nin=_SKETCH_CLOSED)
                     .no_dereference().order_by("+sort_order", "+created_at")):
            if item.pk in seen:
                continue
            seen.add(item.pk)
            raw = item._data.get("parent")
            parent_pk = getattr(raw, "id", None) or getattr(raw, "pk", None) or raw
            children.setdefault(parent_pk, []).append(item)
            nxt.append(item.pk)
        frontier = nxt

    def _walk(pk, depth):
        for item in children.get(pk, []):
            title = " ".join((item.title or "").split())
            if title and item.kind != "section":
                lines.append("  " * depth + "- " + title)
                if shown is not None and item.kind == "task":
                    shown.append(str(item.pk))
                _walk(item.pk, depth + 1)

    # The loose items first, then each section as a ``##`` heading with its
    # items as a list of their own: the order the project page shows them in.
    _walk(project.pk, 0)
    for section in children.get(project.pk, []):
        title = " ".join((section.title or "").split())
        if section.kind != "section" or not title:
            continue
        if lines:
            lines.append("")
        lines.append("## " + title)
        _walk(section.pk, 0)
    return "\n".join(lines)


def sketch_tasks(text: str, user, project, shown=None, apply: bool = True) -> dict:
    """Bring ``project`` in line with the sketch ``text``.

    New lines become tasks, a moved line moves its task, and a task in
    ``shown`` (the pks the sketch was opened with) whose line is gone is
    closed. ``shown=None`` closes nothing. ``apply=False`` writes nothing and
    returns what a save would do.

    A Markdown heading (``## Phase 1``) is a SECTION of the project: the list
    under it, up to the next heading, is filed into that section. A heading
    naming a section the project already has uses it, any other creates one.
    Only a project holds sections, so in a sketch of anything else a heading
    is an ordinary line.

    Returns the counts ``tasks_created``, ``tasks_skipped`` and
    ``sections_created``, and the titles ``created``, ``moved``, ``closed``
    (the missing lines), ``closed_with`` (open sub-tasks the sketch didn't
    list that close with their parent), ``refused`` (moves that would put
    a task under its own sub-task, left where they are) and ``updated``
    (existing tasks given a person or a tag typed on their line).
    """
    stats = {"tasks_created": 0, "tasks_skipped": 0, "sections_created": 0,
             "created": [], "moved": [], "closed": [], "closed_with": [],
             "refused": [], "updated": []}
    by_title, open_items = _sketch_open_items(project)
    # Two passes over the same text: the first ("scan") only notes which
    # existing items are still at their own place, the second files the
    # lines. A line that matches nothing at its place may then be an open
    # item moved there.
    ctx = {"mode": "scan", "claimed": set(), "moved": by_title, "kept": set(),
           "drafts": {}}
    for mode in ("scan", "apply" if apply else "plan"):
        ctx["mode"] = mode
        if project.kind != "project":
            _sketch_level(parse_tasks(text), project, user, stats, ctx)
            continue
        heading, block = None, []
        for raw in (text or "").splitlines() + ["# "]:
            m = _HEADING_RE.match(raw)
            if not m:
                block.append(raw)
                continue
            target = project if heading is None else _sketch_section(
                heading, project, user, stats, ctx)
            if target is not None:
                _sketch_level(parse_tasks("\n".join(block)), target, user,
                              stats, ctx)
            heading, block = (m.group(1) or None), []
    _sketch_close(shown, open_items, user, stats, ctx)
    return stats


def _sketch_open_items(project):
    """The open items under ``project`` (any depth): by casefolded title, and
    by pk.

    What the sketch view lists, so what a line may have been moved from and
    what a missing line may close. Sections are headings, not lines, and
    closed items aren't listed.
    """
    from application.models.item import Item

    found: dict = {}
    by_pk: dict = {}
    frontier, seen = [project.pk], {project.pk}
    while frontier:
        nxt = []
        for item in Item.objects(parent__in=frontier, kind__in=_SKETCH_KINDS,
                                 status__nin=_SKETCH_CLOSED):
            if item.pk in seen:
                continue
            seen.add(item.pk)
            nxt.append(item.pk)
            if item.kind != "section":
                key = (item.title or "").strip().casefold()
                found.setdefault(key, []).append(item)
                by_pk[str(item.pk)] = item
        frontier = nxt
    return found, by_pk


# A heading line: hashes, a space, a title. ``#project`` without the space is
# the quick-add project token and stays a list line.
_HEADING_RE = re.compile(r"^\s*#{1,6}(?:\s+(.*?))?(?:\s+#+)?\s*$")


def _sketch_section(title: str, project, user, stats, ctx):
    """The section ``title`` of ``project``: the existing one, else a new one.

    In the scan a section that doesn't exist yet is ``None``, in a plan a
    draft.
    """
    from application.models.item import Item

    title = " ".join(title.split())[:500]
    existing = Item.objects(parent=project, kind="section",
                            title__iexact=title).first()
    if existing is not None or ctx["mode"] == "scan":
        return existing
    if ctx["mode"] == "plan":
        # The same new heading twice is one section, as it will be on save.
        key = title.casefold()
        if key not in ctx["drafts"]:
            stats["sections_created"] += 1
            ctx["drafts"][key] = _Draft(title, project)
        return ctx["drafts"][key]
    stats["sections_created"] += 1
    last = Item.objects(parent=project).order_by("-sort_order").only(
        "sort_order").first()
    section = Item(
        owner=user, kind="section", title=title, parent=project,
        project_type="parallel", status="active", review_interval_days=0,
        sort_order=((last.sort_order or 0) + 1) if last else 0,
    )
    section.save()
    return section


def _sketch_level(nodes, parent, user, stats, ctx) -> None:
    from application.models.item import Item
    from application.services.quick_parse import parse_task_text

    if not nodes:
        return
    siblings = [] if parent.pk is None else list(
        Item.objects(parent=parent, kind__in=_SKETCH_KINDS)
        .only("title", "sort_order", "kind", "status"))
    # An open item wins over a closed one of the same title: the open one is
    # the line the sketch showed, and losing it to a closed twin would close it.
    by_title = {}
    for s in sorted(siblings, key=lambda s: s.status not in _SKETCH_CLOSED):
        by_title[(s.title or "").strip().casefold()] = s
    # New lines go after what is already there, in the order they were typed.
    order = max((s.sort_order or 0 for s in siblings), default=-1) + 1

    for node in nodes:
        parsed = parse_task_text(node.title, user)
        title = (parsed.title or node.title)[:500]
        # The raw line counts too: an existing title that happens to contain
        # a token (``today``, ``p1``) loses it to the parser and would
        # otherwise come back as a second task.
        keys = (node.title.strip().casefold(), title.strip().casefold())
        existing = by_title.get(keys[0]) or by_title.get(keys[1])
        if ctx["mode"] == "scan":
            # First pass: only note what is still at its own place.
            if existing is not None:
                ctx["claimed"].add(existing.pk)
                _sketch_level(node.children, existing, user, stats, ctx)
            continue
        if existing is None:
            existing = _sketch_moved(keys, ctx)
            if existing is not None and _sketch_move(existing, parent, order,
                                                     stats, ctx):
                order += 1
        if existing is not None:
            ctx["kept"].add(existing.pk)
            stats["tasks_skipped"] += 1
            if node.title.strip().casefold() != (existing.title or "").strip().casefold():
                _sketch_tokens(existing, parsed, user, stats, ctx)
            _sketch_level(node.children, existing, user, stats, ctx)
            continue
        stats["tasks_created"] += 1
        stats["created"].append(title)
        if ctx["mode"] == "plan":
            _sketch_level(node.children, _Draft(title, parent), user, stats, ctx)
            continue
        task = Item(
            owner=user, kind="task", title=title, parent=parent,
            sort_order=order, tags=list(parsed.tags),
            priority=parsed.priority, due_date=parsed.due_date,
            flagged=parsed.flagged, status="open",
        )
        if parsed.assignee:
            task.assign_to(parsed.assignee, by_user=user)
        task.save()
        order += 1
        by_title[title.strip().casefold()] = task
        _sketch_level(node.children, task, user, stats, ctx)


def _sketch_tokens(item, parsed, user, stats, ctx) -> None:
    """Hand an existing task the ``@person`` and ``+tag`` typed on its line.

    Only those two tokens, and only adding: a tag is added, never removed, and
    a person is assigned, never unassigned (the sketch doesn't show who has a
    task, so a missing ``@`` says nothing). Date, priority and flag stay as
    they are. Only called for a line that differs from the task's title, so
    a title that itself contains an ``@`` or ``+`` changes nothing.
    """
    from application.models.item import Item

    if item.kind != "task" or not (parsed.tags or parsed.assignee):
        return
    # The siblings were read with ``only()``: the tags and the assignee are
    # read again in full.
    task = Item.objects(pk=item.pk).first()
    if task is None:
        return
    tags = [t for t in parsed.tags if t not in (task.tags or [])]
    def _pk(field):
        raw = task._data.get(field)
        return getattr(raw, "id", None) or getattr(raw, "pk", None) or raw

    person = parsed.assignee
    current = _pk("assigned_to")
    # Already theirs, or the owner on a task nobody else has: nothing to do.
    if person is not None and (person.pk == current or (
            current is None and person.pk == _pk("owner"))):
        person = None
    if not tags and person is None:
        return
    stats["updated"].append(task.title)
    if ctx["mode"] != "apply":
        return
    task.tags = list(task.tags or []) + tags
    if person is not None:
        task.assign_to(person, by_user=user)
    task.save()


def _sketch_moved(keys, ctx):
    """The open item a line was moved away from, if there is one.

    Only an item whose own line is gone from its place qualifies, and each
    one is taken once, so a title typed twice is one anchor and one new task.
    """
    for key in keys:
        for item in ctx["moved"].get(key, []):
            if item.pk not in ctx["claimed"]:
                ctx["claimed"].add(item.pk)
                return item
    return None


def _sketch_move(item, parent, order, stats, ctx) -> bool:
    """Move the task ``item`` under ``parent`` (in a plan: only count it).

    Sub-projects and folders stay where they are. A task never goes under
    its own sub-task: that move is reported in ``refused`` and not made.
    Returns whether the task moved.
    """
    raw = item._data.get("parent")
    current = getattr(raw, "id", None) or getattr(raw, "pk", None) or raw
    if item.kind != "task" or (parent.pk is not None and current == parent.pk):
        return False
    node, depth = parent, 0
    while node is not None and depth < 50:
        if node.pk == item.pk:
            stats["refused"].append(item.title)
            return False
        node, depth = node.parent, depth + 1
    stats["moved"].append(item.title)
    if ctx["mode"] == "apply":
        item.parent = parent
        item.sort_order = order
        item.save()
    return True


def _sketch_close(shown, open_items, user, stats, ctx) -> None:
    """Close the tasks the sketch showed whose line is gone.

    ``Item.complete`` closes a task's open sub-tasks with it, so those the
    sketch didn't list (added meanwhile) are named in ``closed_with`` for
    the confirmation. A sub-task whose line was kept was moved out first.
    """
    from application.models.item import Item

    gone = []
    for pk in dict.fromkeys(shown or []):
        item = open_items.get(pk)
        if item is not None and item.kind == "task" and item.pk not in ctx["kept"]:
            gone.append(item)
    gone_pks = {i.pk for i in gone}
    frontier = [i.pk for i in gone]
    while frontier:
        kids = list(Item.objects(parent__in=frontier, kind="task",
                                 status__nin=_SKETCH_CLOSED).only("title"))
        kids = [k for k in kids if k.pk not in ctx["kept"]]
        stats["closed_with"] += [k.title for k in kids if k.pk not in gone_pks]
        frontier = [k.pk for k in kids if k.pk not in gone_pks]
    for item in gone:
        stats["closed"].append(item.title)
        if ctx["mode"] != "apply":
            continue
        item.reload()
        if item.status not in _SKETCH_CLOSED:
            item.complete(by_user=user)
