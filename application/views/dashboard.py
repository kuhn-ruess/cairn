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


# Legal pages are intentionally public (no @login_required) — an Impressum
# must be reachable without an account.
@dashboard_bp.route("/impressum")
def impressum():
    return render_template("legal/impressum.html")


@dashboard_bp.route("/datenschutz")
def datenschutz():
    return render_template("legal/datenschutz.html")

def _scope(user):
    from application.services import focus
    return focus.restrict(Q(owner=user) | Q(assigned_to=user), user)

@dashboard_bp.route("/")
@login_required
def home():
    user = current_user._get_current_object()
    now = datetime.now(timezone.utc)

    from application.services.permissions import inbox_q
    inbox_count = Item.objects(
        (_scope(user) & inbox_q(user)), kind="task",
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
    next_actions = list(
        Item.objects(_scope(user), status="next")
        .order_by("+due_date", "-flagged")
        .limit(10)
    )
    inbox_preview = list(
        Item.objects((_scope(user) & inbox_q(user)), kind="task",
                     status__nin=["done", "dropped"])
        .order_by("-created_at")
        .limit(10)
    )
    # Resolve parent/assignee refs + subtask counts in bulk so the row
    # template doesn't N+1.
    from application.services.prefetch import prefetch_refs, annotate_subtask_counts
    prefetch_refs(next_actions + inbox_preview)
    annotate_subtask_counts(next_actions + inbox_preview)

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
