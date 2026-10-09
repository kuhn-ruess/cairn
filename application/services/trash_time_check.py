"""Where booked time stands after items went through the trash.

Booked time is never touched by the trash (``services/trash.py`` has no business
with the billing collections, and ``tests/test_trash_keeps_time.py`` holds it
to that). What a trashing CAN leave behind is around the time, not in it:

* **Duplicate Zammad tasks.** The Zammad sync looks a ticket's task up through
  ``Item.objects``, which hides the trash, so a ticket that changed while its
  task sat in the trash got a second, new task. Time booked in Zammad meanwhile
  went onto that new task; after the old one was restored there are two tasks
  for one ticket and the time is split between them.
* **Items re-filed at the top level** by a restore that ran child first: they
  carry ``trashed_source="parent"`` (they went in with a parent) and no parent
  any more. Where they came from is not recorded, so this can only list them.

:func:`check` reports all of it, plus how much time sits on tasks that are in
the trash or deleted (shown on the day sheet and in the report either way).
With ``apply=True`` it does the one thing that is safe to do automatically:
re-link the entries of a duplicate Zammad task onto the OLDEST live task for
that ticket, the one the sync itself finds first. No task, comment or entry is
deleted; running it again finds nothing more to move.
"""
from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId


def _user_teams(user):
    from application.services.timetracking import user_teams
    return user_teams(user)


def check(user, *, apply: bool = False) -> dict:
    """One user's report. ``apply`` re-links duplicate-ticket time (see module)."""
    from application.models.item import Item
    from application.models.timetracking import TimeBucket

    out = {"user": user.username, "on_trashed": [0, 0], "on_deleted": [0, 0],
           "duplicates": [], "relinked": 0, "rerooted": []}
    teams = _user_teams(user)
    uid = str(user.pk)

    # 1) Time of this user whose task is in the trash or gone: [entries, minutes].
    by_src: dict = {}
    if teams:
        for b in TimeBucket.objects(team__in=teams).only("entries").as_pymongo():
            for e in b.get("entries") or []:
                sid = e.get("source_item_id")
                if (e.get("status") != "active" or e.get("user_id") != uid
                        or not sid or e.get("source_kind") == "booking"):
                    continue
                n, m = by_src.get(sid, (0, 0))
                by_src[sid] = (n + 1, m + (e.get("minutes") or 0))
    oids = [ObjectId(s) for s in by_src if ObjectId.is_valid(s)]
    state = {str(d["_id"]): bool(d.get("trashed_at"))
             for d in Item._get_collection().find(
                 {"_id": {"$in": oids}}, {"trashed_at": 1})}
    for sid, (n, m) in by_src.items():
        key = None
        if sid not in state:
            key = "on_deleted"
        elif state[sid]:
            key = "on_trashed"
        if key:
            out[key][0] += n
            out[key][1] += m

    # 2) Two or more live tasks of this user for the same Zammad ticket.
    coll = Item._get_collection()
    groups = coll.aggregate([
        {"$match": {"owner": user.pk, "trashed_at": None,
                    "external_id": {"$regex": "^zammad:"}}},
        {"$group": {"_id": "$external_id", "ids": {"$push": "$_id"},
                    "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
    ])
    for g in groups:
        ids = sorted(g["ids"])            # ObjectIds sort by creation time
        keep, dupes = ids[0], ids[1:]
        keep_doc = coll.find_one({"_id": keep}, {"title": 1, "kind": 1})
        row = {"ticket": g["_id"], "keep": str(keep),
               "title": (keep_doc or {}).get("title") or "",
               "dupes": [str(d) for d in dupes], "entries": 0, "minutes": 0}
        dupe_ids = {str(d) for d in dupes}
        for b in TimeBucket.objects(
                team__in=teams,
                entries__source_item_id__in=list(dupe_ids)) if teams else []:
            for e in b.entries:
                if e.source_item_id not in dupe_ids:
                    continue
                row["entries"] += 1
                row["minutes"] += e.minutes or 0
                if not apply:
                    continue
                res = TimeBucket.objects(
                    pk=b.pk,
                    entries__match={"entry_id": e.entry_id,
                                    "source_item_id": e.source_item_id},
                ).update_one(
                    set__entries__S__source_item_id=str(keep),
                    set__entries__S__source_kind=(keep_doc or {}).get("kind") or "task",
                    set__entries__S__source_label=(row["title"] or "")[:200],
                    set__updated_at=datetime.now(timezone.utc),
                )
                out["relinked"] += res
        out["duplicates"].append(row)

    # 3) Restored items that went in with a parent and came back without one.
    for d in coll.find({"owner": user.pk, "trashed_at": None,
                        "trashed_source": "parent",
                        "$or": [{"parent": None}, {"parent": {"$exists": False}}]},
                       {"title": 1, "kind": 1, "trashed_via": 1}):
        out["rerooted"].append({"id": str(d["_id"]), "kind": d.get("kind"),
                                "title": d.get("title") or "",
                                "via": d.get("trashed_via") or ""})
    return out
