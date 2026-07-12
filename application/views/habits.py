"""Habit goals — rolling-cycle personal goals (premium-only).

Each habit is owned by one user and runs in auto-resetting cycles of
``period_days``. Progress is computed on read (see
``application/services/habits.py``); check-ins are logged via AJAX so the +1 /
undo taps swap just the card partial and never pollute browser history.
"""
from datetime import datetime, timezone

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from application.models.habit import Habit, HabitTarget
from application.services.habits import add_checkin, cycle_progress, undo_checkin
from application.services.permissions import premium_required

habits_bp = Blueprint("habits", __name__, url_prefix="/habits")


def _owned(habit_id: str) -> Habit:
    """Fetch a habit owned by the current user, or 404."""
    habit = Habit.objects(
        id=habit_id, owner=current_user._get_current_object()
    ).first()
    if habit is None:
        abort(404)
    return habit


def _parse_targets(existing: Habit | None = None) -> list[HabitTarget]:
    """Build the target list from parallel form arrays. Blank rows are dropped;
    on edit, a row's hidden ``target_tid`` preserves that target's id (so its
    check-ins keep counting)."""
    names = request.form.getlist("target_name")
    counts = request.form.getlist("target_count")
    tids = request.form.getlist("target_tid")
    targets: list[HabitTarget] = []
    for i, name in enumerate(names):
        name = (name or "").strip()
        if not name:
            continue
        try:
            count = max(1, int(counts[i]))
        except (ValueError, IndexError):
            count = 1
        tid = (tids[i] if i < len(tids) else "").strip()
        t = HabitTarget(name=name[:120], count=count)
        if tid:
            t.tid = tid
        targets.append(t)
    return targets


@habits_bp.route("/")
@login_required
@premium_required
def index():
    habits = list(Habit.objects(
        owner=current_user._get_current_object(), archived=None
    ))
    now = datetime.now(timezone.utc)
    cards = [(h, cycle_progress(h, now)) for h in habits]
    return render_template("habits/index.html", cards=cards)


@habits_bp.route("/new", methods=["GET", "POST"])
@login_required
@premium_required
def create():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        try:
            period = min(365, max(1, int(request.form.get("period_days", 14))))
        except ValueError:
            period = 14
        targets = _parse_targets()
        if not name:
            flash("Give the goal a name.", "warning")
        elif not targets:
            flash("Add at least one target.", "warning")
        else:
            Habit(
                owner=current_user._get_current_object(),
                name=name[:200],
                period_days=period,
                targets=targets,
            ).save()
            flash("Goal created.", "success")
            return redirect(url_for("habits.index"))
    return render_template("habits/form.html", habit=None)


@habits_bp.route("/<habit_id>/edit", methods=["GET", "POST"])
@login_required
@premium_required
def edit(habit_id):
    habit = _owned(habit_id)
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        try:
            period = min(365, max(1, int(request.form.get("period_days", 14))))
        except ValueError:
            period = habit.period_days
        targets = _parse_targets(habit)
        if not name:
            flash("Give the goal a name.", "warning")
        elif not targets:
            flash("Add at least one target.", "warning")
        else:
            habit.name = name[:200]
            habit.period_days = period
            habit.targets = targets
            habit.save()
            flash("Goal updated.", "success")
            return redirect(url_for("habits.index"))
    return render_template("habits/form.html", habit=habit)


@habits_bp.route("/<habit_id>/delete", methods=["POST"])
@login_required
@premium_required
def delete(habit_id):
    habit = _owned(habit_id)
    habit.delete()
    flash("Goal deleted.", "success")
    return redirect(url_for("habits.index"))


def _card_response(habit: Habit):
    """Render just the card partial for an AJAX check-in/undo."""
    progress = cycle_progress(habit, datetime.now(timezone.utc))
    return render_template("habits/_habit_card.html", h=habit, p=progress)


@habits_bp.route("/<habit_id>/checkin", methods=["POST"])
@login_required
@premium_required
def checkin(habit_id):
    habit = _owned(habit_id)
    tid = request.form.get("tid", "")
    if not add_checkin(habit, tid):
        abort(400)
    return _card_response(habit)


@habits_bp.route("/<habit_id>/undo", methods=["POST"])
@login_required
@premium_required
def undo(habit_id):
    habit = _owned(habit_id)
    tid = request.form.get("tid", "")
    undo_checkin(habit, tid)
    return _card_response(habit)
