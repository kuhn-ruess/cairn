"""What a running Zammad sync is doing, for the page that started it.

A full pass costs two to three requests per ticket, so it outlives any
gateway's patience: pressing "Sync now" and waiting for the response ended at a
504 with the run still going in the background, and no way to see how far it
got. The run therefore happens in the worker and the page *follows* it.

The progress record lives in Redis, not Mongo: it is written once per ticket,
read every couple of seconds, and worthless a minute after the run ends. Every
call swallows its errors — an install without a reachable Redis loses the
progress bar, which is not a reason to lose the sync.
"""
from __future__ import annotations

import json

from flask import current_app

_TTL = 3600


def _redis():
    import redis
    return redis.Redis.from_url(current_app.config["REDIS_URL"])


def _key(team_id) -> str:
    return f"zammad:team-progress:{team_id}"


def publish(team_id, payload: dict) -> None:
    """Store the whole record (the caller keeps it and re-sends it)."""
    try:
        _redis().set(_key(team_id), json.dumps(payload, default=str), ex=_TTL)
    except Exception:
        pass


def read(team_id) -> dict | None:
    try:
        raw = _redis().get(_key(team_id))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def clear(team_id) -> None:
    try:
        _redis().delete(_key(team_id))
    except Exception:
        pass


def running(record: dict | None) -> bool:
    return bool(record) and record.get("state") in (
        "queued", "fetching", "running", "finishing")
