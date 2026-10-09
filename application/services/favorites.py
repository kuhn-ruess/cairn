"""Sidebar favorites — resolve a user's heterogeneous favorite tokens to
renderable nav entries.

Tokens (see User.favorites):
    item:<pk>     a task, project, folder or note (resolved live, so a
                  deleted or renamed one behaves)
    persp:<key>   a built-in perspective
    cp:<pid>      a custom perspective
    org:<slug>    an "Organize" nav entry
"""
from flask import url_for

# The "Organize" nav entries, keyed by a stable slug (mirrors base.html).
ORGANIZE_ITEMS = {
    "projects": {"label": "Projects", "icon": "bi-collection", "endpoint": "projects.index"},
    "notes": {"label": "Notes", "icon": "bi-journal-text", "endpoint": "notes.index"},
    "scratchpad": {"label": "Scratchpad", "icon": "bi-stars", "endpoint": "scratchpad.index"},
    "tree": {"label": "Tree", "icon": "bi-diagram-3", "endpoint": "tree.index"},
    "tags": {"label": "Tags", "icon": "bi-tag", "endpoint": "tags.index"},
    "perspectives": {"label": "Perspectives", "icon": "bi-binoculars", "endpoint": "custom_perspectives.index"},
    # The endpoint each entry sends you to must be the one its sidebar row
    # links to — a favourite is a shortcut to that row, and "Time tracking"
    # pointing at the customer list instead of the day sheet is a different
    # page, not a shorter path to the same one.
    "timetracking": {"label": "Time tracking", "icon": "bi-stopwatch", "endpoint": "timetracking.day"},
    "customers": {"label": "Customers", "icon": "bi-person-vcard", "endpoint": "timetracking.index"},
    "bookings": {"label": "Bookings", "icon": "bi-calendar-check", "endpoint": "bookings.index"},
    "habits": {"label": "Habits", "icon": "bi-arrow-repeat", "endpoint": "habits.index"},
    "twelve_week": {"label": "12 Week Year", "icon": "bi-bullseye", "endpoint": "twelve_week.index"},
    "templates": {"label": "Templates", "icon": "bi-files", "endpoint": "templates.index"},
    "review": {"label": "Review", "icon": "bi-arrow-clockwise", "endpoint": "review.index"},
    "dependency_graph": {"label": "Dependency graph", "icon": "bi-diagram-3", "endpoint": "tasks.dependency_graph"},
    "trash": {"label": "Trash", "icon": "bi-trash", "endpoint": "trash.index"},
}


def _item_entry(item):
    if item.kind == "note":
        return {"url": url_for("notes.detail", note_id=item.pk),
                "icon": "bi-journal-text", "color": None}
    if item.kind in ("project", "folder", "section"):
        # Same glyphs as `item_icon` — a favourited folder that renders as a
        # project is the one row in the sidebar you can't place at a glance.
        icon = {"folder": "bi-folder2-open",
                "section": "bi-bookmark"}.get(item.kind, "bi-collection")
        return {"url": url_for("projects.detail", project_id=item.pk),
                "icon": icon, "color": item.color or None}
    return {"url": url_for("tasks.detail", task_id=item.pk),
            "icon": "bi-check2-square", "color": None}


def resolve_favorites(user):
    """Resolve the user's favorite tokens to nav dicts
    ``{token, url, label, icon, color}`` in saved order. Tokens that no longer
    resolve (deleted item, removed perspective) are skipped silently."""
    tokens = list(user.favorites or [])
    if not tokens:
        return []

    # Batch the item lookups (one query for every item:<pk>).
    from application.models.item import Item
    item_pks = [t.split(":", 1)[1] for t in tokens if t.startswith("item:")]
    items = {}
    if item_pks:
        for it in Item.objects(pk__in=item_pks).only(
            "pk", "title", "kind", "color", "folder_domain"
        ):
            items[str(it.pk)] = it

    from application.services import perspectives as persp_svc

    out = []
    for tok in tokens:
        typ, _, ref = tok.partition(":")
        if typ == "item":
            it = items.get(ref)
            if not it:
                continue
            entry = _item_entry(it)
            entry.update(token=tok, label=it.title or "(untitled)")
            out.append(entry)
        elif typ == "persp":
            p = persp_svc._BY_KEY.get(ref)
            if not p:
                continue
            out.append({"token": tok, "url": url_for(p.endpoint),
                        "label": p.label, "icon": p.icon, "color": None})
        elif typ == "cp":
            cp = next((c for c in (user.custom_perspectives or []) if c.pid == ref), None)
            if not cp:
                continue
            out.append({"token": tok,
                        "url": url_for("custom_perspectives.view", pid=cp.pid),
                        "label": cp.name, "icon": cp.icon or "bi-star",
                        "color": cp.color or None})
        elif typ == "org":
            meta = ORGANIZE_ITEMS.get(ref)
            if not meta:
                continue
            out.append({"token": tok, "url": url_for(meta["endpoint"]),
                        "label": meta["label"], "icon": meta["icon"], "color": None})
    return out
