"""Tasks → the WebDAV share, as Markdown files with their comment history.

This rides along with the notes mirror (:mod:`application.services.notes_sync`
— same share, same round, same credentials) and fills one reserved folder at
the root: ``Tasks``. Under it the project tree repeats itself, and every task
the user owns is one file: its note, then its comments, oldest first. The
thread that lives in Cairn is then also readable in DEVONthink, in Obsidian, or
with grep.

**This half is a copy, not a sync.** Nothing under ``Tasks/`` is read back: a
task's state (status, due date, assignee) is decided in Cairn and by the apps
on its API, and a file that could also decide it would need conflict rules for
something nobody edits in a text editor. So the notes round steps over this
whole subtree — a file here never becomes a note — and an edit made over there
is overwritten on the next round.

The one deletion rule
---------------------
A file under ``Tasks/`` that no live task claims any more — the task was
trashed, or swept by the retention cleanup — is **moved into**
``Tasks/Archive`` and forgotten: the task's ``mirror_path`` is cleared, so no
later round touches that file again. Nothing under ``Tasks/`` is deleted, ever.
That is the point of the folder: a task that goes away in Cairn does not take
its comment history off the NAS with it.

Archiving is a MOVE, so the guard the notes half needs (an empty listing must
not read as a mass deletion) has nothing to protect here: only a file that is
actually there and that no live task claims is ever moved, and the move loses
nothing.
"""
from __future__ import annotations

import posixpath
from datetime import datetime, timezone

from application.models.item import Item
from application.services.webdav import DavError

TASKS_DIR = "Tasks"
ARCHIVE_DIR = f"{TASKS_DIR}/Archive"
INBOX_DIR = "Inbox"
MD = ".md"

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def owns(path: str) -> bool:
    """Is ``path`` inside the tasks subtree? The notes round asks this to keep
    its hands off — otherwise every task file would be adopted as a note."""
    return path == TASKS_DIR or path.startswith(f"{TASKS_DIR}/")


def _ref_id(item, field: str) -> str:
    """The id in a reference field WITHOUT dereferencing it. ``item.parent``
    would fetch the document; this reads the id that is already there — a
    ``DBRef``, an ``ObjectId`` or (once something else touched it) the loaded
    document, whichever mongoengine happens to be holding."""
    ref = item._data.get(field)
    if ref is None:
        return ""
    return str(getattr(ref, "id", None) or getattr(ref, "pk", None) or ref)


def _aware(dt):
    if dt is None:
        return _EPOCH
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _stamp(dt, fmt="%Y-%m-%d %H:%M") -> str:
    return _aware(dt).astimezone(timezone.utc).strftime(fmt) if dt else ""


def container_paths(user) -> dict:
    """``{project or folder id: "Work/Acme/Website"}`` for the project tree.

    Sections resolve to their project — they are an intra-project detail, and a
    directory per section would bury the files one level deeper for nothing.
    """
    from application.services.notes_sync import safe_name

    nodes = {
        str(c.pk): c
        for c in Item.objects(owner=user, kind__in=["project", "folder", "section"])
        .only("pk", "title", "parent", "kind")
    }
    out: dict = {}

    def walk(cid: str) -> str:
        if cid in out:
            return out[cid]
        node = nodes.get(cid)
        if node is None:
            return ""
        out[cid] = ""                       # cycle guard, replaced below
        parent = walk(_ref_id(node, "parent"))
        if node.kind == "section":
            out[cid] = parent
        else:
            name = safe_name(node.title or "Untitled")
            out[cid] = f"{parent}/{name}" if parent else name
        return out[cid]

    for cid in nodes:
        walk(cid)
    return out


def dir_for(task, paths: dict, task_parents: dict) -> str:
    """Which directory a task's file belongs in.

    A subtask files with the project it hangs under, not in a folder of its
    parent task: the parent is named in the file's front matter, and a
    directory per task turns a checklist into a maze.
    """
    seen: set[str] = set()
    cid = _ref_id(task, "parent")
    while cid and cid not in seen:
        seen.add(cid)
        if cid in paths:
            return f"{TASKS_DIR}/{paths[cid]}" if paths[cid] else TASKS_DIR
        cid = task_parents.get(cid, "")     # a parent task: keep walking up
    return f"{TASKS_DIR}/{INBOX_DIR}"


def render(task, people: dict) -> str:
    """The whole file: front matter, the task's note, then its comments.

    ``people`` maps user id → display name, resolved in one query by the
    caller — a comment thread must not cost a lookup per line.
    """
    tags = ", ".join(task.tags or [])
    title = task.title or "Untitled task"
    head = [
        "---",
        f"cairn-id: {task.pk}",
        f"title: {title}",
        f"status: {task.status or ''}",
        f"tags: [{tags}]",
    ]
    if task.due_date:
        head.append(f"due: {_stamp(task.due_date, '%Y-%m-%d')}")
    assignee = people.get(_ref_id(task, "assigned_to"), "")
    if assignee:
        head.append(f"assignee: {assignee}")
    if task.flagged:
        head.append("flagged: true")
    head += [
        f"created: {_stamp(task.created_at)}",
        f"updated: {_stamp(task.updated_at)}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    body = (task.note or "").strip()
    if body:
        head += [body, ""]

    comments = sorted(task.comments or [], key=lambda c: _aware(c.created_at))
    if comments:
        head += ["## Comments", ""]
        for c in comments:
            who = people.get(_ref_id(c, "author"), "") or c.external_author or "Someone"
            head += [f"### {who} — {_stamp(c.created_at)}", "",
                     (c.body or "").strip(), ""]
    return "\n".join(head)


def sync(round_) -> None:
    """Write every task's file, then archive the ones nothing claims.

    Takes the notes round (for the client, the stats and the dry-run flag)
    rather than opening a second connection: it is one share and one pass
    over it.
    """
    from application.services.notes_sync import safe_name

    user, stats = round_.user, round_.stats
    present = _listing(round_)
    paths = container_paths(user)
    tasks = list(Item.objects(owner=user, kind="task"))
    task_parents = {str(t.pk): _ref_id(t, "parent") for t in tasks}
    people = _people_for(tasks)

    claimed: set[str] = set()
    taken: set[str] = set()
    # By id, which for an ObjectId is creation order: a task that already owns
    # a name keeps it instead of two same-named tasks swapping files each round.
    for task in sorted(tasks, key=lambda t: str(t.pk)):
        directory = dir_for(task, paths, task_parents)
        stem = safe_name(task.title or "Untitled task")
        want = f"{directory}/{stem}{MD}"
        if want.lower() in taken:
            want = f"{directory}/{stem} ({str(task.pk)[-6:]}){MD}"
        taken.add(want.lower())
        claimed.add(want)
        try:
            _one(round_, task, want, render(task, people), present)
        except DavError as exc:
            stats.errors.append(f"{task.title}: {exc}")

    _archive(round_, sorted(present - claimed))


def _listing(round_) -> set:
    """The task files that are on the share right now, ``Archive`` aside."""
    try:
        entries = round_.client.walk(TASKS_DIR)
    except DavError as exc:
        if exc.status == 404:
            return set()                    # nothing mirrored yet
        round_.stats.errors.append(f"{TASKS_DIR}: {exc}")
        return set()
    return {e.path for e in entries
            if not e.is_dir and e.path.lower().endswith(MD)
            and not e.path.startswith(f"{ARCHIVE_DIR}/")}


def _people_for(tasks) -> dict:
    """``{user id: display name}`` for every assignee and comment author."""
    from application.models.user import User

    ids = set()
    for task in tasks:
        ids.add(_ref_id(task, "assigned_to"))
        for c in task.comments or []:
            ids.add(_ref_id(c, "author"))
    ids.discard("")
    if not ids:
        return {}
    return {
        str(u.pk): (u.display_name or u.username or "")
        for u in User.objects(pk__in=list(ids)).only("pk", "username", "display_name")
    }


def _one(round_, task, want: str, text: str, present: set) -> None:
    """One task's file — written only when it would say something new, or when
    it isn't there any more (someone tidied the share)."""
    from application.services.notes_sync import _hash

    digest = _hash(text)
    was = task.mirror_path or ""
    if was == want and task.mirror_hash == digest and want in present:
        round_.stats.unchanged += 1
        return
    if round_.dry_run:
        round_.stats.tasks_written += 1
        return
    round_.client.write(want, text.encode("utf-8"), "text/markdown; charset=utf-8")
    round_.stats.tasks_written += 1
    if was and was != want and owns(was) and was in present:
        # Renamed, or moved to another project: one file per task, so the old
        # one goes. It is a copy of this same task, not history.
        try:
            round_.client.delete(was)
            present.discard(was)
        except DavError as exc:
            round_.stats.errors.append(f"{was}: {exc}")
    Item.all_objects(pk=task.pk).update(set__mirror_path=want, set__mirror_hash=digest)
    task.mirror_path, task.mirror_hash = want, digest


def _archive(round_, stray: list) -> None:
    """Move what no live task claims into ``Tasks/Archive`` — and forget it."""
    stats = round_.stats
    if not stray:
        return
    # The tasks still pointing at these files (trashed ones, mostly): the
    # bookkeeping has to go, or restoring one would write the file again while
    # the archived copy sits beside it.
    owners = {t.mirror_path: t for t in Item.all_objects(
        owner=round_.user, kind="task", mirror_path__in=stray).only("pk", "mirror_path")}

    for path in stray:
        target = f"{ARCHIVE_DIR}/{path[len(TASKS_DIR) + 1:]}"
        try:
            if not round_.dry_run:
                round_.client.move(path, _free_name(round_.client, target))
        except DavError as exc:
            stats.errors.append(f"{path}: {exc}")
            continue
        stats.tasks_archived += 1
        task = owners.get(path)
        if task is not None and not round_.dry_run:
            Item.all_objects(pk=task.pk).update(set__mirror_path="", set__mirror_hash="")


def _free_name(client, target: str) -> str:
    """``target``, or the next free ``… (2).md`` beside it. The archive keeps
    what it is given: a second task of the same name never overwrites the first
    one's history."""
    try:
        have = {e.path.lower() for e in client.list(posixpath.dirname(target))}
    except DavError:
        return target
    if target.lower() not in have:
        return target
    stem = target[: -len(MD)]
    n = 2
    while f"{stem} ({n}){MD}".lower() in have:
        n += 1
    return f"{stem} ({n}){MD}"
