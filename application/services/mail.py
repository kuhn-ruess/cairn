"""Minimal SMTP sender — no extra dependencies.

Configured entirely via env vars (prefix ``BKTODO_SMTP_*``):

* ``BKTODO_SMTP_HOST`` — required to enable mail (e.g. smtp.gmail.com)
* ``BKTODO_SMTP_PORT`` — default 587
* ``BKTODO_SMTP_USER`` / ``BKTODO_SMTP_PASSWORD`` — optional
* ``BKTODO_SMTP_FROM`` — sender address; defaults to the SMTP user
* ``BKTODO_SMTP_TLS`` — "1" for STARTTLS (default), "0" to disable
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
    cfg = current_app.config
    host = cfg.get("SMTP_HOST")
    if not host:
        log.info("SMTP not configured — skipping email to %s", to)
        return False

    port = int(cfg.get("SMTP_PORT") or 587)
    user = cfg.get("SMTP_USER")
    password = cfg.get("SMTP_PASSWORD")
    sender = cfg.get("SMTP_FROM") or user or "cairn@localhost"
    use_tls = str(cfg.get("SMTP_TLS", "1")).lower() not in ("0", "false", "no")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, mimetype, data in (attachments or []):
        maintype, _, subtype = mimetype.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "plain",
                           filename=filename)
        # Attached bytes carry no charset of their own, so a text part would
        # be read as us-ascii — and a guest called Müller comes out broken in
        # the calendar entry.
        if maintype == "text":
            msg.get_payload()[-1].set_param("charset", "utf-8")

    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if use_tls:
                smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("Email send failed: %s", exc)
        return False
