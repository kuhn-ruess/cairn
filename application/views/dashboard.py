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
    from application.services.legal import operator
    return render_template("about.html", app_version=APP_VERSION,
                           operator=operator())


# Legal pages are intentionally public (no @login_required) — an Impressum
# must be reachable without an account. Who the operator IS never comes from
# the templates; ``services.legal.operator`` reads it from BKTODO_LEGAL_*, and
# an instance that hasn't configured it renders the "no Impressum" notice.
@dashboard_bp.route("/impressum")
def impressum():
    from application.services.legal import operator
    return render_template("legal/impressum.html", operator=operator())


@dashboard_bp.route("/datenschutz")
def datenschutz():
    from application.services.legal import operator
    return render_template("legal/datenschutz.html", operator=operator())

def _scope(user):
    from application.services import focus
    from application.services.permissions import visible_items_q
    return focus.restrict(visible_items_q(user), user)

def _perspective_scope(user):
    # Same narrow scope the Today count and the Next page use, so the home
    # page's Next preview lists exactly what those perspectives would.
    from application.services import focus
    from application.services.permissions import perspective_base_q
    return focus.restrict(perspective_base_q(user), user)

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
    # The review's own count, so the card agrees with the sidebar badge and
    # with the queue itself. Counting next_review_at here missed every
    # task-holding folder (a folder has no next_review_at until it has been
    # reviewed once) and every never-reviewed project.
    from application.views.review import review_due_count
    due_review = review_due_count(user, now)

    # Two separate previews so the home page matches the GTD flow: deal
    # with the inbox (untriaged) first, then look at what's actually next.
    next_actions = list(
        Item.objects(_perspective_scope(user), status="next")
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
