"""Trash (soft-delete) helpers.

Deleting from the UI moves an item — and its whole subtree — into the trash
by stamping ``Item.trashed_at`` instead of dropping the document. The default
``Item.objects`` manager hides trashed items everywhere, so they vanish from
every perspective/project/search until restored or purged.

Purge (permanent delete) happens either manually from the trash view or via
``cairn cleanup-trash`` once an item has sat in the trash past the user's
``trash_retention_days`` window (default 30, ``0`` = keep until emptied).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Best-effort cap on Google Calendar event deletions per purge — the API has
# no bulk delete, so a giant purge shouldn't hang on it.
CAL_CLEANUP_CAP = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _subtree_pks(root_pk) -> list:
    """All pks at or below ``root_pk`` (sections / sub-projects / sub-tasks),
    including trashed ones — uses all_objects so a partly-trashed subtree is
    handled completely."""
    from application.models.item import Item
    pks = [root_pk]
    frontier = [root_pk]
    while frontier:
        kids = list(Item.all_objects(parent__in=frontier).only("id"))
        if not kids:
            break
        frontier = [k.pk for k in kids]
        pks.extend(frontier)
    return pks


def trash_item(item) -> int:
    """Soft-delete ``item`` and its subtree. Best-effort removes any mirrored
    calendar events for the affected tasks. Returns the number trashed."""
    from application.models.item import Item
    pks = _subtree_pks(item.pk)
    _clear_calendar_events(pks, item.owner)
    return Item.all_objects(pk__in=pks, trashed_at=None).update(
        set__trashed_at=_now()
    )


def _reroot_children_of(pks) -> int:
    """Detach anything still live that is filed under one of ``pks``, right
    before those documents are deleted for good.

    A subtree is trashed and purged as a whole, so this normally finds nothing.
    It finds something when part of it came back on its own — a co-owner
    restored their share of a trashed folder (see :func:`restore_item`) — and
    without it that item would keep pointing at a document that no longer
    exists: present in the database, listed by no tree, deletable by nobody."""
    from application.models.item import Item
    return Item.all_objects(parent__in=list(pks), trashed_at=None).update(
        unset__parent=True
    )


def _parent_is_reachable(item) -> bool:
    """True when this item's parent still exists and is not itself trashed.

    Reads the raw reference rather than ``item.parent``: dereferencing a parent
    whose document is gone is exactly the case this has to answer for, and that
    is the one that raises."""
    from application.models.item import Item
    raw = item._data.get("parent")
    pid = getattr(raw, "id", None) or getattr(raw, "pk", None)
    if pid is None:
        return True          # top level — reachable by definition
    par = Item.all_objects(pk=pid).only("id", "trashed_at").first()
    return par is not None and par.trashed_at is None


def restore_item(item) -> int:
    """Restore ``item`` and any descendants currently in the trash.

    A subtree does not always come back in one piece: ``trashed_roots`` is
    per-owner, so in a team the parts of one trashed folder sit in several
    people's trash and each restores their own. Restoring into a parent that is
    still trashed (or gone) would put the item somewhere nothing can reach —
    indistinguishable from never having restored it — so it is re-filed at the
    top level, where its owner can see it and file it again."""
    from application.models.item import Item
    pks = _subtree_pks(item.pk)
    # Only what went in WITH it. ``trash_item`` stamps a whole cascade in one
    # update, so one deletion is one exact ``trashed_at`` — and anything under
    # here carrying a different stamp was thrown away separately, usually long
    # before. Undoing one deletion must not undo those too: emptying a folder
    # over months and then deleting it would bring the months back.
    stamp = item.trashed_at
    if stamp is None:
        return 0
    n = Item.all_objects(pk__in=pks, trashed_at=stamp).update(
        unset__trashed_at=True
    )
    if not _parent_is_reachable(item):
        Item.all_objects(pk=item.pk).update(unset__parent=True)
        item.parent = None
    return n


def purge_item(item) -> int:
    """Permanently delete a trashed item and its subtree. Returns the count."""
    from application.models.item import Item
    pks = _subtree_pks(item.pk)
    # Only what is actually IN the trash goes. Anything under here that was
    # restored on its own (see restore_item) is live work — somebody else's,
    # usually — and a purge that takes it along deletes it with no trash to get
    # it back from. Re-root the survivors instead of leaving them pointing at a
    # document that no longer exists, which is how an item ends up in the
    # database and in no tree.
    dead = {item.pk} | {
        i.pk for i in Item.all_objects(pk__in=pks, trashed_at__ne=None).only("id")
    }
    dead = list(dead)
    _clear_calendar_events(dead, item.owner)
    _reroot_children_of(dead)
    return Item.all_objects(pk__in=dead).delete()


def trashed_roots(user):
    """Trashed items the user owns that sit at the *top* of one deletion — what
    the trash view lists, so a trashed project shows once rather than once per
    descendant.

    "Top of one deletion", not "top of the trash": an item whose parent is also
    in the trash but from a *different* moment was thrown away on its own, and
    :func:`restore_item` leaves it there when the parent comes back. It has to
    be listed in its own right or it would be in the trash and on no page."""
    from application.models.item import Item
    items = list(
        Item.all_objects(owner=user, trashed_at__ne=None)
        .order_by("-trashed_at")
    )
    stamps = {i.pk: i.trashed_at for i in items}
    roots = []
    for i in items:
        # Raw read: a parent whose document is gone must not raise here, of all
        # places — the trash is where such an item gets dealt with.
        raw = i._data.get("parent")
        parent_pk = getattr(raw, "id", None) or getattr(raw, "pk", None)
        if parent_pk not in stamps or stamps[parent_pk] != i.trashed_at:
            roots.append(i)
    return roots


def empty_trash(user) -> int:
    """Permanently delete everything in the user's trash. Returns the count."""
    from application.models.item import Item
    pks = [
        i.pk for i in Item.all_objects(owner=user, trashed_at__ne=None).only("id")
    ]
    if not pks:
        return 0
    _clear_calendar_events(pks, user)
    _reroot_children_of(pks)
    return Item.all_objects(pk__in=pks).delete()


def purge_trash(user, *, dry_run: bool = False) -> int:
    """Permanently delete the user's trashed items past their retention window.
    ``trash_retention_days == 0`` means keep until emptied by hand — a no-op."""
    from application.models.item import Item
    days = int(getattr(user, "trash_retention_days", 30) or 0)
    if days <= 0:
        return 0
    cutoff = _now() - timedelta(days=days)
    qs = Item.all_objects(owner=user, trashed_at__lte=cutoff)
    if dry_run:
        return qs.count()
    pks = [i.pk for i in qs.only("id")]
    _clear_calendar_events(pks, user)
    _reroot_children_of(pks)
    return Item.all_objects(pk__in=pks).delete()


def purge_trash_all(*, dry_run: bool = False) -> dict:
    """Run :func:`purge_trash` for every user with a retention window. The
    field defaults to 30, but older user docs predate it (field absent) — match
    those too via ``exists=False`` so the default still applies to them."""
    from mongoengine import Q
    from application.models.user import User
    out = {}
    qs = User.objects(
        Q(trash_retention_days__gt=0) | Q(trash_retention_days__exists=False)
    ).only("id", "username", "trash_retention_days")
    for user in qs:
        out[user.username] = purge_trash(user, dry_run=dry_run)
    return out


def _clear_calendar_events(pks, owner) -> None:
    """Best-effort removal of mirrored Google Calendar events for the given
    task pks. Capped + swallowed so calendar trouble never blocks a delete."""
    from application.models.item import Item
    try:
        from application.services.calendar_writeback import (
            delete_task_calendar_events,
        )
    except Exception:
        return
    n = 0
    for t in Item.all_objects(pk__in=pks, kind="task").filter(
        __raw__={"$or": [{"gcal_due_event_id": {"$ne": None}},
                         {"gcal_plan_event_id": {"$ne": None}}]}
    ):
        if n >= CAL_CLEANUP_CAP:
            break
        try:
            delete_task_calendar_events(t, owner)
        except Exception:
            pass
        n += 1
