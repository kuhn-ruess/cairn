"""The old Habits area, now folded into the 12 Week Year.

The rolling habits moved into the 12 Week Year as habit tactics
(``services/habit_migration.py``). Every old address still answers, with a
redirect to the one place they live now; nothing here writes to the old
``habits`` collection any more, which stays as it is (read only) until it is
dropped by hand.
"""
from flask import Blueprint, redirect, url_for
from flask_login import login_required

habits_bp = Blueprint("habits", __name__, url_prefix="/habits")


@habits_bp.route("/", methods=["GET", "POST"])
@habits_bp.route("/<path:rest>", methods=["GET", "POST"])
@login_required
def index(rest=None):
    return redirect(url_for("twelve_week.index"))
