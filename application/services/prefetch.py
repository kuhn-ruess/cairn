"""Bulk reference prefetching to avoid template-loop N+1s.

Rendering a list of tasks touches ``.project`` (parent) / ``.assigned_to`` /
``.assigned_by`` per row — each a separate dereference query. Calling
``prefetch_refs(items)`` first warms mongoengine's reference cache in one
``$in`` query per referenced collection, so the template reads from memory.
"""

from __future__ import annotations


def prefetch_refs(items):
    """Warm parent/assignee references for ``items`` in bulk (in place).

    Dangling refs keep their raw value (unchanged behaviour). ``all_objects``
    so a trashed parent still resolves, mirroring how reference deref bypasses
    the queryset manager.
    """
    from application.models.item import Item
    from application.models.user import User

    def _rawid(raw):
        return getattr(raw, "id", None) or getattr(raw, "pk", None)

    item_ids, user_ids = set(), set()
    for t in items:
        praw = t._data.get("parent")
        if praw is not None and not isinstance(praw, Item):
            item_ids.add(_rawid(praw))
        for f in ("assigned_to", "assigned_by"):
            uraw = t._data.get(f)
            if uraw is not None and not isinstance(uraw, User):
                user_ids.add(_rawid(uraw))
    item_ids.discard(None)
    user_ids.discard(None)
    items_by_id = {p.pk: p for p in Item.all_objects(pk__in=list(item_ids))} if item_ids else {}
    users_by_id = {u.pk: u for u in User.objects(pk__in=list(user_ids))} if user_ids else {}
    for t in items:
        praw = t._data.get("parent")
        if praw is not None and not isinstance(praw, Item):
            t._data["parent"] = items_by_id.get(_rawid(praw), praw)
        for f in ("assigned_to", "assigned_by"):
            uraw = t._data.get(f)
            if uraw is not None and not isinstance(uraw, User):
                t._data[f] = users_by_id.get(_rawid(uraw), uraw)
