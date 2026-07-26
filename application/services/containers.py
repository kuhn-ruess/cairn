"""Hierarchical container pickers.

Shared by the task inspector ("Project") and the project/folder inspector
("Inside"): both need a ``<select>`` whose option labels disambiguate a project
nested several folders deep. The identifying name leads, with the ancestor
breadcrumb trailing as muted context (e.g. ``📁  Website — Work / Acme``) — the
selects are narrow and a native ``<option>`` clips on the RIGHT, so a
path-first label (``Work / Acme / Website``) hid exactly the part that matters.
Options sort by that leading name, so scanning/typing finds a project by title.

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

    def ancestor_path(c):
        """Breadcrumb of c's ancestors (root→parent), leaf excluded."""
        parts, cur, seen = [], c.parent, {str(c.pk)}
        cur = by_id.get(str(cur.pk)) if cur else None
        while cur is not None and str(cur.pk) not in seen:
            seen.add(str(cur.pk))
            parts.append(cur.name)
            par = cur.parent
            cur = by_id.get(str(par.pk)) if par else None
        parts.reverse()
        return " / ".join(parts)

    def sort_key(c):
        # Sort by leading name, then ancestor path so same-named siblings stay
        # grouped by where they live.
        return (c.name.lower(), ancestor_path(c).lower())

    def path_label(c):
        icon = _KIND_ICON.get(c.kind, "📁")
        crumb = ancestor_path(c)
        label = f"{icon}  {c.name}"
        return f"{label} — {crumb}" if crumb else label

    def offered(c):
        if c.kind not in selectable_kinds:
            return False
        if exclude_pk is not None and c.pk == exclude_pk:
            return False
        if active_projects_only and c.kind == "project" and c.status != "active":
            return False
        return True

    selectable = sorted((c for c in containers if offered(c)), key=sort_key)
    return [(str(c.pk), path_label(c)) for c in selectable]
