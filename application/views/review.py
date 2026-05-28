"""GTD weekly review perspective."""
from datetime import datetime, timezone

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask_login import current_user, login_required
from application.models.item import Item

review_bp = Blueprint("review", __name__, url_prefix="/review")

@review_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()
    now = datetime.now(timezone.utc)
    due = list(
        Item.objects(
            owner=user,
            status="active",
            next_review_at__lte=now,
        ).order_by("+next_review_at")
    )
    upcoming = Item.objects(
        owner=user,
        status="active",
        next_review_at__gt=now,
    ).order_by("+next_review_at").limit(10)
    never_reviewed = list(
        Item.objects(owner=user, status="active", next_review_at=None)
    )
    # Pull each reviewed project's open tasks so the user can step
    # through project + its actions in one pass (OmniFocus-style).
    review_pks = [p.pk for p in due] + [p.pk for p in never_reviewed]
    tasks_by_project: dict = {}
    if review_pks:
        for t in Item.objects(
            owner=user, kind="task", parent__in=review_pks,
            status__nin=["done", "dropped"],
        ).order_by("+sort_order", "+created_at"):
            tasks_by_project.setdefault(t.parent.pk, []).append(t)
    return render_template(
        "review/index.html",
        due_projects=due,
        upcoming=upcoming,
        never_reviewed=never_reviewed,
        tasks_by_project=tasks_by_project,
    )

@review_bp.route("/<project_id>/done", methods=["POST"])
@login_required
def mark_reviewed(project_id):
    user = current_user._get_current_object()
    project = Item.objects(pk=project_id).first()
    if not project or project.owner != user:
        abort(404)
    project.mark_reviewed()
    flash(f"Reviewed: {project.name}", "success")
    return redirect(url_for("review.index"))
