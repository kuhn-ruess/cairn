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
