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


def describe_containers(items, team=None):
    """Where each container lives, and — when that's a private place — whose.

    ``items`` are containers that may belong to *anyone* (the time-tracking page
    lists every folder linked to a billing project, and colleagues link their
    own). For each one, returns ``{path, owner, shared}``:

    * ``path``  — the full breadcrumb, resolved through ancestors that the
      viewer may not own or even be able to open.
    * ``shared`` — whether it (or an ancestor) is shared with ``team``. A
      container that isn't sits in someone's private area, which is exactly when
      knowing the ``owner`` matters: the billing project draws time out of a
      place nobody else on the team can see.

    Ancestors are fetched level by level and owners in one go, so the whole
    description costs a handful of queries no matter how many items come in.
    ``all_objects`` throughout — a trashed ancestor still has to name the path.
    """
    items = list(items or [])
    if not items:
        return {}

    known = {str(i.pk): i for i in items}
    frontier = [i.parent.pk for i in items if i.parent]
    depth = 0
    while frontier and depth < 20:
        missing = [pk for pk in frontier if str(pk) not in known]
        if not missing:
            break
        parents = list(Item.all_objects(pk__in=missing)
                       .only("pk", "title", "kind", "parent", "owner", "shared_teams"))
        if not parents:
            break
        for p in parents:
            known[str(p.pk)] = p
        frontier = [p.parent.pk for p in parents if p.parent]
        depth += 1

    owner_ids = {getattr(i._data.get("owner"), "pk", None)
                 or getattr(i._data.get("owner"), "id", None)
                 for i in known.values() if i._data.get("owner") is not None}
    owner_ids.discard(None)
    names = {}
    if owner_ids:
        from application.models.user import User
        names = {str(u.pk): (u.display_name or u.username or "")
                 for u in User.objects(pk__in=list(owner_ids))
                 .only("pk", "display_name", "username")}

    team_id = str(team.pk) if team is not None else None

    def ref_id(ref):
        # Un-dereferenced references come back as raw DBRefs (``.id``), already
        # loaded ones as Documents (``.pk``). Both mean the same thing here.
        return str(getattr(ref, "pk", None) or getattr(ref, "id", None) or "")

    def chain(item):
        out, cur, seen = [], item, set()
        while cur is not None and str(cur.pk) not in seen:
            seen.add(str(cur.pk))
            out.append(cur)
            par = cur.parent
            cur = known.get(str(par.pk)) if par else None
        return out

    described = {}
    for item in items:
        line = chain(item)
        shared = team_id is not None and any(
            any(ref_id(t) == team_id for t in (c._data.get("shared_teams") or []))
            for c in line
        )
        owner_ref = item._data.get("owner")
        described[str(item.pk)] = {
            "path": " / ".join(c.name for c in reversed(line)),
            "owner": names.get(ref_id(owner_ref), "") if owner_ref is not None else "",
            "shared": shared,
        }
    return described
