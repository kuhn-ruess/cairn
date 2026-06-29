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
        from application.services.permissions import teams_of
        scope = Q(owner=user) | Q(members=user) | Q(shared_teams__in=teams_of(user))
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
