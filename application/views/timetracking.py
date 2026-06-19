"""Team time-tracking — customer & billing-project management + GTD linking.

Team-only (``team_required``). Every customer/project is scoped to the acting
user's team; cross-team access 404s. See application.models.timetracking for the
data model and application.services.timetracking for the bucket mechanics.
"""
from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from application.models.item import Item
from application.models.timetracking import Customer, TimeTrackingProject
from application.services.permissions import team_required
from application.services.timetracking import primary_team

timetracking_bp = Blueprint("timetracking", __name__, url_prefix="/time")


def _team():
    """The acting user's team, or 403 (team_required already guarantees one)."""
    team = primary_team(current_user)
    if not team:
        abort(403)
    return team


def _customer_or_404(cid, team):
    c = Customer.objects(pk=cid, team=team).first()
    if not c:
        abort(404)
    return c


def _project_or_404(pid, team):
    p = TimeTrackingProject.objects(pk=pid, team=team).first()
    if not p:
        abort(404)
    return p


def _parse_rate_cents(raw: str):
    """Parse a euro amount ('85', '85.50', '85,50') into integer cents.
    Empty/invalid → None (no rate = hours only)."""
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    if val < 0:
        return None
    return round(val * 100)


@timetracking_bp.route("/")
@login_required
@team_required
def index():
    team = _team()
    customers = list(Customer.objects(team=team).order_by("+archived", "+name"))
    projects = list(TimeTrackingProject.objects(team=team).order_by("+archived", "+name"))

    # Group projects under their customer, and collect the linked GTD items per
    # project in one query (no per-row lookups in the template).
    by_customer: dict[str, list] = {}
    for p in projects:
        by_customer.setdefault(str(p.customer.id), []).append(p)

    linked = list(Item.objects(time_project__in=[p.id for p in projects])
                  .only("id", "title", "kind", "time_project"))
    links_by_project: dict[str, list] = {}
    for it in linked:
        links_by_project.setdefault(str(it.time_project.id), []).append(it)

    # Containers the user can attach (their own projects/folders).
    user = current_user._get_current_object()
    linkable = list(Item.objects(owner=user, kind__in=["project", "folder"])
                    .only("id", "title", "kind").order_by("+title"))

    return render_template(
        "timetracking/index.html",
        team=team,
        customers=customers,
        by_customer=by_customer,
        links_by_project=links_by_project,
        linkable=linkable,
    )


@timetracking_bp.route("/report")
@login_required
@team_required
def report():
    # Filtered report + export — built in phase 4.
    flash("The time report is coming in the next step.", "info")
    return redirect(url_for("timetracking.index"))


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

@timetracking_bp.route("/customers", methods=["POST"])
@login_required
@team_required
def customer_create():
    team = _team()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Customer name is required.", "warning")
        return redirect(url_for("timetracking.index"))
    Customer(team=team, name=name[:200],
             note=(request.form.get("note") or "").strip()).save()
    flash(f"Customer “{name}” created.", "success")
    return redirect(url_for("timetracking.index"))


@timetracking_bp.route("/customers/<cid>/edit", methods=["POST"])
@login_required
@team_required
def customer_edit(cid):
    team = _team()
    c = _customer_or_404(cid, team)
    name = (request.form.get("name") or "").strip()
    if name:
        c.name = name[:200]
    c.note = (request.form.get("note") or "").strip()
    c.archived = request.form.get("archived") == "1"
    c.save()
    flash("Customer updated.", "success")
    return redirect(url_for("timetracking.index"))


# ---------------------------------------------------------------------------
# Billing projects
# ---------------------------------------------------------------------------

@timetracking_bp.route("/projects", methods=["POST"])
@login_required
@team_required
def project_create():
    team = _team()
    name = (request.form.get("name") or "").strip()
    customer = _customer_or_404(request.form.get("customer_id"), team)
    if not name:
        flash("Project name is required.", "warning")
        return redirect(url_for("timetracking.index"))
    TimeTrackingProject(
        team=team, customer=customer, name=name[:200],
        note=(request.form.get("note") or "").strip(),
        hourly_rate_cents=_parse_rate_cents(request.form.get("hourly_rate")),
    ).save()
    flash(f"Project “{name}” created.", "success")
    return redirect(url_for("timetracking.index"))


@timetracking_bp.route("/projects/<pid>/edit", methods=["POST"])
@login_required
@team_required
def project_edit(pid):
    team = _team()
    p = _project_or_404(pid, team)
    name = (request.form.get("name") or "").strip()
    if name:
        p.name = name[:200]
    p.note = (request.form.get("note") or "").strip()
    p.hourly_rate_cents = _parse_rate_cents(request.form.get("hourly_rate"))
    p.archived = request.form.get("archived") == "1"
    p.save()
    flash("Project updated.", "success")
    return redirect(url_for("timetracking.index"))


# ---------------------------------------------------------------------------
# Linking a Cairn folder/project to a billing project
# ---------------------------------------------------------------------------

@timetracking_bp.route("/link", methods=["POST"])
@login_required
@team_required
def link_item():
    """Bind a Cairn folder/project to a billing project (or clear the link).

    Tasks below the linked container inherit the billing project automatically
    (see services.timetracking.resolve_time_project)."""
    team = _team()
    user = current_user._get_current_object()
    item = Item.objects(pk=request.form.get("item_id"), owner=user,
                        kind__in=["project", "folder"]).first()
    if not item:
        abort(404)
    pid = (request.form.get("project_id") or "").strip()
    if pid:
        item.time_project = _project_or_404(pid, team)
        flash(f"“{item.title}” linked to {item.time_project.name}.", "success")
    else:
        item.time_project = None
        flash(f"“{item.title}” unlinked.", "success")
    item.save()
    return redirect(url_for("timetracking.index"))
