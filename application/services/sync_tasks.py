"""Celery tasks for the background syncs.

Two tasks, registered via ``@shared_task`` so they bind to whatever Celery
app is active (see :mod:`application.celery_app`):

* ``zammad.enqueue_due`` — run periodically by Celery beat. Scans for users
  due a sync and fans one ``zammad.sync_user`` task out per user.
* ``zammad.sync_user`` — syncs a single user. Scales horizontally: add more
  worker processes / machines and the queue spreads across them.

The notes mirror (``notes_mirror.*``) rides the same two-step: beat fans out
per user, one task per mirror, one lock each.

A short-lived Redis lock per user prevents the same user being synced twice
at once (e.g. beat re-enqueues while a slow sync is still running). The
actual work lives in :mod:`application.services.zammad_auto` and
:mod:`application.services.notes_sync`, neither of which knows about Celery.
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


_producer = None
_mail_producer = None


def enqueue_team_sync(team_id: str, *, full: bool = False,
                      dry_run: bool = False, trigger: str = "manual") -> None:
    """Put a team sync on the queue from the **web** process.

    The web app never imports :mod:`application.celery_app` — that module
    builds its own Flask app for the workers — so the ``@shared_task``
    decorators here are bound to no broker in this process, and calling
    ``.delay()`` would try Celery's amqp default. Sending the task *by name*
    over a small producer keeps the two sides where they belong: the worker
    owns the code, the web request only writes a message.

    Raises whatever the broker raises when it can't be reached; the caller
    decides whether that means "run it here instead"."""
    global _producer
    from celery import Celery

    url = current_app.config["REDIS_URL"]
    if _producer is None or _producer.conf.broker_url != url:
        _producer = Celery("cairn-producer", broker=url)
        _producer.conf.task_default_queue = "zammad"
    _producer.send_task(
        "zammad.sync_team", args=[team_id],
        kwargs={"full": full, "dry_run": dry_run, "trigger": trigger},
        queue="zammad", retry=False)


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
def sync_team_zammad(self, team_id: str, full: bool = False,
                     dry_run: bool = False, trigger: str = "background"):
    """Sync one team. Locked per team like the per-user task, for the same
    reason: beat must not start a second run over a slow one.

    Also the way the *manual* "Sync now" runs: a full pass over an instance
    takes minutes, which is longer than any gateway waits, so the button
    enqueues this and the page follows the progress record it publishes
    (:mod:`application.services.sync_progress`)."""
    from application.models.team import Team
    from application.services import sync_progress
    from application.services.zammad_import import ZammadError
    from application.services.zammad_team import sync_team

    r = _redis()
    lock = f"zammad:team-lock:{team_id}"
    if not r.set(lock, "1", nx=True, ex=_LOCK_TTL):
        return None

    record = {"state": "queued", "done": 0, "total": 0, "trigger": trigger,
              "full": bool(full), "dry_run": bool(dry_run)}

    def _progress(stage, done, total, stats):
        record.update(state=stage, done=done, total=total,
                      created=stats.tickets_created,
                      updated=stats.tickets_updated,
                      entries=stats.time_entries_imported,
                      errors=len(stats.errors))
        sync_progress.publish(team_id, record)

    try:
        team = Team.objects(pk=team_id).first()
        if team is None:
            return None
        sync_progress.publish(team_id, record)
        stats = sync_team(team, full=full, dry_run=dry_run, trigger=trigger,
                          on_progress=_progress)
        record.update(state="done", stats=stats.as_dict())
        sync_progress.publish(team_id, record)
        return stats.as_dict()
    except ZammadError as exc:
        record.update(state="error", error=str(exc))
        sync_progress.publish(team_id, record)
        raise self.retry(exc=exc)
    except Exception as exc:  # noqa: BLE001 — the page must hear about it
        record.update(state="error", error=str(exc))
        sync_progress.publish(team_id, record)
        raise
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


# ---------------------------------------------------------------------------
# Booking confirmations
# ---------------------------------------------------------------------------

def enqueue_booking_mails(booking_id: str) -> None:
    """Hand a new booking's two confirmation mails to the worker, from the
    **web** process (same producer as ``enqueue_team_sync``). Raises if the
    broker can't be reached; ``booking.announce`` then sends them itself.

    Its own producer, because it has to fail FAST: the visitor is waiting,
    and the transport's default reconnect loop spends about six seconds on a
    refused connection before it gives up (``max_retries`` 0 makes that
    instant)."""
    global _mail_producer
    from celery import Celery

    url = current_app.config["REDIS_URL"]
    if _mail_producer is None or _mail_producer.conf.broker_url != url:
        _mail_producer = Celery("cairn-producer-mail", broker=url)
        _mail_producer.conf.task_default_queue = "zammad"
        _mail_producer.conf.broker_transport_options = {"max_retries": 0}
    _mail_producer.send_task("booking.send_mails", args=[booking_id],
                             queue="zammad", retry=False)


@shared_task(name="booking.send_mails")
def send_booking_mails(booking_id: str) -> str:
    """The confirmations of one booking. A booking called off before the
    worker got to it gets none: its row is gone, and so is the appointment."""
    from application.models.booking import Booking
    from application.services.booking import send_confirmations

    booking = Booking.objects(pk=booking_id).first()
    if booking is None:
        return "gone"
    send_confirmations(booking.owner, booking)
    return "sent"


# ---------------------------------------------------------------------------
# Notes ⇄ WebDAV mirror
# ---------------------------------------------------------------------------

def enqueue_notes_mirror(user_id: str, *, trigger: str = "manual") -> None:
    """Queue one user's mirror round from the **web** process — same producer
    trick as ``enqueue_team_sync``: a first round can write hundreds of files,
    which outlives any gateway. Raises if the broker can't be reached, and the
    caller decides whether that means "run it here instead"."""
    global _producer
    from celery import Celery

    url = current_app.config["REDIS_URL"]
    if _producer is None or _producer.conf.broker_url != url:
        _producer = Celery("cairn-producer", broker=url)
        _producer.conf.task_default_queue = "zammad"
    _producer.send_task("notes_mirror.sync_user", args=[user_id],
                        kwargs={"trigger": trigger}, queue="zammad", retry=False)


def enqueue_mirror_push(user_id: str, note_id: str = "") -> None:
    """Queue a push of ONE note (or, with no id, the scratchpad) a few seconds
    from now — what saving it does. One queued push per file at a time: the
    marker is cleared when the task STARTS, so a save that lands after that
    queues the next one, and the task always syncs the note as it is then."""
    global _producer
    from celery import Celery
    from application.services.notes_sync import PUSH_DELAY

    r = _redis()
    if not r.set(f"mirror:push-queued:{user_id}:{note_id}", "1", nx=True, ex=60):
        return
    url = current_app.config["REDIS_URL"]
    if _producer is None or _producer.conf.broker_url != url:
        _producer = Celery("cairn-producer", broker=url)
        _producer.conf.task_default_queue = "zammad"
    _producer.send_task("notes_mirror.sync_one", args=[user_id, note_id],
                        queue="zammad", countdown=PUSH_DELAY, retry=False)


@shared_task(name="notes_mirror.sync_one", bind=True, max_retries=20,
             default_retry_delay=30)
def sync_one_notes_mirror(self, user_id: str, note_id: str = ""):
    """One note's (or the pad's) push. A round in progress means "later", not
    "never" — it may already have passed this note."""
    from application.services import notes_sync

    _redis().delete(f"mirror:push-queued:{user_id}:{note_id}")
    result = notes_sync.sync_one(user_id, note_id)
    if result == "busy":
        raise self.retry(countdown=30)
    return result


@shared_task(name="notes_mirror.enqueue_due")
def enqueue_due_notes_mirrors() -> int:
    """Fan out to every user whose mirror interval has elapsed."""
    from application.services import notes_sync

    r = _redis()
    enqueued = 0
    for uid in notes_sync.due_user_ids():
        if not r.set(f"mirror:queued:{uid}", "1", nx=True, ex=300):
            continue
        sync_user_notes_mirror.delay(uid, trigger="background")
        enqueued += 1
    return enqueued


@shared_task(name="notes_mirror.sync_user", bind=True, max_retries=2,
             default_retry_delay=300)
def sync_user_notes_mirror(self, user_id: str, trigger: str = "background"):
    """One user's mirror round. Locked per user: two rounds over the same
    share would race on the same files, and the second would see the first's
    half-written state as remote edits."""
    from application.models.user import User
    from application.services import notes_sync
    from application.services.webdav import DavError

    r = _redis()
    lock = f"mirror:lock:{user_id}"
    if not r.set(lock, "1", nx=True, ex=_LOCK_TTL):
        return "skip: already running"
    try:
        user = User.objects(pk=user_id).first()
        if user is None or not (user.notes_mirror_url or "").strip():
            return "skip: not configured"
        stats = notes_sync.sync_user(user, trigger=trigger)
        return stats.as_dict()
    except DavError as exc:
        # The share being unreachable is worth one retry — a NAS reboots.
        raise self.retry(exc=exc)
    finally:
        r.delete(lock)
        r.delete(f"mirror:queued:{user_id}")
