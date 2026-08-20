"""Broker-agnostic core for the background Zammad auto-sync.

This module knows nothing about Celery or Redis — it only answers two
questions and does one thing:

* which users are *due* for an automatic sync, and
* sync one user, replaying their stored import options.

The Celery tasks in :mod:`application.services.sync_tasks` wrap these (one
task enqueues :func:`due_user_ids`, another runs :func:`sync_user`); the
``cairn sync-zammad-all`` CLI command calls them directly with a thread
pool. Swapping the broker later only touches the wrapper, not this core.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from application.models.item import Item
from application.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def due_user_ids(interval_min: int, *, force: bool = False,
                 all_configured: bool = False) -> list[str]:
    """Return ids of users whose Zammad should be synced now.

    A user qualifies when they have a saved URL + token and either opted
    into auto-sync (``all_configured`` lifts that requirement) and their
    last sync is older than ``interval_min`` (``force`` ignores recency).
    """
    cutoff = _utcnow() - timedelta(minutes=max(interval_min, 0))
    # ``zammad_enabled`` off means the user has handed their sync over (to the
    # team's, usually) — a background run must not quietly keep going.
    q = User.objects(zammad_url__nin=[None, ""], zammad_token__nin=[None, ""],
                     zammad_enabled__ne=False)
    if not all_configured:
        q = q.filter(zammad_auto_sync=True)
    out: list[str] = []
    for u in q.only("id", "last_zammad_sync_at"):
        if not force and u.last_zammad_sync_at and u.last_zammad_sync_at > cutoff:
            continue
        out.append(str(u.pk))
    return out


def sync_user(user_id: str):
    """Sync one user's Zammad, replaying their stored import options.

    Returns the :class:`ZammadStats` (or ``None`` when the user is gone or
    not configured). Raises ``ZammadError`` for an outright failure so the
    caller (Celery) can retry.
    """
    from application.services.crypto import decrypt_for_user
    from application.services.zammad_import import import_for_user

    user = User.objects(pk=user_id).first()
    if not user or not user.zammad_url or not user.zammad_token:
        return None
    token = decrypt_for_user(user, user.zammad_token)
    if not token:
        return None

    project = None
    if user.zammad_sync_project:
        project = Item.objects(
            pk=user.zammad_sync_project, owner=user, kind="project",
        ).first()  # gone → fall back to inbox (parent=None)

    # import_for_user stamps last_zammad_sync_at + saves on a real (non-dry)
    # run, so the next due-check naturally skips this user until interval.
    return import_for_user(
        user, user.zammad_url, token,
        project=project,
        assignee_only=bool(user.zammad_assignee_only),
        include_closed=bool(user.zammad_include_closed),
    )
