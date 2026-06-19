"""Service layer for the team time-tracking / billing module.

Keeps the bucket mechanics (atomic append, soft-cancel, inheritance resolution)
out of the views. See :mod:`application.models.timetracking` for the data model
and the design rationale.
"""
from __future__ import annotations

from datetime import datetime, timezone

from application.models.item import Item
from application.models.timetracking import (
    Customer,
    TimeBucket,
    TimeEntry,
    TimeTrackingProject,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Team resolution / gating
# ---------------------------------------------------------------------------

def primary_team(user):
    """The single team this module operates in for ``user``.

    A member's ``user.team``; otherwise the first team they admin. ``None`` for
    solo users — the module is team-only, so views gate on this being set."""
    if user is None:
        return None
    if getattr(user, "team", None):
        return user.team
    from application.models.team import Team
    return Team.objects(admin=user._get_current_object()
                        if hasattr(user, "_get_current_object") else user).first()


def has_team(user) -> bool:
    return primary_team(user) is not None


# ---------------------------------------------------------------------------
# Inheritance: resolve the billing project for a Cairn item
# ---------------------------------------------------------------------------

def resolve_time_project(item) -> TimeTrackingProject | None:
    """Climb ``item``'s parents until one carries a ``time_project`` link.

    Tasks rarely set it themselves; they inherit the folder/project binding.
    Bounded + cycle-guarded like the other ancestor walks in the model."""
    seen = set()
    node = item
    depth = 0
    while node is not None and depth < 50:
        if node.pk in seen:
            break
        seen.add(node.pk)
        tp = getattr(node, "time_project", None)
        if tp:
            # Dereference defensively — a deleted/archived project shouldn't crash.
            try:
                if tp and not tp.archived:
                    return tp
            except Exception:
                return None
        node = node.parent
        depth += 1
    return None


# ---------------------------------------------------------------------------
# Logging time into the monthly bucket
# ---------------------------------------------------------------------------

def period_for(spent_on: datetime) -> str:
    return spent_on.strftime("%Y-%m")


def add_entry(*, project: TimeTrackingProject, user, minutes: int,
              spent_on: datetime, note: str = "", source_item=None,
              billable: bool | None = None) -> TimeEntry:
    """Append a time entry to the ``(team, customer, month)`` bucket atomically.

    Freezes the project's current rate + computed amount onto the entry, and
    captures the source item only as a soft id + label snapshot."""
    team = project.team
    customer = project.customer
    period = period_for(spent_on)

    rate = project.hourly_rate_cents
    if billable is None:
        billable = True
    amount = None
    if billable and rate:
        amount = round(rate * minutes / 60)

    label = ""
    src_id = None
    src_kind = None
    if source_item is not None:
        src_id = str(source_item.pk)
        src_kind = source_item.kind
        label = (source_item.title or "")[:200]

    entry = TimeEntry(
        project=project,
        user=user._get_current_object() if hasattr(user, "_get_current_object") else user,
        user_name=getattr(user, "display_name", None) or getattr(user, "username", "") or "",
        minutes=minutes,
        spent_on=spent_on,
        note=(note or "")[:1000],
        source_item_id=src_id,
        source_kind=src_kind,
        source_label=label,
        hourly_rate_cents=rate,
        amount_cents=amount,
        billable=bool(billable),
    )

    # One atomic upsert-and-push: creates the bucket on first write for the
    # month, then appends. Concurrent loggers each issue their own atomic
    # $push, so they never clobber one another.
    TimeBucket.objects(team=team, customer=customer, period=period).update_one(
        upsert=True,
        push__entries=entry,
        inc__total_minutes=minutes,
        set__updated_at=_utcnow(),
        set_on_insert__created_at=_utcnow(),
    )
    return entry


def parse_duration(raw: str):
    """Parse a logged duration into whole minutes, or ``None`` if unparseable.

    Accepts ``90`` (minutes), ``1:30`` (h:mm), ``1h30m`` / ``1h`` / ``30m``,
    ``1.5h`` / ``1,5h`` (decimal hours). Lenient on spaces."""
    import re
    s = (raw or "").strip().lower().replace(" ", "")
    if not s:
        return None
    m = re.fullmatch(r"(\d+):([0-5]?\d)", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)h", s)
    if m:
        return round(float(m.group(1).replace(",", ".")) * 60)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m?)?", s)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    if s.isdigit():
        return int(s)
    return None


def task_time_context(user, item):
    """Everything the inspector time section needs for one task, or ``None``.

    ``None`` when the user has no team or the task's subtree isn't linked to a
    billing project — in which case the section renders nothing. Otherwise:
    ``{project, total_minutes, entries}`` where each entry is a flat dict (so the
    template never dereferences across documents)."""
    if not has_team(user):
        return None
    project = resolve_time_project(item)
    if project is None:
        return None
    team = project.team
    customer = project.customer
    src_id = str(item.pk)
    buckets = TimeBucket.objects(team=team, customer=customer,
                                 entries__source_item_id=src_id)
    rows = []
    total = 0
    for b in buckets:
        for e in b.entries:
            if e.source_item_id != src_id or e.status != "active":
                continue
            total += e.minutes or 0
            rows.append({
                "entry_id": e.entry_id,
                "bucket_id": str(b.id),
                "minutes": e.minutes,
                "spent_on": e.spent_on,
                "note": e.note,
                "locked": e.locked,
                "billable": e.billable,
                "user_name": e.user_name,
                "amount_cents": e.amount_cents,
            })
    rows.sort(key=lambda r: r["spent_on"] or _utcnow(), reverse=True)
    return {"project": project, "total_minutes": total, "entries": rows}


def cancel_entry(*, bucket: TimeBucket, entry_id: str, user) -> bool:
    """Soft-cancel an entry: never removed, just flipped to ``cancelled``.

    Refuses if the entry is ``locked`` (billed). Targets the entry by its stable
    ``entry_id`` via a positional update — no reliance on list position."""
    entry = next((e for e in bucket.entries
                  if e.entry_id == entry_id and e.status == "active"), None)
    if entry is None:
        return False
    if entry.locked:
        return False
    actor = user._get_current_object() if hasattr(user, "_get_current_object") else user
    res = TimeBucket.objects(
        pk=bucket.pk, entries__entry_id=entry_id, entries__locked=False,
    ).update_one(
        set__entries__S__status="cancelled",
        set__entries__S__cancelled_at=_utcnow(),
        set__entries__S__cancelled_by=actor,
        inc__total_minutes=-(entry.minutes or 0),
        set__updated_at=_utcnow(),
    )
    return bool(res)
