"""Template library — deep-copy a hidden template subtree into real items.

A template is an ordinary :class:`Item` with ``template=True`` (and so is its
whole subtree). The global ``Item.objects`` manager hides those from every
normal list, so we MUST traverse with ``Item.all_objects`` here — otherwise the
template's own children are invisible to the copy.

"Using" a template preserves kind:
* a **task** template becomes a real task in the Inbox (``parent=None``),
* a **project** template becomes a real top-level project,
carrying its sub-tasks / sections / checklists. Everything is reset to a fresh
state (statuses re-opened, checklists un-ticked, no dates) — a template holds
structure, not progress.
"""
from application.models.item import ChecklistItem, Item, NoteFile, NoteImage


def _fresh_status(kind: str) -> str:
    if kind == "project":
        return "active"
    if kind == "task":
        return "open"
    return "active"  # section / folder


def _copy_node(src: Item, owner, parent, *, as_template: bool) -> Item:
    """Recursively copy ``src`` (and its subtree) under ``parent``.

    ``as_template`` propagates the flag: cloning FOR USE sets it False (real
    items); duplicating WITHIN the library keeps it True.
    """
    new = Item(
        owner=owner,
        kind=src.kind,
        parent=parent,
        template=as_template,
        folder_domain=src.folder_domain,
        title=src.title,
        note=src.note or "",
        color=src.color or "",
        tags=list(src.tags or []),
        priority=src.priority,
        flagged=bool(src.flagged),
        estimated_minutes=src.estimated_minutes,
        project_type=src.project_type,
        sort_order=src.sort_order or 0,
        status=_fresh_status(src.kind),
        # Reset every checklist item to unchecked — a fresh copy starts empty.
        checklist=[ChecklistItem(text=ci.text, done=False)
                   for ci in (src.checklist or [])],
        # Preserve inline note images/files (tokens are referenced by the note
        # markdown, so the links keep resolving on the copy).
        note_images=[NoteImage(token=i.token, content_type=i.content_type,
                               data=i.data) for i in (src.note_images or [])],
        note_files=[NoteFile(token=f.token, filename=f.filename,
                             content_type=f.content_type, data=f.data)
                    for f in (src.note_files or [])],
    )
    new.save()
    for child in _children(src, owner):
        _copy_node(child, owner, new, as_template=as_template)
    return new


def _children(item: Item, owner):
    """Direct children of a template node, ordered — via ``all_objects`` so the
    (hidden) template children are visible."""
    return (Item.all_objects(parent=item, owner=owner, trashed_at=None)
            .order_by("+sort_order", "+created_at"))


def use_template(tpl: Item, owner) -> Item:
    """Deep-copy a template into real, usable items. Returns the new root
    (a task lands in the Inbox; a project becomes a top-level project)."""
    return _copy_node(tpl, owner, None, as_template=False)


def duplicate_template(tpl: Item, owner) -> Item:
    """Copy a template within the library (stays a template). Returns the copy,
    reparented next to the original."""
    copy = _copy_node(tpl, owner, tpl.parent, as_template=True)
    copy.title = f"{tpl.title} (copy)"
    copy.save()
    return copy
