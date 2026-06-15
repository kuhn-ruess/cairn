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
        for f in ("parent", "assignee_parent"):
            praw = t._data.get(f)
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
        for f in ("parent", "assignee_parent"):
            praw = t._data.get(f)
            if praw is not None and not isinstance(praw, Item):
                t._data[f] = items_by_id.get(_rawid(praw), praw)
        for f in ("assigned_to", "assigned_by"):
            uraw = t._data.get(f)
            if uraw is not None and not isinstance(uraw, User):
                t._data[f] = users_by_id.get(_rawid(uraw), uraw)


def annotate_subtask_counts(items):
    """Set ``_subtask_total`` / ``_subtask_done`` on each task in one query
    (no per-row child count → no N+1). Tasks with no children get 0."""
    from collections import defaultdict
    from application.models.item import Item

    pks = [t.pk for t in items if getattr(t, "kind", None) == "task"]
    total, done = defaultdict(int), defaultdict(int)
    if pks:
        for ch in Item.objects(parent__in=pks, kind="task").only("parent", "status"):
            praw = ch._data.get("parent")
            pid = getattr(praw, "id", None) or getattr(praw, "pk", None)
            if pid is None:
                continue
            total[pid] += 1
            if ch.status in ("done", "completed"):
                done[pid] += 1
    for t in items:
        t._subtask_total = total.get(t.pk, 0)
        t._subtask_done = done.get(t.pk, 0)


def prepare_list(items):
    """Materialise + warm refs + annotate subtask counts for a task list,
    so the shared row template renders with no per-row queries. Returns the
    list (accepts a queryset or list)."""
    items = list(items)
    prefetch_refs(items)
    annotate_subtask_counts(items)
    return items
