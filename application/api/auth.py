"""Token-based authentication helpers for the REST API."""
from datetime import datetime, timezone
from functools import wraps

from flask import g, jsonify, request

from application.models.user import User


def _extract_token() -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    return request.headers.get("X-API-Token") or request.args.get("token")


def token_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        raw = _extract_token()
        if not raw:
            return jsonify({"error": "missing token"}), 401
        hit = User.by_api_token(raw)
        if not hit:
            return jsonify({"error": "invalid token"}), 401
        user, token = hit
        # Read-only tokens may only drive safe requests — block any mutating
        # method outright, regardless of the endpoint.
        if token.read_only and request.method not in ("GET", "HEAD", "OPTIONS"):
            return jsonify({"error": "read-only token"}), 403
        # Stamp last_used_at without rewriting the whole user document on
        # every API call: a targeted atomic update of just the matched token,
        # throttled to ~once a minute (the value is only used for display).
        now = datetime.now(timezone.utc)
        last = token.last_used_at
        if last is None or (now - last).total_seconds() > 60:
            User.objects(pk=user.pk, api_tokens__pk_ref=token.pk_ref).update_one(
                set__api_tokens__S__last_used_at=now)
            token.last_used_at = now
        g.current_user = user
        g.api_token = token
        return func(*args, **kwargs)

    return wrapper
