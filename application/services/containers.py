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
        # The notes-tree folders are fetched too, only so that what hangs
        # underneath one can be recognised and left out (see ``in_notes``).
        Item.objects(scope, kind__in=["project", "folder", "section"])
        # ``name`` is a property alias for ``title`` — project the real field
        # or mongoengine raises LookUpError on .only().
        .only("pk", "title", "kind", "parent", "status", "folder_domain")
        # Parents are looked up in ``by_id``; a dereference here would be one
        # query per container, on every page (the quick-add dialog).
        .no_dereference()
    )
    by_id = {str(c.pk): c for c in containers}

    def in_notes(c):
        """True for a folder of the notes tree AND for anything below one.
        A note folder is never a valid parent for a project or task, and a
        task-tree container that ended up inside the notes tree (the project
        and API create paths don't check the parent's domain) is shown by
        neither navigator, so offering it only files work out of sight.
        A folder without the field (older than the notes) counts as tasks."""
        cur, seen = c, set()
        while cur is not None and str(cur.pk) not in seen:
            if cur.kind == "folder" and cur.folder_domain == "notes":
                return True
            seen.add(str(cur.pk))
            par = _ref_pk(cur.parent)
            cur = by_id.get(str(par)) if par else None
        return False

    def path_label(c):
        parts, cur, seen = [], c, set()
        while cur is not None and str(cur.pk) not in seen:
            seen.add(str(cur.pk))
            parts.append(cur.name)
            par = _ref_pk(cur.parent)
            cur = by_id.get(str(par)) if par else None
        parts.reverse()
        return f"{_KIND_ICON.get(c.kind, '📁')}  " + " / ".join(parts)

    def offered(c):
        if c.kind not in selectable_kinds or in_notes(c):
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
    cycle-guarded; a folder that is gone resolves to ``None`` instead of
    raising — TRASHED as well as purged. Dereferencing a reference bypasses the
    ``objects`` manager, so without that check a project would keep its Notes
    button and walk straight into a folder the user had just thrown away."""
    seen, node, depth = set(), item, 0
    while node is not None and depth < 50:
        if node.pk in seen:
            break
        seen.add(node.pk)
        nf = getattr(node, "notes_folder", None)
        if nf:
            try:
                return nf if nf.kind == "folder" and not nf.trashed_at else None
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


def container_paths(items):
    """``{str(item.pk): "Folder / Sub / Project"}`` — where each task is filed.

    The label half of :func:`container_accents`; see there for the walk.
    """
    return {pk: info["label"] for pk, info in container_accents(items).items()}


def container_accents(items):
    """``{str(item.pk): {"pk", "label", "color"}}`` — the container a task is
    filed in, its breadcrumb, and the colour that applies to it.

    The owning project or folder, for a whole list in a handful of queries:
    ancestors level by level, no dereference per row. A section is walked past
    (it is inline structure, not a place), and a task filed nowhere is absent
    from the result — the caller decides what "Inbox" is called.

    ``color`` is **inherited**: the nearest ancestor carrying one wins, so a
    colour set on the "Acme" folder covers every project under it — the same
    climb ``customer_labels`` makes for the customer itself. ``""`` when nobody
    up the chain ever picked one; deriving a colour from that is
    ``services.colors``' business, not this one's.
    """
    items = list(items or [])
    if not items:
        return {}

    known = {}
    frontier = [pk for pk in (_ref_pk(i._data.get("parent")) for i in items) if pk]
    depth = 0
    while frontier and depth < 20:
        missing = [pk for pk in frontier if str(pk) not in known]
        if not missing:
            break
        parents = list(Item.all_objects(pk__in=missing)
                       .no_dereference()
                       .only("pk", "title", "kind", "parent", "color"))
        if not parents:
            break
        for p in parents:
            known[str(p.pk)] = p
        frontier = [pk for pk in (_ref_pk(p._data.get("parent")) for p in parents) if pk]
        depth += 1

    def owning(node):
        """Climb to the nearest project/folder (past sections and sub-tasks)."""
        seen, steps = set(), 0
        while node is not None and steps < 20 and str(node.pk) not in seen:
            seen.add(str(node.pk))
            if node.kind in ("project", "folder"):
                return node
            node = known.get(str(_ref_pk(node._data.get("parent")) or ""))
            steps += 1
        return None

    def path_of(node):
        parts, seen, steps = [], set(), 0
        while node is not None and steps < 20 and str(node.pk) not in seen:
            seen.add(str(node.pk))
            if node.kind in ("project", "folder"):
                parts.append(node.name)
            node = known.get(str(_ref_pk(node._data.get("parent")) or ""))
            steps += 1
        return " / ".join(reversed(parts))

    def color_of(node):
        """The nearest colour up the chain, starting at the container itself."""
        seen, steps = set(), 0
        while node is not None and steps < 20 and str(node.pk) not in seen:
            seen.add(str(node.pk))
            if (node._data.get("color") or "").strip():
                return node._data["color"].strip()
            node = known.get(str(_ref_pk(node._data.get("parent")) or ""))
            steps += 1
        return ""

    out = {}
    for it in items:
        node = known.get(str(_ref_pk(it._data.get("parent")) or ""))
        home = owning(node)
        if home is not None:
            out[str(it.pk)] = {"pk": str(home.pk), "label": path_of(home),
                               "color": color_of(home)}
    return out


def customer_labels(items):
    """``{str(item.pk): customer name}`` for every task that resolves to one.

    Two ways a subtree says whose work it is, and the nearest ancestor carrying
    either one wins: the container names its **customer** outright
    (``Item.customer``), or it is bound to a **billing project**
    (``Item.time_project``) whose customer that is. On one container the two
    cannot disagree — binding a billing project stamps its customer on the
    container (``Item._follow_billing_customer``) — so this only has to prefer
    the explicit field and stop. A task that resolves to nothing is simply absent
    from the result.

    Batched on purpose. ``resolve_time_project`` walks one item's parents and
    dereferences a reference per level, which is fine for one task and is a
    query storm down a list — so ancestors are fetched level by level (one
    query per depth, the pattern ``describe_containers`` uses) and the billing
    projects and customers in one ``$in`` each. Four or five queries for any
    number of tasks.
    """
    items = list(items or [])
    if not items:
        return {}

    # --- ancestors, level by level (never a deref per row) ---
    known = {}
    frontier = []
    for it in items:
        praw = it._data.get("parent")
        pid = _ref_pk(praw)
        if pid:
            frontier.append(pid)
    depth = 0
    while frontier and depth < 20:
        missing = [pk for pk in frontier if str(pk) not in known]
        if not missing:
            break
        # ``no_dereference`` keeps ``customer``/``time_project`` raw DBRefs: the
        # whole point is to collect the ids and resolve them in one query, not
        # per level.
        parents = list(Item.all_objects(pk__in=missing)
                       .no_dereference()
                       .only("pk", "kind", "parent", "time_project", "customer"))
        if not parents:
            break
        for p in parents:
            known[str(p.pk)] = p
        frontier = [pk for pk in (_ref_pk(p._data.get("parent")) for p in parents) if pk]
        depth += 1

    # --- the binding each task inherits ---
    # Either a customer named outright, or a billing project standing in for
    # one. Both are collected as ids and resolved in one query each below.
    direct = {}    # str(task pk) -> str(Customer pk)
    binding = {}   # str(task pk) -> str(TimeTrackingProject pk)
    cust_ids, tp_ids = set(), set()
    for it in items:
        node = known.get(str(_ref_pk(it._data.get("parent")) or ""))
        seen, steps = set(), 0
        while node is not None and steps < 20:
            if str(node.pk) in seen:
                break
            seen.add(str(node.pk))
            # A binding belongs on a folder/project, never on a task — same rule
            # ``resolve_time_project`` states and for the same reason.
            if node.kind != "task":
                cid = _ref_pk(node._data.get("customer"))
                if cid:
                    direct[str(it.pk)] = str(cid)
                    cust_ids.add(cid)
                    break
                tp = _ref_pk(node._data.get("time_project"))
                if tp:
                    binding[str(it.pk)] = str(tp)
                    tp_ids.add(tp)
                    break
            node = known.get(str(_ref_pk(node._data.get("parent")) or ""))
            steps += 1
    if not tp_ids and not cust_ids:
        return {}

    from application.models.timetracking import Customer, TimeTrackingProject
    projects = list(TimeTrackingProject.objects(pk__in=list(tp_ids))
                    .no_dereference().only("pk", "customer", "archived")) \
        if tp_ids else []
    for p in projects:
        if not p.archived:
            cid = _ref_pk(p._data.get("customer"))
            if cid:
                cust_ids.add(cid)
    cust_ids.discard(None)
    if not cust_ids:
        return {}
    # Archived customers drop out here, as they do from every picker: a name
    # that is out of circulation is not a grouping anybody is working in.
    names = {str(c.pk): c.name
             for c in Customer.objects(pk__in=list(cust_ids), archived__ne=True)
             .only("pk", "name")}
    cust_of_project = {str(p.pk): names.get(str(_ref_pk(p._data.get("customer"))), "")
                       for p in projects if not p.archived}
    out = {pk: names[cid] for pk, cid in direct.items() if names.get(cid)}
    out.update({pk: cust_of_project[tp]
                for pk, tp in binding.items()
                if cust_of_project.get(tp)})
    return out


def _ref_pk(ref):
    """The id behind a reference field, dereferenced or not.

    A loaded document answers ``.pk``, a raw ``DBRef`` answers ``.id``; both
    mean the same thing and neither may trigger a query here."""
    if ref is None:
        return None
    return getattr(ref, "pk", None) or getattr(ref, "id", None)
