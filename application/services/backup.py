"""Full per-user export / import (backup & restore).

One bundle = one user's complete data: their profile + embedded tags,
perspectives, calendars and API tokens, plus every ``Item`` they own
(tasks, projects, sections, folders — including trashed ones).

Serialization mirrors the cmdbsyncer rule-export approach: lean on
mongoengine's native ``Document.to_json()`` / ``Document.from_json()`` so
ObjectIds, references, embedded docs and binary note-images round-trip
without a hand-written codec. The whole thing is wrapped in a single JSON
envelope (``format`` / ``version`` / ``exported_at``) instead of JSONL,
because a per-user backup is read as one unit.

Two surfaces share this module:

* GUI download (``/settings/export``) — the user pulls *their own* data;
  ``redact_secrets=True`` strips credentials (password hash, integration
  tokens, OAuth blobs, API-token hashes) so the plaintext file is a safe
  portability export, not a credential dump.
* CLI (``cairn export-user`` / ``cairn import-user``) — operator path; the
  full export keeps everything so a restore into a fresh DB is faithful.

Import semantics (see :func:`import_bundle`):

* user not found  → created verbatim, original ``_id`` preserved (disaster
  recovery into an empty DB; references inside items stay valid).
* user found      → content (items, tags, perspectives, scalar prefs) is
  merged/overwritten; identity, password and connection state are kept.
* ``replace``     → the target user's existing items are dropped first, for
  a clean restore instead of a merge.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

from application.models.item import Item
from application.models.user import User

FORMAT = "cairn-backup"
VERSION = 1

# User fields holding credentials / secrets — dropped from a redacted
# (GUI) export. The full CLI export keeps them so a restore is complete.
_SECRET_USER_FIELDS = (
    "password_hash",
    "todoist_token",
    "zammad_token",
    "password_reset_token",
    "password_reset_expires_at",
    "api_tokens",
)
_SECRET_CALENDAR_FIELDS = (
    "oauth_refresh_token",
    "oauth_access_token",
    "oauth_token_expires_at",
    "oauth_scope",
)

# Scalar/content preferences copied onto an *existing* user on import.
# Identity (username/email), credentials, level/team, api_tokens and
# calendars are intentionally NOT touched on the merge path — those are
# instance-local connection state, not portable content.
_MERGE_CONTENT_FIELDS = (
    "display_name",
    "timezone",
    "task_row_hidden",
    "task_row_hidden_by_view",
    "inspector_layout",
    "inspector_hidden",
    "completed_task_retention_days",
    "trash_retention_days",
    "collapsed_items",
    "scratchpad",
    "editor_vim_mode",
    "today_grouping",
    "today_filter_tag",
    "today_days",
    "today_view_mode",
    "sidebar_perspectives",
    "favorites",
)


class BackupError(Exception):
    """Raised on a malformed bundle or an impossible import target."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def build_export(user, *, include_trashed: bool = True,
                 redact_secrets: bool = False) -> dict:
    """Serialize a user + all their items into a backup bundle (a dict).

    ``include_trashed`` pulls soft-deleted items too (via
    ``Item.all_objects``) so a backup is genuinely complete.
    ``redact_secrets`` strips credentials for the user-facing GUI download.
    """
    user_dict = json.loads(user.to_json())
    if redact_secrets:
        _redact_user(user_dict)

    manager = Item.all_objects if include_trashed else Item.objects
    items = [json.loads(it.to_json()) for it in manager(owner=user)]

    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": _utcnow().isoformat(),
        "user": user_dict,
        "items": items,
    }


def _redact_user(user_dict: dict) -> None:
    for field in _SECRET_USER_FIELDS:
        user_dict.pop(field, None)
    for cal in user_dict.get("calendars") or []:
        for field in _SECRET_CALENDAR_FIELDS:
            cal.pop(field, None)


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #


def import_bundle(payload: dict, *, replace: bool = False,
                  target_username: str | None = None) -> dict:
    """Restore a bundle. Returns a stats dict.

    ``target_username`` forces the import into that existing user (ownership
    of every item is remapped to them). Without it, the bundle is restored
    to its original user — matched by ``_id`` then username, or created if
    neither exists.
    """
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise BackupError("not a Cairn backup file")

    user_dict = dict(payload.get("user") or {})
    items = payload.get("items") or []
    if not user_dict:
        raise BackupError("backup has no user section")
    bundle_oid = (user_dict.get("_id") or {}).get("$oid")

    target, created = _resolve_target(user_dict, bundle_oid, target_username)
    new_oid = str(target.pk)

    deleted = 0
    if replace:
        deleted = Item.all_objects(owner=target).delete()

    # Bypass Item.save() on the way in: writing the raw mongo doc preserves
    # created_at/updated_at/completed_* exactly (save() would re-stamp them).
    coll = Item._get_collection()
    imported = 0
    for raw in items:
        doc = Item.from_json(json.dumps(_remap_item(dict(raw), bundle_oid, new_oid)))
        mongo_doc = doc.to_mongo()
        coll.replace_one({"_id": mongo_doc["_id"]}, mongo_doc, upsert=True)
        imported += 1

    return {
        "user_created": created,
        "username": target.username,
        "items_imported": imported,
        "items_deleted": deleted,
    }


def _resolve_target(user_dict, bundle_oid, target_username):
    """Find (or create) the user the bundle should land on.

    Returns ``(user, created)``.
    """
    if target_username:
        target = User.objects(username=target_username).first()
        if not target:
            raise BackupError(f"no user '{target_username}'")
        _merge_user(target, user_dict)
        return target, False

    target = None
    if bundle_oid:
        target = User.objects(pk=bundle_oid).first()
    if target is None and user_dict.get("username"):
        target = User.objects(username=user_dict["username"]).first()

    if target is None:
        return _create_user(user_dict), True
    _merge_user(target, user_dict)
    return target, False


def _create_user(user_dict: dict):
    """Create the user verbatim, preserving the original ``_id``."""
    # ``created=True`` so the following save() inserts; without it mongoengine
    # treats the doc as already-persisted and save() becomes a no-op UPDATE
    # against an _id that isn't there yet.
    user = User.from_json(json.dumps(user_dict), created=True)
    if not user.password_hash:
        # Redacted (GUI) export carries no hash — set an unusable random one;
        # the operator hands the user a reset link afterwards.
        user.password_hash = generate_password_hash(secrets.token_urlsafe(24))
    user.save()
    return user


def _merge_user(target, user_dict: dict) -> None:
    """Overlay portable content onto an existing user; keep identity,
    credentials, level/team, api_tokens and calendars untouched."""
    parsed = User.from_json(json.dumps(user_dict))
    for field in _MERGE_CONTENT_FIELDS:
        if field in user_dict:
            setattr(target, field, getattr(parsed, field))
    target.tags = parsed.tags
    target.custom_perspectives = parsed.custom_perspectives
    target.save()


def _remap_item(item: dict, old_oid: str | None, new_oid: str) -> dict:
    """Rewrite user references so an item lands on the target user.

    No-op when restoring to the original ``_id``. When the owner changes,
    instance-local share/calendar links from the source are also cleared —
    they're meaningless (and ``share_token`` is unique) on another account.
    """
    if not old_oid or old_oid == new_oid:
        return item

    def _fix(ref):
        if isinstance(ref, dict) and ref.get("$oid") == old_oid:
            ref["$oid"] = new_oid

    for field in ("owner", "assigned_to", "assigned_by", "completed_by"):
        _fix(item.get(field))
    for member in item.get("members") or []:
        _fix(member)
    for comment in item.get("comments") or []:
        _fix(comment.get("author"))

    item.pop("share_token", None)
    item["share_enabled"] = False
    item.pop("gcal_due_event_id", None)
    item.pop("gcal_plan_event_id", None)
    return item
