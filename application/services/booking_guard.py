"""What keeps the public booking form from being an open pipe into a calendar.

Every check here answers one question: is this POST a person who read the
page, or a script that found the URL? They are deliberately layered, because
each one alone is trivially beaten:

* the **form token** is a signed timestamp handed out when the page renders. A
  POST without one never loaded the page (curl against the endpoint, a form
  scraped once and replayed forever), and one that comes back within a second
  or two wasn't typed by a person. It carries the slug, so a token minted on
  one page is worth nothing on another.
* the **honeypot** is a field no browser shows and no person fills. A script
  that posts every input it finds fills it.
* the **page ceiling** counts what was actually created, per page, no matter
  who created it — the one limit a rotating set of IP addresses cannot walk
  around. The per-IP rate limit on the route stops one machine; this stops a
  hundred.
* the **address ceiling** bounds one email address to a few open appointments.
  It is an anti-spam rule and the reason this page can't be used to mailbomb a
  stranger with confirmations they never asked for.

None of it is a CAPTCHA and none of it asks the visitor for anything, because
the page's whole job is that somebody who has never seen Cairn can book in
twenty seconds. The worst case is chosen, not overlooked: a determined flood
exhausts the page ceiling and the page then refuses bookings for an hour. A
page that says "try later" is a better failure than a calendar full of junk
appointments the host has to delete by hand — and the host is told, because a
refusal writes a warning to the log.
"""
from datetime import datetime, timedelta, timezone

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from application.services.booking import BookingError

# The field the form hides and a bot fills. Named like something a form would
# plausibly ask for — "honeypot" would be a giveaway.
HONEYPOT_FIELD = "company"

_SALT = "cairn-booking-form"

# One message for every "your form is wrong" case. A script learns nothing
# from it, and a person only ever needs the one instruction it gives.
_RELOAD = "Please reload the page and pick your time again."


def _signer() -> TimestampSigner:
    return TimestampSigner(current_app.config["SECRET_KEY"], salt=_SALT)


def issue_form_token(slug: str) -> str:
    """The token the rendered form carries. Minted per page render."""
    return _signer().sign((slug or "").strip().lower()).decode("ascii")


def check_form_token(slug: str, token: str) -> None:
    ttl = int(current_app.config.get("BOOKING_FORM_TTL_HOURS") or 12) * 3600
    min_age = int(current_app.config.get("BOOKING_MIN_FORM_SECONDS") or 0)
    if not token:
        raise BookingError(_RELOAD)
    try:
        value, stamped = _signer().unsign(
            token, max_age=ttl, return_timestamp=True)
    except SignatureExpired as exc:
        raise BookingError(
            "This page has been open for a while — please reload it and pick "
            "your time again.") from exc
    except BadSignature as exc:
        raise BookingError(_RELOAD) from exc
    if value.decode("utf-8", "replace") != (slug or "").strip().lower():
        raise BookingError(_RELOAD)
    if min_age and (datetime.now(timezone.utc) - stamped).total_seconds() < min_age:
        # Re-rendering the page hands out a fresh token, so a person who
        # really did fill it in that fast just submits again.
        raise BookingError("That was quick — please submit the form once more.")


def check_honeypot(value: str) -> None:
    if (value or "").strip():
        raise BookingError(_RELOAD)


def check_flood(user, email: str) -> None:
    """The two ceilings that don't care where the request came from."""
    from application.models.booking import Booking

    now = datetime.now(timezone.utc)
    per_hour = int(current_app.config.get("BOOKING_MAX_NEW_PER_HOUR") or 0)
    per_day = int(current_app.config.get("BOOKING_MAX_NEW_PER_DAY") or 0)
    per_email = int(current_app.config.get("BOOKING_MAX_OPEN_PER_EMAIL") or 0)

    for limit, span, label in ((per_hour, timedelta(hours=1), "hour"),
                               (per_day, timedelta(days=1), "day")):
        if not limit:
            continue
        made = Booking.objects(owner=user,
                               created_at__gte=now - span).count()
        if made >= limit:
            current_app.logger.warning(
                "Booking page %s refused a booking: %s taken in the last %s "
                "(limit %s) — the page may be under a flood.",
                getattr(user.booking, "slug", "?"), made, label, limit)
            raise BookingError(
                "This page has taken a lot of bookings just now. Please try "
                "again later.")

    address = (email or "").strip()
    if per_email and address:
        open_ones = Booking.objects(owner=user, start__gte=now,
                                    email__iexact=address).count()
        if open_ones >= per_email:
            raise BookingError(
                "There are already several appointments booked for that email "
                "address. Please reply to your confirmation instead.")
