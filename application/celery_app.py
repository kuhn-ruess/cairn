"""Celery entry point for the background workers.

The worker and beat processes target this module:

    celery -A application.celery_app.celery worker -l info -c 8 -Q zammad
    celery -A application.celery_app.celery beat   -l info

Every task runs inside a Flask app context, so the mongoengine connection
and ``app.config`` (set up in :func:`create_app`) are available exactly as
in a web request. ``import_for_user`` stays the broker-agnostic per-user
core; the tasks in :mod:`application.services.sync_tasks` just wrap it.
"""
from celery import Celery

from application import create_app

# One Flask app per worker process — establishes the mongoengine connection
# and loads config. Tasks reuse it via the ContextTask wrapper below.
flask_app = create_app()
_redis_url = flask_app.config.get("REDIS_URL", "redis://localhost:6379/0")

celery = Celery("cairn", broker=_redis_url, backend=_redis_url)
celery.conf.update(
    task_default_queue="zammad",
    task_acks_late=True,            # re-deliver if a worker dies mid-task
    worker_prefetch_multiplier=1,   # fair dispatch for long-ish sync tasks
    task_track_started=True,
    timezone="UTC",
    beat_schedule={
        "enqueue-due-zammad-syncs": {
            "task": "zammad.enqueue_due",
            "schedule": 60.0,  # tick every minute; due-check spaces real syncs
        },
        # Teams that hold their own connection: one run for everyone, instead
        # of every member fetching the same instance (services.zammad_team).
        "enqueue-due-zammad-team-syncs": {
            "task": "zammad.enqueue_due_teams",
            "schedule": 60.0,
        },
        # Notes ⇄ WebDAV. Same shape: tick every minute, the due-check spaces
        # the actual rounds by each user's own interval.
        "enqueue-due-notes-mirrors": {
            "task": "notes_mirror.enqueue_due",
            "schedule": 60.0,
        },
        # 12 Week Year: completions of habits mapped to Habitify. Every
        # quarter hour; each cycle is read at most once an hour.
        "sync-due-habitify": {
            "task": "habitify.sync_due",
            "schedule": 900.0,
        },
    },
)


class ContextTask(celery.Task):
    abstract = True

    def __call__(self, *args, **kwargs):
        with flask_app.app_context():
            return self.run(*args, **kwargs)


celery.Task = ContextTask

# Import so the @shared_task definitions bind to this Celery app.
from application.services import sync_tasks  # noqa: E402,F401
