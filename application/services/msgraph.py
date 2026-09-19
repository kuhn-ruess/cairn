"""Microsoft Graph — just enough of it to create a Teams meeting.

One app registration per deployment (``BKTODO_MS_CLIENT_ID`` /
``BKTODO_MS_CLIENT_SECRET``, like the Google calendar client), and a
delegated OAuth consent per user: a Teams meeting has an organiser, and the
organiser is the person whose booking page was used. The tokens therefore
live on ``User`` (encrypted, ``services/crypto.py``) and not in config.

Deliberately small:

* ``/me/onlineMeetings`` is the only Graph call that matters here. Cairn does
  not read the user's Outlook calendar — busy times come from the calendars
  already connected under Settings → Calendars, and mixing a second source in
  would make "why is this slot gone?" unanswerable.
* ``/me`` is called exactly once, at connect time, so the settings page can
  name the account that is linked instead of showing a bare "connected".

The tenant is ``organizations`` by default and that is not a detail: a
personal Microsoft account cannot host a Teams meeting through Graph at all,
so letting one consent would buy a connection that fails on first use.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import current_app

log = logging.getLogger(__name__)

GRAPH_API = "https://graph.microsoft.com/v1.0"
LOGIN_HOST = "https://login.microsoftonline.com"

# ``offline_access`` for the refresh token, ``User.Read`` only to label the
# connection, ``OnlineMeetings.ReadWrite`` for the one call that does the work.
SCOPES = "offline_access openid User.Read OnlineMeetings.ReadWrite"


class GraphError(Exception):
    """Anything Microsoft refused, phrased for the person who has to fix it."""


def _tenant() -> str:
    return (current_app.config.get("MS_TENANT") or "organizations").strip()


def _client() -> tuple[str, str]:
    cid = (current_app.config.get("MS_CLIENT_ID") or "").strip()
    secret = (current_app.config.get("MS_CLIENT_SECRET") or "").strip()
    if not cid or not secret:
        raise GraphError(
            "Microsoft isn't set up on this server — the administrator needs "
            "to set BKTODO_MS_CLIENT_ID and BKTODO_MS_CLIENT_SECRET.")
    return cid, secret


def configured() -> bool:
    """Can this deployment offer a Microsoft connection at all?"""
    return bool((current_app.config.get("MS_CLIENT_ID") or "").strip()
                and (current_app.config.get("MS_CLIENT_SECRET") or "").strip())


def connected(user) -> bool:
    return bool(user.ms_refresh_token)


# ----- OAuth ---------------------------------------------------------------


def authorize_url(redirect_uri: str, state: str) -> str:
    cid, _ = _client()
    params = {
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
    }
    return (f"{LOGIN_HOST}/{_tenant()}/oauth2/v2.0/authorize?"
            + urllib.parse.urlencode(params))


def exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
    cid, secret = _client()
    return _post_form(f"{LOGIN_HOST}/{_tenant()}/oauth2/v2.0/token", {
        "client_id": cid,
        "client_secret": secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "scope": SCOPES,
    })


def _refresh(refresh_token: str) -> dict[str, Any]:
    cid, secret = _client()
    return _post_form(f"{LOGIN_HOST}/{_tenant()}/oauth2/v2.0/token", {
        "client_id": cid,
        "client_secret": secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": SCOPES,
    })


def store_tokens(user, data: dict[str, Any]) -> None:
    """Write a token response onto the user, encrypted. Does not save.

    Microsoft hands out a fresh refresh token on every exchange and the old
    one keeps working for a while, so a missing one in a response is not a
    disconnect — the stored one stays."""
    from application.services.crypto import encrypt_for_user

    access = data.get("access_token") or ""
    refresh = data.get("refresh_token") or ""
    if access:
        user.ms_access_token = encrypt_for_user(user, access)
        user.ms_token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(data.get("expires_in") or 3600) - 60)
    if refresh:
        user.ms_refresh_token = encrypt_for_user(user, refresh)


def disconnect(user) -> None:
    user.ms_refresh_token = None
    user.ms_access_token = None
    user.ms_token_expires_at = None
    user.ms_account = ""


def access_token(user) -> str:
    """A usable access token, refreshing and saving it when it has expired."""
    from application.services.crypto import decrypt_for_user

    now = datetime.now(timezone.utc)
    expires = user.ms_token_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if user.ms_access_token and expires and expires > now:
        return decrypt_for_user(user, user.ms_access_token)
    stored = decrypt_for_user(user, user.ms_refresh_token)
    if not stored:
        raise GraphError("No Microsoft account is connected.")
    data = _refresh(stored)
    store_tokens(user, data)
    user.save()
    return decrypt_for_user(user, user.ms_access_token)


# ----- The two calls -------------------------------------------------------


def account_label(token: str) -> str:
    """``user@contoso.com`` — what the settings page shows as "connected as"."""
    me = _get(f"{GRAPH_API}/me", token)
    return (me.get("userPrincipalName") or me.get("mail")
            or me.get("displayName") or "")


def create_meeting(user, *, subject: str, start: datetime, end: datetime) -> dict:
    """Create a Teams meeting and return ``{"join_url": …, "id": …}``.

    The times go over as UTC instants; Teams has no opinion about the
    visitor's timezone and neither does this module."""
    token = access_token(user)
    body = {
        "subject": subject[:255],
        "startDateTime": start.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"),
        "endDateTime": end.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"),
    }
    data = _post_json(f"{GRAPH_API}/me/onlineMeetings", token, body)
    join = data.get("joinWebUrl") or data.get("joinUrl") or ""
    if not join:
        raise GraphError("Microsoft created a meeting without a join link.")
    return {"join_url": join, "id": data.get("id") or ""}


# ----- HTTP ----------------------------------------------------------------


def _post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    return _send(req)


def _post_json(url: str, token: str, body: dict) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    return _send(req)


def _get(url: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    return _send(req)


def _send(req) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise GraphError(f"Microsoft answered {exc.code}: {_message(detail)}") from exc
    except urllib.error.URLError as exc:
        raise GraphError(f"Microsoft is unreachable: {exc.reason}") from exc


def _message(detail: str) -> str:
    """The human sentence out of a Graph error body, or the body itself."""
    try:
        parsed = json.loads(detail)
    except ValueError:
        return detail
    if isinstance(parsed.get("error"), dict):
        return parsed["error"].get("message") or detail
    return parsed.get("error_description") or detail
