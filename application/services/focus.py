"""OmniFocus-style "Focus".

When a user focuses a folder/project/section (``User.focused_item_pk``), the
whole app narrows to tasks beneath that item. The single lever is
:func:`restrict`, which ANDs ``parent__in=<focus subtree>`` into any task
queryset's ``Q``. The bulk of the app routes through ``_scope(user)`` in the
view blueprints, so making those delegate here covers most perspectives for
free; the remaining non-``_scope`` query sites call :func:`restrict` directly.

Everything is cached per-request on ``flask.g`` so we never re-walk the tree
or re-query the focused item inside a Jinja loop (see the DB-load guidance).
"""
from flask import g
from mongoengine import Q

from application.models.item import Item

# Only containers can be focused — focusing a bare task makes no sense.
FOCUSABLE_KINDS = ("folder", "project", "section")


def current(user):
    """Return the focused ``Item`` for ``user``, or ``None``.

    Validates the stored pk on every call: if the item was deleted/trashed,
    is no longer viewable, or isn't a focusable container, the stale focus is
    cleared (persisted) and ``None`` returned. Cached on ``flask.g``.
    """
    cached = getattr(g, "_focus_item", "unset")
    if cached != "unset":
        return cached

    item = None
    pk = (getattr(user, "focused_item_pk", "") or "").strip()
    if pk:
        # Item.objects hides trashed items, so a trashed focus resolves to None.
        item = Item.objects(pk=pk).first()
        if (item is None
                or item.kind not in FOCUSABLE_KINDS
                or not item.can_be_viewed_by(user)):
            # Stale focus — clear it so the user isn't stuck on a dead filter.
            user.focused_item_pk = ""
            user.save()
            item = None

    g._focus_item = item
    return item


def focus_pks(user):
    """Return ``[focus pk + every descendant container pk]`` or ``None``.

    ``None`` means no active focus (callers should leave their query
    untouched). Walks the ``parent`` tree breadth-first — same pattern as
    ``tasks._scoped_project_pks``. Cached on ``flask.g``.
    """
    cached = getattr(g, "_focus_pks", "unset")
    if cached != "unset":
        return cached

    root = current(user)
    if root is None:
        g._focus_pks = None
        return None

    out = [root.pk]
    frontier = [root.pk]
    while frontier:
        kids = list(Item.objects(parent__in=frontier).only("pk"))
        if not kids:
            break
        new_pks = [k.pk for k in kids]
        out.extend(new_pks)
        frontier = new_pks

    g._focus_pks = out
    return out


def restrict(q, user):
    """AND the active focus filter into a task queryset ``Q``.

    No-op when no focus is set. Tasks hang off a project/section whose pk is
    in the focus subtree, so ``parent__in`` is the correct constraint for
    every perspective.
    """
    pks = focus_pks(user)
    if pks is None:
        return q
    return q & Q(parent__in=pks)
