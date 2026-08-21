"""Recording what a sync run did, and reading it back.

Deliberately tiny and deliberately forgiving: a log that can break the thing it
is logging is worse than no log, so :func:`record` swallows its own errors. The
run is the point; this is the note about it.
"""
from __future__ import annotations

import logging

from application.models.sync_run import SyncRun

log = logging.getLogger(__name__)

# Counters worth keeping. The stats objects carry working state too (caches, the
# per-ticket debug trace); this is the part a person reads three weeks later.
_KEEP = (
    "tickets_fetched", "tickets_created", "tickets_updated", "tickets_skipped",
    "tickets_dropped", "tickets_unchanged", "tickets_unowned",
    "tickets_foreign_owner", "filtered_assignee", "filtered_closed",
    "closed_with_time", "comments_imported",
    "time_entries_imported", "time_entries_repaired", "time_customers_created",
    "time_folders_linked", "time_task_links_cleared", "time_skipped_unlinked",
    "time_skipped_no_time", "time_skipped_old", "time_users_matched",
    "api_calls", "since",
    # mite
    "customers_created", "customers_updated", "projects_created",
    "projects_updated", "entries_imported", "entries_updated",
    "entries_skipped", "entries_empty", "entries_undated", "entries_frozen",
    # notes mirror
    "files_written", "files_deleted", "notes_created", "notes_updated",
    "notes_trashed", "folders_created", "assets_written", "conflicts",
    "unchanged",
)


def summarise(stats) -> dict:
    """The counters worth keeping from a stats object, plus its complaints."""
    if stats is None:
        return {}
    raw = stats.as_dict() if hasattr(stats, "as_dict") else dict(stats)
    out = {k: raw[k] for k in _KEEP if raw.get(k)}
    unmatched = raw.get("time_users_unmatched") or raw.get("unmatched_users")
    if unmatched:
        out["unmatched_users"] = list(unmatched)[:20]
    if raw.get("errors"):
        out["errors"] = list(raw["errors"])[:10]
    return out


def record(kind: str, *, team=None, user=None, trigger: str = "manual",
           stats=None, error: str = "", seconds: float = 0.0) -> None:
    """Note one run. Never raises — a broken log must not break a sync."""
    try:
        SyncRun(kind=kind, team=team, user=user, trigger=trigger,
                ok=not error, error=(error or "")[:500],
                seconds=round(seconds, 1), stats=summarise(stats)).save()
    except Exception as exc:  # pragma: no cover - logging must not throw
        log.warning("could not record %s sync run: %s", kind, exc)


def recent(*, team=None, users=None, limit: int = 50) -> list:
    """The team's runs, newest first — its own and its members' personal ones,
    which is what "what has the import been doing" means to a manager."""
    from mongoengine import Q

    query = Q(team=team) if team is not None else Q()
    if users:
        query = query | Q(user__in=list(users))
    return list(SyncRun.objects(query).order_by("-created_at").limit(limit))
