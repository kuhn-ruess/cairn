"""Token-based authentication helpers for the REST API."""
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
        token.mark_used()
        user.save()
        g.current_user = user
        g.api_token = token
        return func(*args, **kwargs)

    return wrapper
