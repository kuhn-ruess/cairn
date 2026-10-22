"""The public booking pages — ``/book`` and ``/book/<slug>``, no login.

A deliberate division of labour between the routes:

* ``index`` lists everyone who switched their page on and asked to be listed.
* ``page`` renders one person's shell. It knows nothing about times, because
  the visitor's timezone lives in the browser and nowhere else.
* ``slots`` answers with UTC instants as JSON. The browser formats them in
  its own zone and groups them into days, which is why the server never
  renders a time on this page.
* ``book`` takes one of those instants back and confirms it.

The HOST, logged in and looking at their own page, gets one thing a visitor
never does: ``slots`` also answers with the events that took the missing days
away. It is the question the page otherwise can't answer — "why is Tuesday
gone?" — and it is why ``_is_owner`` is checked on every single read of it.
Never inside a frame: an embed is somebody else's site, and a calendar's
titles have no business being rendered there.

No CSRF token: the form is filled in by people who have never seen Cairn and
hold no session. What guards it instead is ``services/booking_guard.py`` — a
signed form token minted when the page renders, a honeypot field, and ceilings
on what one page and one email address may take — on top of the per-IP rate
limit, the notice window, and the fact that the worst a forged POST achieves
is an appointment the owner can see and delete.
"""
from datetime import datetime, timezone

from flask import (
    Blueprint, Response, abort, current_app, jsonify, render_template, request,
)

from application import limiter
from application.models.user import User
from application.services.booking import BookingError, confirm, slots_for

booking_bp = Blueprint("booking", __name__, url_prefix="/book")


@booking_bp.after_request
def _allow_framing(resp):
    """Let these pages be embedded in somebody else's site.

    Only this blueprint: the rest of Cairn is a logged-in app and has every
    reason to refuse being framed. ``frame-ancestors`` also has to be here
    rather than in the proxy, because a browser that sees it ignores the
    ``X-Frame-Options: SAMEORIGIN`` the proxy sends for everything — which is
    the header that would otherwise make an embed show an empty box.

    Only an embed (``?embed=1``, which the form POST inside the frame carries
    too) is opened up. The page on its own address is not meant to sit in
    somebody's frame without saying so, and keeps the same-origin rule."""
    if not request.args.get("embed"):
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        return resp
    resp.headers.pop("X-Frame-Options", None)
    resp.headers["Content-Security-Policy"] = (
        "frame-ancestors " + (current_app.config.get("BOOKING_FRAME_ANCESTORS") or "*"))
    return resp


def _host_or_404(slug: str):
    user = User.objects(booking__slug=(slug or "").strip().lower()).first()
    if user is None or not user.booking or not user.booking.is_live():
        abort(404)
    return user


@booking_bp.route("/")
def index():
    """Everyone who has switched their page on and asked to be listed.

    One query, and the portraits are fetched by the browser from
    ``/book/<slug>/photo`` — so a directory of fifty people is still one
    document read, not fifty blobs rendered into the page.

    ``listed`` is filtered HERE and not in the query on purpose: a field left
    at its default is never written to the document, so ``booking.listed:
    true`` matches nobody who simply left the switch alone. The default only
    exists once the document is loaded, which is where the test for it
    belongs."""
    hosts = [u for u in User.objects(booking__enabled=True)
             .only("username", "display_name", "booking")
             if u.booking and u.booking.is_live() and u.booking.listed]
    hosts.sort(key=lambda u: (u.display_name or u.username).lower())
    return render_template("public/booking_index.html", hosts=hosts)


@booking_bp.route("/<slug>")
def page(slug):
    return _render_page(_host_or_404(slug))


def _is_owner(user) -> bool:
    """Is the person reading this the host themselves?

    ``embed`` is a hard no, not an oversight: the page is then rendered inside
    a site Cairn doesn't control, and what blocks the host's calendar is not
    for that context.
    """
    from flask_login import current_user

    if request.args.get("embed"):
        return False
    return bool(getattr(current_user, "is_authenticated", False)
                and getattr(current_user, "pk", None) == user.pk)


def _render_page(user, error=None, status=200):
    from application.models.booking import BookingPhoto
    from application.services.booking_guard import HONEYPOT_FIELD, issue_form_token
    from application.services.tz import user_zone
    html = render_template(
        "public/booking.html", host=user, page=user.booking,
        is_owner=_is_owner(user),
        # Minted per render, so an error page hands out a fresh one and the
        # visitor who tripped a check can simply submit again.
        form_token=issue_form_token(user.booking.slug),
        honeypot=HONEYPOT_FIELD,
        host_name=user.display_name or user.username,
        host_tz=str(user_zone(user)),
        durations=user.booking.ordered_durations(),
        has_photo=bool(BookingPhoto.objects(owner=user).only("id").first()),
        error=error,
        # Embedded: the surrounding site supplies the page, so this one drops
        # its outer margins and reports its height to the frame.
        **_dress(),
    )
    return html if status == 200 else (html, status)


@booking_bp.route("/<slug>/photo")
def photo(slug):
    from application.models.booking import BookingPhoto
    user = _host_or_404(slug)
    shot = BookingPhoto.objects(owner=user).first()
    if shot is None:
        abort(404)
    return Response(bytes(shot.data), mimetype=shot.content_type,
                    headers={"Cache-Control": "public, max-age=3600"})


@booking_bp.route("/<slug>/slots")
@limiter.limit(lambda: current_app.config["RATELIMIT_BOOKING_SLOTS"],
               methods=["GET"])
def slots(slug):
    user = _host_or_404(slug)
    try:
        duration = int(request.args.get("duration") or 0)
    except ValueError:
        duration = 0
    if duration not in user.booking.ordered_durations():
        return jsonify({"error": "Unknown appointment length."}), 400
    # Imported here, not at the top: the seam every other caller patches is
    # the service module, and a name bound at import time isn't that seam.
    from application.services.booking import (
        blocking_spans, bookings_in, busy_events, busy_spans, day_states,
        window_for,
    )
    from application.services.tz import user_zone

    mine = _is_owner(user)
    # One window for the whole answer: the slots, the events and the per-day
    # verdicts all have to be about the same span, and each call to
    # ``window_for`` starts its own clock.
    since, until = window_for(user)
    host_tz = user_zone(user)
    try:
        # The owner's calendars are read ONCE and the spans handed on, so
        # their page costs no more requests than a visitor's — and what the
        # page lists is the same spans the slot maths used.
        events = busy_events(user, user.booking, since, until) if mine else None
        spans = busy_spans(events, host_tz) if events is not None else None
        found = slots_for(
            user, duration, now=since,
            busy=blocking_spans(events, host_tz) if events is not None else None)
    except BookingError as exc:
        # A calendar that won't answer means we don't know what's free. Say so
        # rather than offering times we can't stand behind.
        return jsonify({"error": str(exc)}), 503
    answer = {
        "duration": duration,
        "slots": [s.isoformat() for s in found],
    }
    if mine:
        booked = bookings_in(user, since, until)
        # Only what actually takes time off the page. An entry the calendar
        # shows as free changes nothing about the day, so naming it here would
        # just be one more line to read past.
        answer["busy"] = [{
            "start": span[0].isoformat(),
            "end": span[1].isoformat(),
            "title": e.title or "Busy",
            "all_day": bool(e.all_day),
            "calendar": e.calendar_name,
            "color": e.calendar_color,
        } for e, span in sorted(
            [pair for pair in zip(events, spans) if getattr(pair[0], "busy", True)],
            key=lambda pair: pair[1][0])
        ] + [{
            # An appointment booked through this page blocks the slot just as
            # hard, and it is the one kind the host can't see in a calendar
            # they may not even have connected as a busy one.
            "start": b.start.isoformat(),
            "end": b.end.isoformat(),
            "title": b.name or b.email or "Booked",
            "all_day": False,
            "calendar": "Booked here",
            "color": "#8ec07c",
        } for b in booked]
        # …and why a day offers nothing even where the list above leaves room.
        answer["days"] = day_states(
            user.booking, duration, busy=blocking_spans(events, host_tz),
            booked=[(b.start, b.end) for b in booked],
            now=since, host_tz=host_tz)
        answer["rules"] = {
            "min_notice_hours": int(user.booking.min_notice_hours or 0),
            "buffer_minutes": int(user.booking.buffer_minutes or 0),
            "max_per_day": int(user.booking.max_per_day or 0),
        }
        # The hours the day is measured against. Without them "booked solid"
        # is a verdict the host cannot check: a window trimmed to half an hour
        # looks exactly like a calendar full of meetings.
        answer["hours"] = {
            str(d): [[w.start_hhmm, w.end_hhmm] for w in user.booking.windows_for(d)]
            for d in range(7)
        }
    return jsonify(answer)


@booking_bp.route("/<slug>/book", methods=["POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_BOOKING"],
               methods=["POST"])
def book(slug):
    user = _host_or_404(slug)
    form = request.form
    # Before anything is read out of the form: did this come from the page?
    from application.services.booking_guard import (
        HONEYPOT_FIELD, check_flood, check_form_token, check_honeypot,
    )
    try:
        check_honeypot(form.get(HONEYPOT_FIELD))
        check_form_token(slug, form.get("form_token") or "")
    except BookingError as exc:
        return _error(user, str(exc))

    name = (form.get("name") or "").strip()
    email = (form.get("email") or "").strip()
    if not name or "@" not in email:
        return _error(user, "Please give your name and a valid email address.")
    try:
        start = datetime.fromisoformat((form.get("start") or "").replace("Z", "+00:00"))
    except ValueError:
        return _error(user, "That time couldn't be read — please pick a slot again.")
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    try:
        duration = int(form.get("duration") or 0)
    except ValueError:
        duration = 0

    # A link the visitor brought. Checked here rather than taken as typed:
    # it is shown as a clickable link on the host's settings page, so a
    # "javascript:" one would be a trap laid by a stranger.
    meeting_url = (form.get("meeting_url") or "").strip()
    if meeting_url and not meeting_url.startswith(("http://", "https://")):
        return _error(user, "The meeting link has to start with http:// or https://.")
    if len(meeting_url) > 500:
        return _error(user, "That meeting link is too long.")

    # Sent twice: the confirmation reloaded, or the form submitted again while
    # the first request was still at work. That slot is then the visitor's
    # own, and "just taken" would tell them a booking that went through had
    # failed. They see the confirmation again; nothing is booked or mailed.
    from application.services.booking import recent_duplicate
    again = recent_duplicate(user, start=start, name=name, email=email)
    if again is not None:
        return _done(user, again)

    try:
        # Last, because it is the only check that costs a query — and it sits
        # inside the same handler, so a ceiling reads like any other refusal.
        check_flood(user, email)
        booking = confirm(user, start=start, duration_minutes=duration, name=name,
                          email=email, note=(form.get("note") or "").strip(),
                          guest_timezone=(form.get("tz") or "").strip(),
                          meeting_url=meeting_url)
    except BookingError as exc:
        return _error(user, str(exc))

    from application.services.booking import announce
    announce(user, booking)
    return _done(user, booking)


def _done(user, booking):
    return render_template("public/booking_done.html", host=user,
                           host_name=user.display_name or user.username,
                           booking=booking, **_dress())


def _dress() -> dict:
    """What the URL asked the page to look like (``services/booking_embed``),
    ready for the template: the options, the wording, and the query the form
    has to carry so the confirmation stays dressed the same way."""
    from application.services import booking_embed
    opts = booking_embed.options(request.args)
    theme = _theme()
    return dict(opts, theme=theme,
                t=booking_embed.translator(opts["lang"]),
                js_strings=booking_embed.js_strings(opts["lang"]),
                carry=booking_embed.carried(opts, theme))


def _theme() -> str:
    """``light``/``dark`` if the embed URL insists, else "" — which means the
    page asks the visitor's browser."""
    wanted = (request.args.get("theme") or "").strip().lower()
    return wanted if wanted in ("light", "dark") else ""


def _error(user, message: str):
    return _render_page(user, error=message, status=400)
