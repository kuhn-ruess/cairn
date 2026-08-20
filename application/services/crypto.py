"""Symmetric encryption for sensitive per-user values (OAuth tokens etc.).

The key is **derived** from two ingredients:

* ``SECRET_KEY`` from app config — the static, server-side secret
* the user's MongoDB ``_id``

So an attacker who only has the MongoDB dump (without the app config)
cannot decrypt anything; and the key is unique per user, so a leak of one
user's data plus the app secret doesn't compromise other users.

Legacy plaintext rows are tolerated transparently: ``decrypt_for_user``
returns the input unchanged when the token isn't a valid Fernet payload,
so existing tokens keep working until they're re-saved (and thus
re-encrypted).
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def _key_for(user_id: str) -> bytes:
    secret = current_app.config["SECRET_KEY"]
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    digest = hashlib.sha256(secret + b":bktodo-tokens:" + str(user_id).encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_for(scope, plaintext: str | None) -> str:
    """Encrypt against any document's id — a user, a team.

    The key ingredients are unchanged (server secret + the document's id); only
    the assumption that the document is a *user* is gone, because a team now
    holds a Zammad token too."""
    if not plaintext:
        return ""
    return Fernet(_key_for(str(scope.pk))).encrypt(
        plaintext.encode("utf-8")).decode("ascii")


def decrypt_for(scope, ciphertext: str | None) -> str:
    if not ciphertext:
        return ""
    try:
        return Fernet(_key_for(str(scope.pk))).decrypt(
            ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # Legacy plaintext (see the module docstring) — hand it back as-is.
        return ciphertext


def encrypt_for_user(user, plaintext: str | None) -> str:
    if not plaintext:
        return ""
    f = Fernet(_key_for(str(user.pk)))
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_for_user(user, ciphertext: str | None) -> str:
    if not ciphertext:
        return ""
    try:
        f = Fernet(_key_for(str(user.pk)))
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # Legacy plaintext — return as-is so the caller keeps working.
        # The next save will re-encrypt it.
        return ciphertext
