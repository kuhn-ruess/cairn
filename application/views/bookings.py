"""The appointments people booked through the public page.

Separate from Settings → Booking page on purpose: that form is where the
page is CONFIGURED — slug, hours, calendars, portrait — and it is touched
once. What arrives through it is worked with, so it belongs in the main
menu beside the other lists.

Cancelling lives here for the same reason. The heavy lifting (calendar
entry, the guest's .ics, the row) is ``services/booking.cancel``; this
module only decides who may ask.
"""
from datetime import datetime, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from application.models.booking import Booking

bookings_bp = Blueprint("bookings", __name__, url_prefix="/bookings")


def upcoming_count(user) -> int:
    """For the sidebar badge. Asked only when the page is live, so a user
    without a booking page costs no query at all."""
    return Booking.objects(owner=user,
                           start__gte=datetime.now(timezone.utc)).count()


@bookings_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()
    upcoming = Booking.objects(
        owner=user, start__gte=datetime.now(timezone.utc)).order_by("start")
    return render_template("bookings/index.html", upcoming=upcoming,
                           page=user.booking)


@bookings_bp.route("/<booking_id>/cancel", methods=["POST"])
@login_required
def cancel(booking_id):
    """Call off one appointment.

    Owner-scoped by the query itself rather than by a check afterwards: a
    booking that isn't this user's simply isn't found."""
    from application.services.booking import cancel as cancel_booking

    user = current_user._get_current_object()
    booking = Booking.objects(pk=booking_id, owner=user).first()
    if booking is None:
        abort(404)
    who = booking.name or booking.email or "the guest"
    warnings = cancel_booking(user, booking,
                              notify=bool(request.form.get("notify")))
    flash(f"Appointment with {who} cancelled." if not warnings
          else f"Appointment with {who} cancelled. " + " ".join(warnings),
          "info" if not warnings else "warning")
    return redirect(url_for("bookings.index"))
