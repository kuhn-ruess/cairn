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
hold no session. What guards it instead is the rate limit, the notice window,
and the fact that the worst a forged POST achieves is an appointment the owner
can see and delete.
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
    the header that would otherwise make an embed show an empty box."""
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
    from application.services.tz import user_zone
    html = render_template(
        "public/booking.html", host=user, page=user.booking,
        is_owner=_is_owner(user),
        host_name=user.display_name or user.username,
        host_tz=str(user_zone(user)),
        durations=user.booking.ordered_durations(),
        has_photo=bool(BookingPhoto.objects(owner=user).only("id").first()),
        error=error,
        # Embedded: the surrounding site supplies the page, so this one drops
        # its outer margins and reports its height to the frame.
        embed=bool(request.args.get("embed")),
        theme=_theme(),
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
    from application.services.booking import busy_events, window_for

    mine = _is_owner(user)
    try:
        # The owner's calendars are read ONCE and the intervals handed on, so
        # their page costs no more requests than a visitor's.
        events = (busy_events(user, user.booking, *window_for(user))
                  if mine else None)
        found = slots_for(
            user, duration,
            busy=[(e.start, e.end) for e in events] if events is not None else None)
    except BookingError as exc:
        # A calendar that won't answer means we don't know what's free. Say so
        # rather than offering times we can't stand behind.
        return jsonify({"error": str(exc)}), 503
    answer = {
        "duration": duration,
        "slots": [s.isoformat() for s in found],
    }
    if mine:
        answer["busy"] = [{
            "start": e.start.isoformat(),
            "end": e.end.isoformat(),
            "title": e.title or "Busy",
            "all_day": bool(e.all_day),
            "calendar": e.calendar_name,
            "color": e.calendar_color,
        } for e in sorted(events, key=lambda e: e.start)]
    return jsonify(answer)


@booking_bp.route("/<slug>/book", methods=["POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_BOOKING"],
               methods=["POST"])
def book(slug):
    user = _host_or_404(slug)
    form = request.form
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

    try:
        booking = confirm(user, start=start, duration_minutes=duration, name=name,
                          email=email, note=(form.get("note") or "").strip(),
                          guest_timezone=(form.get("tz") or "").strip(),
                          meeting_url=meeting_url)
    except BookingError as exc:
        return _error(user, str(exc))

    from application.services.booking import announce
    announce(user, booking)
    return render_template("public/booking_done.html", host=user,
                           host_name=user.display_name or user.username,
                           booking=booking,
                           embed=bool(request.args.get("embed")),
                           theme=_theme())


def _theme() -> str:
    """``light``/``dark`` if the embed URL insists, else "" — which means the
    page asks the visitor's browser."""
    wanted = (request.args.get("theme") or "").strip().lower()
    return wanted if wanted in ("light", "dark") else ""


def _error(user, message: str):
    return _render_page(user, error=message, status=400)
