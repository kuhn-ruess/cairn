"""The public booking page — ``/book/<slug>``, no login.

Three routes and a deliberate division of labour between them:

* ``page`` renders the shell. It knows nothing about times, because the
  visitor's timezone lives in the browser and nowhere else.
* ``slots`` answers with UTC instants as JSON. The browser formats them in
  its own zone and groups them into days, which is why the server never
  renders a time on this page.
* ``book`` takes one of those instants back and confirms it.

No CSRF token: the form is filled in by people who have never seen Cairn and
hold no session. What guards it instead is the rate limit, the notice window,
and the fact that the worst a forged POST achieves is an appointment the owner
can see and delete.
"""
from datetime import datetime, timezone

from flask import (
    Blueprint, abort, current_app, jsonify, render_template, request,
)

from application import limiter
from application.models.user import User
from application.services.booking import BookingError, confirm, slots_for

booking_bp = Blueprint("booking", __name__, url_prefix="/book")


def _host_or_404(slug: str):
    user = User.objects(booking__slug=(slug or "").strip().lower()).first()
    if user is None or not user.booking or not user.booking.is_live():
        abort(404)
    return user


@booking_bp.route("/<slug>")
def page(slug):
    user = _host_or_404(slug)
    from application.services.tz import user_zone
    return render_template(
        "public/booking.html", host=user, page=user.booking,
        host_name=user.display_name or user.username,
        host_tz=str(user_zone(user)),
        durations=user.booking.ordered_durations(),
    )


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
    try:
        found = slots_for(user, duration)
    except BookingError as exc:
        # A calendar that won't answer means we don't know what's free. Say so
        # rather than offering times we can't stand behind.
        return jsonify({"error": str(exc)}), 503
    return jsonify({
        "duration": duration,
        "slots": [s.isoformat() for s in found],
    })


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

    try:
        booking = confirm(user, start=start, duration_minutes=duration, name=name,
                          email=email, note=(form.get("note") or "").strip(),
                          guest_timezone=(form.get("tz") or "").strip())
    except BookingError as exc:
        return _error(user, str(exc))

    from application.services.booking import announce
    announce(user, booking)
    return render_template("public/booking_done.html", host=user,
                           host_name=user.display_name or user.username,
                           booking=booking)


def _error(user, message: str):
    from application.services.tz import user_zone
    return render_template(
        "public/booking.html", host=user, page=user.booking,
        host_name=user.display_name or user.username,
        host_tz=str(user_zone(user)),
        durations=user.booking.ordered_durations(),
        error=message), 400
