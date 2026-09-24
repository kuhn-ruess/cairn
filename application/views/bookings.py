"""The appointments people booked through the public page.

Separate from Settings → Booking page on purpose: that form is where the
page is CONFIGURED — slug, hours, calendars, portrait — and it is touched
once. What arrives through it is worked with, so it belongs in the main
menu beside the other lists.

Cancelling lives here for the same reason. The heavy lifting (calendar
entry, the guest's .ics, the row) is ``services/booking.cancel``; this
module only decides who may ask.

**Billing a meeting happens here too, and only here.** A booking never books
itself into the time sheet: it is billed from this list, one at a time, the way
time is logged on a task — same picker, same entry rows, same cancel
(``services.timetracking.booking_time_context``). Two things follow from that
and both are the point:

* a call that was moved, shortened, lengthened or never held needs no special
  state, because nothing was booked on the strength of the appointment existing;
* the duration in the panel is the one that gets billed, pre-filled with what
  was booked. Correcting it keeps the correction on the appointment
  (``Booking.actual_minutes``), so the list afterwards says fifty minutes and
  not thirty.
"""
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from application.models.booking import Booking

bookings_bp = Blueprint("bookings", __name__, url_prefix="/bookings")

# How far back the list looks. The past half exists to be billed from, and a
# meeting nobody invoiced in three months is not going to be invoiced from a
# list — it is one date filter away in the report.
PAST_DAYS = 90


def upcoming_count(user) -> int:
    """For the sidebar badge. Asked only when the page is live, so a user
    without a booking page costs no query at all."""
    return Booking.objects(owner=user,
                           start__gte=datetime.now(timezone.utc)).count()


def _booking_or_404(user, booking_id):
    """Owner-scoped by the query itself rather than by a check afterwards: a
    booking that isn't this user's simply isn't found."""
    booking = Booking.objects(pk=booking_id, owner=user).first()
    if booking is None:
        abort(404)
    return booking


def _is_ajax() -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


def _page_context(user):
    """The two things every row needs, fetched once for the whole page."""
    from application.services.customers import customer_choices
    return {"customers": customer_choices(user),
            "now": datetime.now(timezone.utc)}


def _row(user, booking):
    """One re-rendered row — the answer to every mutating call here.

    ``panel_open`` because the row that comes back replaces the one that was
    open: closing the panel under somebody who just booked a meeting's time is
    the same bug as a render swap stealing the caret."""
    from application.services.timetracking import booking_time_context
    return render_template("bookings/_row.html", b=booking,
                           tt=booking_time_context(user, booking),
                           panel_open=True, **_page_context(user))


def _answer(user, booking, message: str = "", category: str = "info"):
    """Swap the row in place for a fetch, redirect for a plain form post.

    In-place, because the alternative is a full navigation per booking and a
    Back button that walks through every one of them."""
    if _is_ajax():
        return _row(user, booking)
    if message:
        flash(message, category)
    return redirect(url_for("bookings.index"))


@bookings_bp.route("/")
@login_required
def index():
    from application.services.timetracking import booking_time_context

    user = current_user._get_current_object()
    now = datetime.now(timezone.utc)
    upcoming = list(Booking.objects(owner=user, start__gte=now).order_by("start"))
    # Newest first: the call you just had is the one you are about to bill.
    past = list(Booking.objects(owner=user, start__lt=now,
                                start__gte=now - timedelta(days=PAST_DAYS))
                .order_by("-start"))
    tts = {str(b.pk): booking_time_context(user, b) for b in upcoming + past}
    return render_template("bookings/index.html", upcoming=upcoming, past=past,
                           tts=tts, page=user.booking, past_days=PAST_DAYS,
                           **_page_context(user))


@bookings_bp.route("/<booking_id>/customer", methods=["POST"])
@login_required
def set_customer(booking_id):
    """Say whose meeting this is — or correct what the domain match guessed.

    The domain map answers most of them when the booking is taken; this is the
    rest, and the only place a wrong guess is put right."""
    from application.services.customers import customer_for_user

    user = current_user._get_current_object()
    booking = _booking_or_404(user, booking_id)
    raw = (request.form.get("customer_id") or "").strip()
    booking.customer = customer_for_user(user, raw) if raw else None
    booking.save()
    return _answer(user, booking, "Appointment filed.", "success")


@bookings_bp.route("/<booking_id>/time", methods=["POST"])
@login_required
def log_time(booking_id):
    """Bill one meeting: write its time into the customer's day bucket.

    Nothing here is automatic and nothing is repeated on its own — press it
    twice and there are two entries, exactly as on a task. The duration is
    whatever the field says, which is how a call that ran long is booked
    correctly; when it differs from what was booked the difference is kept on
    the appointment so the list keeps saying so."""
    from application.services.timetracking import (
        add_entry, has_team, parse_duration, project_for_user,
    )

    user = current_user._get_current_object()
    booking = _booking_or_404(user, booking_id)
    if not has_team(user):
        abort(404)
    project = project_for_user(user, (request.form.get("project_id") or "").strip())
    if project is None:
        return _answer(user, booking, "Pick a billing project first.", "warning")
    minutes = parse_duration(request.form.get("duration") or "")
    if not minutes:
        return _answer(user, booking,
                       "Enter a duration (1h30m, 1:30, 90).", "warning")

    from application.services.tz import user_zone
    spent_on = booking.start.astimezone(user_zone(user))
    note = (request.form.get("note") or "").strip()
    add_entry(
        project=project, user=user, minutes=minutes, spent_on=spent_on,
        note=note or f"Meeting: {booking.name or booking.email or 'appointment'}",
        # A soft backlink, like a task's — the entry stays readable and the
        # panel can show what this appointment already booked. ``booking`` is
        # its own source kind; nothing turns it into a URL.
        source={"source_item_id": str(booking.pk), "source_kind": "booking",
                "source_label": (booking.name or booking.email or "")[:200]},
        # A plain checkbox: present means billable, absent means not. The task
        # bar is JS-driven and sends an explicit ``0``; this form isn't, and
        # reading it that way would make un-ticking the box do nothing.
        billable=request.form.get("billable") == "1",
    )
    # The correction belongs to the appointment, not only to the invoice.
    if minutes != int(booking.duration_minutes or 0):
        booking.actual_minutes = minutes
        booking.save()
    user.tt_last_project = str(project.id)
    user.save()
    return _answer(user, booking, "Time booked.", "success")


@bookings_bp.route("/<booking_id>/time/<bucket_id>/<entry_id>/cancel",
                   methods=["POST"])
@login_required
def cancel_time(booking_id, bucket_id, entry_id):
    """Take back one booked entry. Soft-cancelled in place, like everywhere in
    the time module — a billed (locked) entry refuses, and says so."""
    from application.models.timetracking import TimeBucket
    from application.services.timetracking import (
        cancel_entry, may_touch_entry, user_team_ids,
    )

    user = current_user._get_current_object()
    booking = _booking_or_404(user, booking_id)
    bucket = TimeBucket.objects(pk=bucket_id).first()
    if bucket is None or str(bucket.team.id) not in user_team_ids(user):
        abort(404)
    entry = next((e for e in bucket.entries if e.entry_id == entry_id), None)
    if entry is None:
        abort(404)
    if not may_touch_entry(user, bucket.team, entry):
        return _answer(user, booking,
                       "That entry is billed and locked.", "warning")
    cancel_entry(bucket=bucket, entry_id=entry_id, user=user)
    return _answer(user, booking, "Entry removed.", "info")


@bookings_bp.route("/<booking_id>/cancel", methods=["POST"])
@login_required
def cancel(booking_id):
    """Call off one appointment."""
    from application.services.booking import cancel as cancel_booking

    user = current_user._get_current_object()
    booking = _booking_or_404(user, booking_id)
    who = booking.name or booking.email or "the guest"
    warnings = cancel_booking(user, booking,
                              notify=bool(request.form.get("notify")))
    flash(f"Appointment with {who} cancelled." if not warnings
          else f"Appointment with {who} cancelled. " + " ".join(warnings),
          "info" if not warnings else "warning")
    return redirect(url_for("bookings.index"))
