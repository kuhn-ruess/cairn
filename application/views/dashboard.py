"""Dashboard / home perspective."""
from datetime import datetime, timezone

from flask import Blueprint, render_template
from flask_login import current_user, login_required
from mongoengine import Q
from application.models.item import Item

dashboard_bp = Blueprint("dashboard", __name__)

@dashboard_bp.route("/help")
@login_required
def help_page():
    return render_template("help.html")


@dashboard_bp.route("/about")
@login_required
def about():
    from application import APP_VERSION
    return render_template("about.html", app_version=APP_VERSION)

def _scope(user):
    return Q(owner=user) | Q(assigned_to=user)

@dashboard_bp.route("/")
@login_required
def home():
    user = current_user._get_current_object()
    now = datetime.now(timezone.utc)

    inbox_count = Item.objects(
        _scope(user), kind="task", parent=None,
        status__nin=["done", "dropped"]
    ).count()
    # Use the shared Today selection so this count matches the Today page
    # exactly (was a divergent "due within 2 days" formula before).
    from application.services.today import today_count as _today_count
    today_count = _today_count(user)
    flagged_count = Item.objects(
        _scope(user), status__nin=["done", "dropped"], flagged=True
    ).count()
    waiting_count = Item.objects(_scope(user), status="waiting").count()
    due_review = Item.objects(
        owner=user, status="active", next_review_at__lte=now
    ).count()

    # Two separate previews so the home page matches the GTD flow: deal
    # with the inbox (untriaged) first, then look at what's actually next.
    next_actions = (
        Item.objects(_scope(user), status="next")
        .order_by("+due_date", "-flagged")
        .limit(10)
    )
    inbox_preview = (
        Item.objects(_scope(user), kind="task", parent=None,
                     status__nin=["done", "dropped"])
        .order_by("-created_at")
        .limit(10)
    )

    return render_template(
        "dashboard.html",
        inbox_count=inbox_count,
        today_count=today_count,
        flagged_count=flagged_count,
        waiting_count=waiting_count,
        due_review=due_review,
        next_actions=next_actions,
        inbox_preview=inbox_preview,
    )
