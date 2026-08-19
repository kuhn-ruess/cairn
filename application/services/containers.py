"""Hierarchical container pickers.

Shared by the task inspector ("Project") and the project/folder inspector
("Inside"): both need a ``<select>`` whose option labels carry the full folder
breadcrumb (e.g. ``📁  Work / Acme / Website``) instead of a bare name, so a
project nested several folders deep is unambiguous.

One query fetches every project/folder/section in scope; ancestor paths resolve
in-memory through a pk→item map (``.pk`` on a reference reads the DBRef, not the
document, so there are no per-row lookups).
"""
from mongoengine import Q

from application.models.item import Item

_KIND_ICON = {"folder": "🗂️", "section": "🔖", "project": "📁"}


def container_choices(user, *, selectable_kinds=("project", "folder"),
                      include_shared=False, active_projects_only=False,
                      exclude_pk=None):
    """Return ``[(pk, "📁  Folder / Sub / Project"), …]`` for a container pick.

    ``selectable_kinds`` are the kinds actually offered as options; the others
    are still fetched so a candidate's path can resolve past them. Options are
    sorted by their breadcrumb label.

    - ``include_shared`` — also include items shared with the user (members),
      not just owned ones.
    - ``active_projects_only`` — drop projects that aren't ``status="active"``
      (a task can only move into an active project).
    - ``exclude_pk`` — drop this item (so a project can't become its own
      ancestor when editing).
    """
    if include_shared:
        # Every container the user may OPEN — owned, directly shared (members /
        # team), inherited from a shared ancestor, or under a subtree a team
        # admin manages. Since ``can_be_edited_by == can_be_viewed_by``, this is
        # also exactly the set they may create/file inside.
        from bson import ObjectId
        from application.services.permissions import viewable_container_pks
        scope = Q(pk__in=[ObjectId(p) for p in viewable_container_pks(user)])
    else:
        scope = Q(owner=user)
    containers = list(
        # ``folder_domain != "notes"`` keeps the notes-tree folders out of the
        # project/task container pickers (a note folder is never a valid parent
        # for a project or task). ``$ne`` also matches pre-notes folders that
        # have no ``folder_domain`` field at all.
        Item.objects(scope, kind__in=["project", "folder", "section"],
                     folder_domain__ne="notes")
        # ``name`` is a property alias for ``title`` — project the real field
        # or mongoengine raises LookUpError on .only().
        .only("pk", "title", "kind", "parent", "status")
    )
    by_id = {str(c.pk): c for c in containers}

    def path_label(c):
        parts, cur, seen = [], c, set()
        while cur is not None and str(cur.pk) not in seen:
            seen.add(str(cur.pk))
            parts.append(cur.name)
            par = cur.parent
            cur = by_id.get(str(par.pk)) if par else None
        parts.reverse()
        return f"{_KIND_ICON.get(c.kind, '📁')}  " + " / ".join(parts)

    def offered(c):
        if c.kind not in selectable_kinds:
            return False
        if exclude_pk is not None and c.pk == exclude_pk:
            return False
        if active_projects_only and c.kind == "project" and c.status != "active":
            return False
        return True

    selectable = sorted((c for c in containers if offered(c)),
                        key=lambda c: path_label(c).lower())
    return [(str(c.pk), path_label(c)) for c in selectable]


def notes_folder_choices(user):
    """``[(pk, "🗂️  Clients / Acme"), …]`` over the *notes* tree.

    The counterpart to :func:`container_choices` for the notes navigator, used
    by the project/folder inspector to bind a project to its notes folder.
    Same one-query + in-memory breadcrumb approach."""
    from application.services.permissions import container_visibility_q
    folders = list(
        Item.objects(container_visibility_q(user), kind="folder",
                     folder_domain="notes").only("pk", "title", "parent")
    )
    by_id = {str(f.pk): f for f in folders}

    def path_label(f):
        parts, cur, seen = [], f, set()
        while cur is not None and str(cur.pk) not in seen:
            seen.add(str(cur.pk))
            parts.append(cur.name)
            par = cur.parent
            cur = by_id.get(str(par.pk)) if par else None
        parts.reverse()
        return "🗂️  " + " / ".join(parts)

    rows = sorted(folders, key=lambda f: path_label(f).lower())
    return [(str(f.pk), path_label(f)) for f in rows]


def resolve_notes_folder(item):
    """The notes folder ``item`` belongs to — its own link, or the nearest
    ancestor's. Mirrors ``services.timetracking.resolve_time_project``: a task
    inherits the notes folder of its project/folder, so "open the notes for
    what I'm working on" works from anywhere in the subtree. Bounded and
    cycle-guarded; a deleted folder resolves to ``None`` instead of raising."""
    seen, node, depth = set(), item, 0
    while node is not None and depth < 50:
        if node.pk in seen:
            break
        seen.add(node.pk)
        nf = getattr(node, "notes_folder", None)
        if nf:
            try:
                return nf if nf.kind == "folder" else None
            except Exception:
                return None
        node = node.parent
        depth += 1
    return None
