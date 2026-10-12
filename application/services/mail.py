"""Minimal SMTP sender — no extra dependencies.

Configured entirely via env vars (prefix ``BKTODO_SMTP_*``):

* ``BKTODO_SMTP_HOST`` — required to enable mail (e.g. smtp.gmail.com)
* ``BKTODO_SMTP_PORT`` — default 587
* ``BKTODO_SMTP_USER`` / ``BKTODO_SMTP_PASSWORD`` — optional
* ``BKTODO_SMTP_FROM`` — sender address; defaults to the SMTP user
* ``BKTODO_SMTP_TLS`` — "1" for STARTTLS (default), "0" to disable
* ``BKTODO_SMTP_SSL`` — "1" for implicit TLS. Left empty it is inferred from
  the port, because 465 means implicit TLS everywhere and a deployment that
  sets that port and nothing else should simply work.

``deliver`` raises and ``send_email`` swallows. Both exist because the two
callers want opposite things: a booking confirmation must never take down the
request it rides on, and ``./cairn test-mail`` is worth nothing unless it can
say what the server actually answered.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from flask import current_app

log = logging.getLogger(__name__)


def smtp_configured() -> bool:
    return bool(current_app.config.get("SMTP_HOST"))


def send_email(to: str, subject: str, body: str, html: str | None = None,
               attachments: list[tuple[str, str, bytes]] | None = None) -> bool:
    """Send a single email. Returns True on success, False otherwise.

    Returning False instead of raising lets the caller fall back to a
    "tell the user to ask the admin" UX without blowing up the request.

    ``attachments`` are ``(filename, mimetype, bytes)`` — what the booking
    page needs to put an ``.ics`` beside the confirmation.
    """
    if not smtp_configured():
        log.info("SMTP not configured — skipping email to %s", to)
        return False
    try:
        deliver(to, subject, body, html, attachments)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("Email send failed: %s", exc)
        return False


def deliver(to: str, subject: str, body: str, html: str | None = None,
            attachments: list[tuple[str, str, bytes]] | None = None) -> None:
    """Like ``send_email``, but the failure is the caller's to handle."""
    settings = smtp_settings()
    if not settings["host"]:
        raise RuntimeError("BKTODO_SMTP_HOST is not set — mail is switched off.")
    msg = _build(settings["sender"], to, subject, body, html, attachments)
    opener = smtplib.SMTP_SSL if settings["ssl"] else smtplib.SMTP
    with opener(settings["host"], settings["port"], timeout=15) as smtp:
        if settings["starttls"] and not settings["ssl"]:
            smtp.starttls()
        if settings["user"] and settings["password"]:
            smtp.login(settings["user"], settings["password"])
        smtp.send_message(msg)


def smtp_settings() -> dict:
    """The resolved SMTP configuration — also what ``./cairn test-mail``
    prints, minus the password."""
    cfg = current_app.config
    port = int(cfg.get("SMTP_PORT") or 587)
    ssl_set = str(cfg.get("SMTP_SSL", "")).strip().lower()
    user = cfg.get("SMTP_USER") or ""
    return {
        "host": cfg.get("SMTP_HOST") or "",
        "port": port,
        "user": user,
        "password": cfg.get("SMTP_PASSWORD") or "",
        "sender": cfg.get("SMTP_FROM") or user or "cairn@localhost",
        # Implicit TLS talks TLS from the first byte; STARTTLS upgrades a
        # plain connection. Getting the two the wrong way round is a hang
        # rather than an error message, which is why the port decides when
        # nothing else was said.
        "ssl": ssl_set in ("1", "true", "yes") or (ssl_set == "" and port == 465),
        "starttls": str(cfg.get("SMTP_TLS", "1")).lower() not in ("0", "false", "no"),
    }


def _build(sender: str, to: str, subject: str, body: str, html, attachments):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, mimetype, data in (attachments or []):
        # ``text/calendar; method=REQUEST``: the parameters after the type
        # ride along, iMIP needs ``method`` on the part.
        mimetype, *params = [p.strip() for p in mimetype.split(";")]
        maintype, _, subtype = mimetype.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "plain",
                           filename=filename)
        for param in params:
            key, _, value = param.partition("=")
            if key and value:
                msg.get_payload()[-1].set_param(key.strip(), value.strip())
        # Attached bytes carry no charset of their own, so a text part would
        # be read as us-ascii — and a guest called Müller comes out broken in
        # the calendar entry.
        if maintype == "text":
            msg.get_payload()[-1].set_param("charset", "utf-8")
    return msg
