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
  that posts every input it finds fills it. Its name is chosen so that no
  BROWSER fills it either: it was once ``company``, and autofilling name and
  email two rows above put a company in it and refused a real booking.
* the **page ceiling** counts what was actually created, per page, no matter
  who created it — the one limit a rotating set of IP addresses cannot walk
  around. The per-IP rate limit on the route stops one machine; this stops a
  hundred.
* the **address ceiling** bounds how often one email address may book PER DAY.
  Each booking mails that address a confirmation, so this is what stops the
  page being used to fill a stranger's inbox. It counts bookings MADE, not
  appointments held: a client booking a term's worth of sessions is not what
  this is looking for.

None of it is a CAPTCHA and none of it asks the visitor for anything, because
the page's whole job is that somebody who has never seen Cairn can book in
twenty seconds. The worst case is chosen, not overlooked: a determined flood
exhausts the page ceiling and the page then refuses bookings for an hour. A
page that says "try later" is a better failure than a calendar full of junk
appointments the host has to delete by hand — and the host is told, because a
refusal writes a warning to the log.
"""
from datetime import datetime, timedelta, timezone

from flask import current_app, request
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from application.services.booking import BookingError

# The field the form hides and a bot fills. The name matters twice over: it
# must mean nothing to a bot, and nothing to a BROWSER either. "company" was
# both — Safari and every password manager fill a company field when the
# visitor autofills their name and email two rows above, and the guard then
# refused a real person. Nothing in this name is in an autofill vocabulary.
HONEYPOT_FIELD = "note_2"

_SALT = "cairn-booking-form"

# One message for every "your form is wrong" case. A script learns nothing
# from it, and a person only ever needs the one instruction it gives. WHICH
# check refused goes to the log instead — the visitor must not be told, but a
# refusal nobody can explain afterwards is how a guard quietly eats real
# bookings.
_RELOAD = "Please reload the page and pick your time again."


def _refuse(why: str, message: str = _RELOAD) -> BookingError:
    current_app.logger.warning(
        "Booking form refused (%s): %s %s from %s", why,
        request.method if request else "?",
        request.path if request else "?",
        (request.remote_addr if request else "?"))
    return BookingError(message)


def _signer() -> TimestampSigner:
    return TimestampSigner(current_app.config["SECRET_KEY"], salt=_SALT)


def issue_form_token(slug: str) -> str:
    """The token the rendered form carries. Minted per page render."""
    return _signer().sign((slug or "").strip().lower()).decode("ascii")


def check_form_token(slug: str, token: str) -> None:
    ttl = int(current_app.config.get("BOOKING_FORM_TTL_HOURS") or 12) * 3600
    min_age = int(current_app.config.get("BOOKING_MIN_FORM_SECONDS") or 0)
    if not token:
        # Also what a page rendered before this guard existed looks like, and
        # what a browser sends from a cached copy of one.
        raise _refuse("no form token — the page predates the guard, or the "
                      "POST never loaded it")
    try:
        value, stamped = _signer().unsign(
            token, max_age=ttl, return_timestamp=True)
    except SignatureExpired as exc:
        raise _refuse(
            "form token older than the TTL",
            "This page has been open for a while — please reload it and pick "
            "your time again.") from exc
    except BadSignature as exc:
        raise _refuse("form token doesn't verify — forged, or the "
                      "SECRET_KEY changed under it") from exc
    if value.decode("utf-8", "replace") != (slug or "").strip().lower():
        raise _refuse("form token was minted for another booking page")
    if min_age and (datetime.now(timezone.utc) - stamped).total_seconds() < min_age:
        # Re-rendering the page hands out a fresh token, so a person who
        # really did fill it in that fast just submits again.
        raise _refuse("form came back in under BOOKING_MIN_FORM_SECONDS",
                      "That was quick — please submit the form once more.")


def check_honeypot(value: str) -> None:
    if (value or "").strip():
        raise _refuse("honeypot field was filled in")


def check_flood(user, email: str) -> None:
    """The two ceilings that don't care where the request came from."""
    from application.models.booking import Booking

    now = datetime.now(timezone.utc)
    per_hour = int(current_app.config.get("BOOKING_MAX_NEW_PER_HOUR") or 0)
    per_day = int(current_app.config.get("BOOKING_MAX_NEW_PER_DAY") or 0)
    per_email = int(current_app.config.get("BOOKING_MAX_PER_EMAIL_PER_DAY") or 0)

    for limit, span, label in ((per_hour, timedelta(hours=1), "hour"),
                               (per_day, timedelta(days=1), "day")):
        if not limit:
            continue
        made = Booking.objects(owner=user,
                               created_at__gte=now - span).count()
        if made >= limit:
            raise _refuse(
                f"page ceiling: {made} taken in the last {label} "
                f"(limit {limit}) — this page may be under a flood",
                "This page has taken a lot of bookings just now. Please try "
                "again later.")

    # Counted over what this address BOOKED TODAY, not over what it holds.
    # The rule is about the rate a stranger's inbox can be filled at, and a
    # client who books six sessions for the term is not that — the first cut
    # of this counted open future appointments and locked out exactly the
    # people the page exists for.
    address = (email or "").strip()
    if per_email and address:
        today = Booking.objects(owner=user, email__iexact=address,
                                created_at__gte=now - timedelta(days=1)).count()
        if today >= per_email:
            raise _refuse(
                f"address ceiling: {today} bookings from this address in the "
                f"last day (limit {per_email})",
                "That email address has booked several times today already. "
                "Please try again tomorrow, or reply to your confirmation.")
