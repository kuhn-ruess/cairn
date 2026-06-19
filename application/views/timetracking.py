"""Team time-tracking — customer & billing-project management + GTD linking.

Team-only (``team_required``). Every customer/project is scoped to the acting
user's team; cross-team access 404s. See application.models.timetracking for the
data model and application.services.timetracking for the bucket mechanics.
"""
from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from datetime import datetime, timezone

from application.models.item import Item
from application.models.timetracking import Customer, TimeBucket, TimeTrackingProject
from application.services.permissions import team_required
from application.services.timetracking import (
    add_entry,
    cancel_entry,
    parse_duration,
    primary_team,
    resolve_time_project,
    task_time_context,
)

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


def _parse_budget(form):
    """Form (budget_unit ∈ ''/hours/money, budget_value, budget_recurring) →
    ``(budget_value, budget_type)``. Hours are stored as minutes, money as
    cents. Blank/zero/invalid → ``(None, None)`` (no budget)."""
    unit = (form.get("budget_unit") or "").strip()
    raw = (form.get("budget_value") or "").strip().replace(",", ".")
    if not unit or not raw:
        return None, None
    try:
        val = float(raw)
    except ValueError:
        return None, None
    if val <= 0:
        return None, None
    recurring = form.get("budget_recurring") == "1"
    if unit == "money":
        value, base = round(val * 100), "cents"
    else:  # hours
        value, base = round(val * 60), "minutes"
    return value, base + ("_per_month" if recurring else "")


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

    # Budget vs. actual — one bucket pass for the whole team, then a status per
    # project (None when no budget set).
    from application.services.tz import user_zone
    from application.services.timetracking import budget_status, team_consumption
    today = datetime.now(user_zone(user)).date()
    consumption = team_consumption(team, f"{today.year:04d}-{today.month:02d}")
    budgets = {str(p.id): budget_status(p, consumption.get(str(p.id)))
               for p in projects}

    return render_template(
        "timetracking/index.html",
        team=team,
        customers=customers,
        by_customer=by_customer,
        links_by_project=links_by_project,
        linkable=linkable,
        budgets=budgets,
    )


# ---------------------------------------------------------------------------
# Logging time on a task (inspector section) → monthly bucket
# ---------------------------------------------------------------------------

def _task_time_response(task):
    """Re-render the inspector time partial (AJAX swap) or redirect to detail."""
    tt = task_time_context(current_user._get_current_object(), task) if task else None
    if request.headers.get("X-Partial") == "1":
        return render_template("timetracking/_task_time.html", task=task, tt=tt)
    if task:
        return redirect(url_for("tasks.detail", task_id=str(task.pk)) + "#time")
    return redirect(url_for("timetracking.index"))


@timetracking_bp.route("/log/<task_id>", methods=["POST"])
@login_required
@team_required
def log(task_id):
    """Log a chunk of time on a task into its billing project's monthly bucket.

    The billing project is inherited from the task's subtree; if nothing is
    linked the section isn't shown, so this just no-ops back."""
    from application.services.tz import attach_user_tz, user_zone
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    project = resolve_time_project(task)
    if project and project.team == primary_team(user):
        minutes = parse_duration(request.form.get("duration") or "")
        if minutes:
            raw_date = (request.form.get("spent_on") or "").strip()
            try:
                when = attach_user_tz(datetime.strptime(raw_date, "%Y-%m-%d"), user)
            except (ValueError, TypeError):
                when = datetime.now(user_zone(user))
            add_entry(
                project=project, user=user, minutes=minutes, spent_on=when,
                note=request.form.get("note") or "", source_item=task,
                billable=request.form.get("billable") != "0",
            )
    return _task_time_response(task)


@timetracking_bp.route("/entry/<bucket_id>/<entry_id>/cancel", methods=["POST"])
@login_required
@team_required
def entry_cancel(bucket_id, entry_id):
    """Soft-cancel a time entry. Allowed for the person who logged it or a team
    manager/biller. Refused on locked (billed) entries (handled in the service)."""
    team = _team()
    bucket = TimeBucket.objects(pk=bucket_id, team=team).first()
    if not bucket:
        abort(404)
    user = current_user._get_current_object()
    entry = next((e for e in bucket.entries if e.entry_id == entry_id), None)
    if entry is None:
        abort(404)
    is_owner = entry.user_id == str(user.id)
    is_manager = team.is_managed_by(user) or user in (team.billers or [])
    if not (is_owner or is_manager):
        abort(403)
    cancel_entry(bucket=bucket, entry_id=entry_id, user=user)
    task = None
    if entry.source_item_id:
        task = Item.objects(pk=entry.source_item_id).first()
    return _task_time_response(task)


def _report_filters():
    """Parse the report query string into normalised filter values + the
    resolved date range, shared by the HTML report and the CSV export."""
    from application.services.tz import user_zone
    from application.services.timetracking import resolve_period

    user = current_user._get_current_object()
    today = datetime.now(user_zone(user)).date()
    preset = request.args.get("period") or "this_month"
    date_from, date_to = resolve_period(
        preset, request.args.get("from"), request.args.get("to"), today)

    def _tri(name):
        v = request.args.get(name)
        return True if v == "1" else False if v == "0" else None

    return {
        "preset": preset,
        "date_from": date_from,
        "date_to": date_to,
        "customer_id": request.args.get("customer") or None,
        "project_id": request.args.get("project") or None,
        "user_id": request.args.get("user") or None,
        "billable": _tri("billable"),
        "locked": _tri("locked"),
    }


def _grouped_report(team, f):
    """Build the customer → project → user grouping + totals for the filters."""
    from application.services.timetracking import (
        build_report_rows, reachable_project_ids)
    from application.services.permissions import team_members

    user = current_user._get_current_object()
    scope = reachable_project_ids(user, team)
    rows = build_report_rows(
        team=team, scope_ids=scope,
        date_from=f["date_from"], date_to=f["date_to"],
        customer_id=f["customer_id"], project_id=f["project_id"],
        user_id=f["user_id"], billable=f["billable"], locked=f["locked"],
    )

    customers = {}
    grand_min = grand_amt = 0
    for r in rows:
        grand_min += r["minutes"]
        grand_amt += r["amount_cents"] or 0
        c = customers.setdefault(r["customer"], {
            "name": r["customer"], "minutes": 0, "amount": 0, "projects": {}})
        c["minutes"] += r["minutes"]; c["amount"] += r["amount_cents"] or 0
        p = c["projects"].setdefault(r["project_id"], {
            "name": r["project"], "minutes": 0, "amount": 0, "users": {}})
        p["minutes"] += r["minutes"]; p["amount"] += r["amount_cents"] or 0
        u = p["users"].setdefault(r["user_id"] or "", {
            "name": r["user_name"] or "—", "minutes": 0, "amount": 0, "entries": []})
        u["minutes"] += r["minutes"]; u["amount"] += r["amount_cents"] or 0
        u["entries"].append(r)

    # Stable ordering: customers/projects by name; entries newest first.
    out = []
    for c in sorted(customers.values(), key=lambda x: x["name"].lower()):
        projects = []
        for p in sorted(c["projects"].values(), key=lambda x: x["name"].lower()):
            users = sorted(p["users"].values(), key=lambda x: x["name"].lower())
            for u in users:
                u["entries"].sort(key=lambda r: r["spent_on"], reverse=True)
            projects.append({**p, "users": users})
        out.append({**c, "projects": projects})

    # Filter dropdown data.
    customers_list = list(Customer.objects(team=team).order_by("+name"))
    projects_list = list(TimeTrackingProject.objects(team=team).order_by("+name"))
    members = team_members(team)
    return {
        "groups": out, "rows": rows,
        "grand_minutes": grand_min, "grand_amount": grand_amt,
        "customers": customers_list, "projects": projects_list, "members": members,
    }


@timetracking_bp.route("/report")
@login_required
@team_required
def report():
    from application.services.timetracking import PERIOD_PRESETS, can_bill
    team = _team()
    user = current_user._get_current_object()
    f = _report_filters()
    data = _grouped_report(team, f)
    return render_template(
        "timetracking/report.html", team=team, f=f, presets=PERIOD_PRESETS,
        can_bill=can_bill(user, team), can_unlock=team.is_managed_by(user),
        **data)


@timetracking_bp.route("/report/bill", methods=["POST"])
@login_required
@team_required
def report_bill():
    """Bulk lock (bill) or unlock the selected entries, then back to the report
    with the same filters preserved."""
    from application.services.timetracking import set_locked
    team = _team()
    user = current_user._get_current_object()
    pairs = []
    for raw in request.form.getlist("entry"):
        bid, _, eid = raw.partition(":")
        if bid and eid:
            pairs.append((bid, eid))
    lock = request.form.get("action") != "unlock"
    n = set_locked(team=team, user=user, items=pairs, locked=lock)
    if n:
        flash(f"{n} entr{'y' if n == 1 else 'ies'} "
              f"marked {'billed' if lock else 'open'}.", "success")
    else:
        flash("Nothing changed.", "info")
    # Preserve the report filters from the hidden query string.
    args = {k: v for k, v in request.form.items()
            if k in ("period", "from", "to", "customer", "project", "user",
                     "billable", "locked")}
    return redirect(url_for("timetracking.report", **args))


@timetracking_bp.route("/report.csv")
@login_required
@team_required
def report_csv():
    import csv
    import io
    from flask import Response
    team = _team()
    f = _report_filters()
    data = _grouped_report(team, f)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Customer", "Project", "User", "Minutes", "Hours",
                "Billable", "Billed", "Amount (EUR)", "Note"])
    for r in sorted(data["rows"], key=lambda r: r["spent_on"]):
        w.writerow([
            r["spent_on"].isoformat(), r["customer"], r["project"],
            r["user_name"], r["minutes"], f"{r['minutes'] / 60:.2f}",
            "yes" if r["billable"] else "no", "yes" if r["locked"] else "no",
            f"{(r['amount_cents'] or 0) / 100:.2f}", r["note"],
        ])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition":
            f"attachment; filename=cairn-time-"
            f"{f['date_from']:%Y%m%d}-{f['date_to']:%Y%m%d}.csv",
    })


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
    budget_value, budget_type = _parse_budget(request.form)
    TimeTrackingProject(
        team=team, customer=customer, name=name[:200],
        note=(request.form.get("note") or "").strip(),
        hourly_rate_cents=_parse_rate_cents(request.form.get("hourly_rate")),
        budget_value=budget_value, budget_type=budget_type,
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
    p.budget_value, p.budget_type = _parse_budget(request.form)
    p.archived = request.form.get("archived") == "1"
    p.save()
    flash("Project updated.", "success")
    return redirect(url_for("timetracking.index"))


@timetracking_bp.route("/projects/<pid>/share", methods=["POST"])
@login_required
@team_required
def project_share(pid):
    """Toggle the customer's read-only time-sheet link for a billing project.

    ``action`` ∈ enable / disable / regenerate. The link shows hours only."""
    team = _team()
    p = _project_or_404(pid, team)
    action = request.form.get("action")
    if action == "disable":
        p.share_enabled = False
        flash("Customer link disabled.", "info")
    else:  # enable / regenerate
        if action == "regenerate" or not p.share_token:
            p.regenerate_share_token()
        p.share_enabled = True
        flash("Customer link is live.", "success")
    p.save()
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
