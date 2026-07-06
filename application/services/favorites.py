"""Sidebar favorites — resolve a user's heterogeneous favorite tokens to
renderable nav entries.

Tokens (see User.favorites):
    item:<pk>     a task or note (resolved live so deleted/renamed ones behave)
    persp:<key>   a built-in perspective
    cp:<pid>      a custom perspective
    org:<slug>    an "Organize" nav entry
"""
from flask import url_for

# The "Organize" nav entries, keyed by a stable slug (mirrors base.html).
ORGANIZE_ITEMS = {
    "projects": {"label": "Projects", "icon": "bi-collection", "endpoint": "projects.index"},
    "notes": {"label": "Notes", "icon": "bi-journal-text", "endpoint": "notes.index"},
    "braindump": {"label": "Braindump", "icon": "bi-stars", "endpoint": "braindump.index"},
    "tree": {"label": "Tree", "icon": "bi-diagram-3", "endpoint": "tree.index"},
    "tags": {"label": "Tags", "icon": "bi-tag", "endpoint": "tags.index"},
    "perspectives": {"label": "Perspectives", "icon": "bi-binoculars", "endpoint": "custom_perspectives.index"},
    "timetracking": {"label": "Time tracking", "icon": "bi-stopwatch", "endpoint": "timetracking.index"},
    "review": {"label": "Review", "icon": "bi-arrow-clockwise", "endpoint": "review.index"},
    "dependency_graph": {"label": "Dependency graph", "icon": "bi-diagram-3", "endpoint": "tasks.dependency_graph"},
    "trash": {"label": "Trash", "icon": "bi-trash", "endpoint": "trash.index"},
}


def _item_entry(item):
    if item.kind == "note":
        return {"url": url_for("notes.detail", note_id=item.pk),
                "icon": "bi-journal-text", "color": None}
    if item.kind in ("project", "folder", "section"):
        return {"url": url_for("projects.detail", project_id=item.pk),
                "icon": "bi-collection", "color": item.color or None}
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
