"""Celery tasks for the distributed Zammad auto-sync.

Two tasks, registered via ``@shared_task`` so they bind to whatever Celery
app is active (see :mod:`application.celery_app`):

* ``zammad.enqueue_due`` — run periodically by Celery beat. Scans for users
  due a sync and fans one ``zammad.sync_user`` task out per user.
* ``zammad.sync_user`` — syncs a single user. Scales horizontally: add more
  worker processes / machines and the queue spreads across them.

A short-lived Redis lock per user prevents the same user being synced twice
at once (e.g. beat re-enqueues while a slow sync is still running). The
actual work lives in :mod:`application.services.zammad_auto`, which has no
Celery/Redis dependency.
"""
from __future__ import annotations

import redis
from celery import shared_task
from flask import current_app

from application.services import zammad_auto

# Max time we expect one user's sync to take; the lock auto-expires after
# this so a crashed worker can't wedge a user forever.
_LOCK_TTL = 1800


def _redis():
    return redis.Redis.from_url(current_app.config["REDIS_URL"])


@shared_task(name="zammad.enqueue_due")
def enqueue_due_zammad_syncs() -> int:
    """Enqueue a sync task for every user currently due one. Returns the
    count enqueued. A per-user 'queued' marker (TTL = interval) keeps beat
    from flooding the queue with the same user every tick."""
    interval = int(current_app.config.get("ZAMMAD_SYNC_INTERVAL_MIN", 5))
    r = _redis()
    enqueued = 0
    for uid in zammad_auto.due_user_ids(interval):
        if not r.set(f"zammad:queued:{uid}", "1", nx=True, ex=interval * 60):
            continue  # already queued recently
        sync_user_zammad.delay(uid)
        enqueued += 1
    return enqueued


@shared_task(name="zammad.enqueue_due_teams")
def enqueue_due_team_syncs() -> int:
    """Same fan-out for teams that hold their own Zammad connection.

    A team sync replaces its members' individual ones: one run reads the
    instance and files each ticket under its owner (services.zammad_team)."""
    from application.services import zammad_team

    interval = int(current_app.config.get("ZAMMAD_SYNC_INTERVAL_MIN", 5))
    r = _redis()
    enqueued = 0
    for tid in zammad_team.due_team_ids(interval):
        if not r.set(f"zammad:team-queued:{tid}", "1", nx=True, ex=interval * 60):
            continue
        sync_team_zammad.delay(tid)
        enqueued += 1
    return enqueued


@shared_task(name="zammad.sync_team", bind=True, max_retries=2,
             default_retry_delay=120)
def sync_team_zammad(self, team_id: str):
    """Sync one team. Locked per team like the per-user task, for the same
    reason: beat must not start a second run over a slow one."""
    from application.models.team import Team
    from application.services.zammad_import import ZammadError
    from application.services.zammad_team import sync_team

    r = _redis()
    lock = f"zammad:team-lock:{team_id}"
    if not r.set(lock, "1", nx=True, ex=_LOCK_TTL):
        return None
    try:
        team = Team.objects(pk=team_id).first()
        if team is None:
            return None
        return sync_team(team, trigger="background").as_dict()
    except ZammadError as exc:
        raise self.retry(exc=exc)
    finally:
        r.delete(lock)


@shared_task(name="zammad.sync_user", bind=True, max_retries=2,
             default_retry_delay=120)
def sync_user_zammad(self, user_id: str):
    """Sync one user. Skips if another worker holds the per-user lock;
    retries transient Zammad failures (the HTTP layer already retries
    individual calls, this covers a whole-sync wobble)."""
    from application.services.zammad_import import ZammadError

    r = _redis()
    lock = f"zammad:lock:{user_id}"
    if not r.set(lock, "1", nx=True, ex=_LOCK_TTL):
        return "skip: already running"
    try:
        stats = zammad_auto.sync_user(user_id)
        if stats is None:
            return "skip: not configured"
        return {"created": stats.tickets_created,
                "updated": stats.tickets_updated,
                "errors": len(stats.errors)}
    except ZammadError as exc:
        raise self.retry(exc=exc)
    finally:
        r.delete(lock)
        r.delete(f"zammad:queued:{user_id}")
