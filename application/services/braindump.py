"""Braindump → projects/tasks converter.

Turns a free-form Markdown outline into real ``Item`` documents:

* a Markdown **heading** (``#``…``######``) becomes a **project**. Deeper
  headings nest as sub-projects of the nearest shallower heading.
* a **list item** (``-``/``*``/``+`` or ``1.``) becomes a **task** in the
  current heading's project. Indented list items become sub-tasks (their
  parent task is flipped to a container via ``Item.ensure_container``).
* list items written before any heading land in the Inbox (no project).

Existing projects are reused: a heading whose title matches one of the
user's projects/folders (case-insensitive) attaches its tasks to that
project instead of creating a duplicate.

Each task line is run through :func:`parse_task_text`, so the Todoist-style
``@tag #project p1 ! today`` tokens work inside the dump too.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# A heading: one-to-six leading ``#`` then the title.
HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# A list item: optional indentation, a bullet (-,*,+) or ``1.`` / ``1)``.
LIST_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.+?)\s*$")


@dataclass
class TaskNode:
    title: str
    indent: int
    children: list = field(default_factory=list)


@dataclass
class ProjectNode:
    title: str
    level: int  # heading depth 1-6; 0 = synthetic Inbox bucket
    tasks: list = field(default_factory=list)      # top-level TaskNodes
    children: list = field(default_factory=list)   # nested ProjectNodes


def parse_braindump(text: str) -> list[ProjectNode]:
    """Parse Markdown into a forest of ``ProjectNode``s. Pure — no DB."""
    roots: list[ProjectNode] = []
    header_stack: list[ProjectNode] = []
    inbox: ProjectNode | None = None
    task_stack: list[tuple[int, TaskNode]] = []

    for raw in (text or "").splitlines():
        if not raw.strip():
            continue

        hm = HEADER_RE.match(raw)
        if hm:
            level = len(hm.group(1))
            node = ProjectNode(title=hm.group(2).strip(), level=level)
            # Nest under the nearest shallower heading.
            while header_stack and header_stack[-1].level >= level:
                header_stack.pop()
            if header_stack:
                header_stack[-1].children.append(node)
            else:
                roots.append(node)
            header_stack.append(node)
            task_stack = []  # tasks don't cross a heading boundary
            continue

        lm = LIST_RE.match(raw)
        if lm:
            indent = len(lm.group(1).expandtabs(4))
            node = TaskNode(title=lm.group(2).strip(), indent=indent)
            # A shallower-or-equal item closes deeper open items.
            while task_stack and task_stack[-1][0] >= indent:
                task_stack.pop()
            if task_stack:
                task_stack[-1][1].children.append(node)
            elif header_stack:
                header_stack[-1].tasks.append(node)
            else:
                if inbox is None:
                    inbox = ProjectNode(title="", level=0)
                inbox.tasks.append(node)
            task_stack.append((indent, node))
            continue
        # Anything else (prose, blank-ish lines) is ignored.

    result: list[ProjectNode] = []
    if inbox and inbox.tasks:
        result.append(inbox)
    result.extend(roots)
    return result


def apply_braindump(text: str, user) -> dict:
    """Parse ``text`` and create the projects/tasks for ``user``.

    Returns a stats dict: ``projects_created``, ``projects_matched``,
    ``tasks_created``.
    """
    stats = {
        "projects_created": 0, "projects_matched": 0,
        "tasks_created": 0, "tasks_updated": 0,
    }
    for pnode in parse_braindump(text):
        _create_project(pnode, parent_item=None, user=user, stats=stats)
    return stats


def _create_project(pnode: ProjectNode, parent_item, user, stats: dict) -> None:
    from application.models.item import Item

    # Inbox bucket — tasks with no project.
    if pnode.level == 0:
        for i, tnode in enumerate(pnode.tasks):
            _create_task(tnode, parent_item=None, user=user, stats=stats, order=i)
        return

    # Reuse an existing project/folder with the same name; else create one.
    project = Item.objects(
        owner=user, kind__in=["project", "folder"], title__iexact=pnode.title,
    ).first()
    if project:
        stats["projects_matched"] += 1
    else:
        project = Item(
            owner=user, kind="project", title=pnode.title,
            status="active", parent=parent_item,
        )
        project.save()
        stats["projects_created"] += 1

    for i, tnode in enumerate(pnode.tasks):
        _create_task(tnode, parent_item=project, user=user, stats=stats, order=i)
    for child in pnode.children:
        _create_project(child, parent_item=project, user=user, stats=stats)


def _create_task(tnode: TaskNode, parent_item, user, stats: dict, order: int) -> None:
    from application.models.item import Item
    from application.services.quick_parse import parse_task_text

    parsed = parse_task_text(tnode.title, user)
    title = parsed.title or tnode.title
    tag_names = [t.name for t in parsed.tags]

    # Re-running the dump is idempotent: a task with the same title in the
    # same project is updated rather than duplicated — so editing e.g.
    # "today" → "tomorrow" and converting again moves the existing task's
    # due date instead of creating a second copy. Title match ignores the
    # inline tokens because ``parse_task_text`` already strips them.
    existing = Item.objects(
        owner=user, parent=parent_item, kind__ne="folder", title__iexact=title,
    ).first()

    if existing:
        existing.due_date = parsed.due_date
        existing.priority = parsed.priority
        existing.flagged = parsed.flagged
        # Tags are merged additively — the dump never strips a tag the user
        # added elsewhere.
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
            parent=parent_item,
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
            _create_task(child, parent_item=task, user=user, stats=stats, order=i)
