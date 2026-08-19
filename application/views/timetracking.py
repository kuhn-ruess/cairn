"""Team time-tracking — customer & billing-project management + GTD linking.

Team-only (``team_required``). Every customer/project is scoped to the acting
user's team; cross-team access 404s. See application.models.timetracking for the
data model and application.services.timetracking for the bucket mechanics.
"""
from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for,
)
from bson import ObjectId
from flask_login import current_user, login_required

from datetime import datetime

from application.models.item import Item
from application.models.timetracking import Customer, TimeBucket, TimeTrackingProject
from application.services.permissions import team_required
from application.services.timetracking import (
    add_entry,
    to_oid,
    cancel_entry,
    custom_values,
    day_sheet,
    default_project_id,
    discard_timer,
    edit_entry,
    may_edit_entry,
    parse_clock,
    parse_duration,
    parse_project_fields,
    pickable_projects,
    project_for_user,
    start_timer,
    stop_timer,
    task_time_context,
)

timetracking_bp = Blueprint("timetracking", __name__, url_prefix="/time")


def _user_teams(user):
    """Every team the user participates in (member, admin, or biller), by name.
    The module is scoped to one of these at a time."""
    from application.services.timetracking import user_teams
    return sorted(user_teams(user), key=lambda t: (t.name or "").lower())


def _team_ids(user) -> set:
    from application.services.timetracking import user_team_ids
    return user_team_ids(user)


def _team():
    """The team the module currently operates on.

    Honours ``team=<id>`` (query string OR form body, so it survives POSTs)
    when the user belongs to it; otherwise the team they pinned as their
    default. 404s if they ask for a team they're not in, 403 if they have none."""
    from application.services.timetracking import default_team
    user = current_user._get_current_object()
    teams = _user_teams(user)
    if not teams:
        abort(403)
    raw = request.values.get("team")
    if raw:
        for t in teams:
            if str(t.pk) == raw:
                return t
        abort(404)
    return default_team(user)


@timetracking_bp.route("/default-team", methods=["POST"])
@login_required
@team_required
def set_default_team():
    """Pin the team the module opens in. Sending the current default again
    clears it (back to "first by name")."""
    user = current_user._get_current_object()
    team = _team()
    if user.tt_default_team == str(team.pk):
        user.tt_default_team = ""
        flash(f"“{team.name}” is no longer your default.", "info")
    else:
        user.tt_default_team = str(team.pk)
        flash(f"Time tracking now opens in “{team.name}”.", "success")
    user.save()
    return redirect(request.referrer or url_for("timetracking.index", team=team.pk))


def _customer_or_404(cid, team):
    c = Customer.objects(pk=to_oid(cid) or ObjectId(), team=team).first()
    if not c:
        abort(404)
    return c


def _project_or_404(pid, team):
    p = TimeTrackingProject.objects(pk=to_oid(pid) or ObjectId(), team=team).first()
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


def _field_rows(form):
    """The custom-field editor's parallel lists → rows for parse_project_fields.

    Every row posts all five inputs, so the lists line up. A row with a blank
    label is how a field gets removed."""
    keys = form.getlist("cf_key")
    labels = form.getlist("cf_label")
    types = form.getlist("cf_type")
    defaults = form.getlist("cf_default")
    options = form.getlist("cf_options")

    def at(seq, i):
        return seq[i] if i < len(seq) else ""

    return [(at(keys, i), labels[i], at(types, i), at(defaults, i), at(options, i))
            for i in range(len(labels))]


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
        all_teams=_user_teams(user),
        can_manage=team.is_managed_by(user),
        customers=customers,
        by_customer=by_customer,
        links_by_project=links_by_project,
        linkable=linkable,
        budgets=budgets,
    )


# ---------------------------------------------------------------------------
# Capturing time: the stopwatch, and logging on a task or on the day sheet
# ---------------------------------------------------------------------------

def _parse_day(raw, user):
    """``YYYY-MM-DD`` → date, falling back to *today in the user's timezone*."""
    from application.services.tz import user_zone
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return datetime.now(user_zone(user)).date()


def _spent_on(raw, user):
    """The form's date → an aware datetime in the user's tz (noon, so the day
    survives any later timezone conversion), or now when blank/invalid."""
    from application.services.tz import user_zone
    zone = user_zone(user)
    try:
        d = datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return datetime.now(zone)
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=zone)


def _booking(form, project):
    """The parts of a booking that every capture path shares.

    Returns ``(minutes, start_time, end_time, custom)``. Clock times win when
    both are given — that is the user explicitly choosing to book "09:00–10:30"
    instead of "1h30m", and the duration is then derived from them."""
    clock = parse_clock(form.get("start_time"), form.get("end_time"))
    if clock:
        minutes, start, end = clock
    else:
        minutes, start, end = parse_duration(form.get("duration") or ""), "", ""
    return minutes, start, end, custom_values(project, form.get)


def _ajax_payload(user, task=None):
    """The JSON the capture endpoints answer with: always the navbar chip, plus
    whichever block the caller is showing (``X-Partial: task`` / ``day``)."""
    want = request.headers.get("X-Partial") or ""
    timer = user.running_timer
    out = {"chip": render_template("timetracking/_timer_chip.html", timer=timer),
           # Which task the stopwatch runs on (if any) — lets the play/stop
           # buttons in a task list flip without re-rendering the list.
           "timer_item": (timer.source_item_id or "") if timer else ""}
    if want == "task" and task is not None:
        out["block"] = render_template(
            "timetracking/_task_time.html", task=task,
            tt=task_time_context(user, task))
    elif want == "day":
        day = _parse_day(request.values.get("d"), user)
        out["block"] = render_template("timetracking/_day_body.html",
                                       d=day_sheet(user, day))
    return out


def _capture_response(user, task=None, fallback=None):
    """AJAX → JSON payload; plain POST → back where the user came from."""
    if request.headers.get("X-Partial"):
        return jsonify(_ajax_payload(user, task))
    if task is not None:
        return redirect(url_for("tasks.detail", task_id=str(task.pk)) + "#time")
    return redirect(fallback or url_for("timetracking.day"))


def _viewable_task(user, task_id):
    tid = to_oid(task_id)
    if tid is None:
        return None
    task = Item.objects(pk=tid).first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    return task


def _resolve_project(user, task=None, explicit=None):
    """The billing project a capture action targets: an explicitly picked one,
    else the task's inherited/last-used default. ``None`` when nothing fits."""
    if explicit:
        return project_for_user(user, explicit)
    pid = default_project_id(user, task)
    return project_for_user(user, pid) if pid else None


@timetracking_bp.route("/log/<task_id>", methods=["POST"])
@login_required
@team_required
def log(task_id):
    """Log a chunk of time on a task.

    The billing project comes from the picker if the user chose one, otherwise
    from the task's subtree (inherited) or the project they last logged to — so
    logging from a task stays a one-field action in the common case."""
    user = current_user._get_current_object()
    task = _viewable_task(user, task_id)
    project = _resolve_project(user, task, request.form.get("project_id"))
    if project is not None:
        minutes, start, end, custom = _booking(request.form, project)
        if minutes:
            add_entry(
                project=project, user=user, minutes=minutes,
                spent_on=_spent_on(request.form.get("spent_on"), user),
                start_time=start, end_time=end, custom=custom,
                note=request.form.get("note") or "", source_item=task,
                billable=request.form.get("billable") != "0",
            )
            user.tt_last_project = str(project.id)
            user.save()
    return _capture_response(user, task)


@timetracking_bp.route("/day/log", methods=["POST"])
@login_required
@team_required
def day_log():
    """Quick-add a manual entry from the day sheet (no task involved)."""
    user = current_user._get_current_object()
    project = _resolve_project(user, None, request.form.get("project_id"))
    minutes = None
    if project is not None:
        minutes, start, end, custom = _booking(request.form, project)
        if minutes:
            add_entry(
                project=project, user=user, minutes=minutes,
                spent_on=_spent_on(request.form.get("spent_on"), user),
                start_time=start, end_time=end, custom=custom,
                note=request.form.get("note") or "",
                billable=request.form.get("billable") != "0",
            )
            user.tt_last_project = str(project.id)
            user.save()
    if not minutes:
        flash("Enter a duration (1h30m, 1:30, 90) or a start and end time.",
              "warning")
    return _capture_response(user)


@timetracking_bp.route("/timer/start", methods=["POST"])
@login_required
@team_required
def timer_start():
    """Start the stopwatch — the one-click path from a task row or the day sheet.

    When no billing project can be resolved the answer is a project list rather
    than an error, so the caller can pop a picker and start without a detour."""
    user = current_user._get_current_object()
    task = _viewable_task(user, request.form.get("task_id"))
    project = _resolve_project(user, task, request.form.get("project_id"))
    if project is None:
        projects = pickable_projects(user)
        if request.headers.get("X-Partial"):
            return jsonify({"needs_project": True, "projects": projects,
                            "task_id": request.form.get("task_id") or ""})
        flash("Pick a billing project to track time against." if projects
              else "Create a customer and a billing project first.", "warning")
        return _capture_response(user, task)
    start_timer(user, project=project, source_item=task,
                note=request.form.get("note") or "",
                # Kept as raw {key: value}; resolved against the project's
                # definitions when the clock stops.
                custom={c["key"]: c["value"]
                        for c in custom_values(project, request.form.get)},
                billable=request.form.get("billable") != "0")
    return _capture_response(user, task)


@timetracking_bp.route("/timer/stop", methods=["POST"])
@login_required
@team_required
def timer_stop():
    """Stop the stopwatch and log the elapsed time as a normal entry."""
    user = current_user._get_current_object()
    timer = user.running_timer
    task = None
    if timer and timer.source_item_id:
        task = _viewable_task(user, timer.source_item_id)
        if task is not None and not task.can_be_viewed_by(user):
            task = None
    entry = stop_timer(user)
    if entry and not request.headers.get("X-Partial"):
        flash(f"Logged {entry.minutes // 60}h {entry.minutes % 60}m "
              f"on {entry.project_name}.", "success")
    return _capture_response(user, task)


@timetracking_bp.route("/timer/discard", methods=["POST"])
@login_required
@team_required
def timer_discard():
    """Throw the running stopwatch away without logging it."""
    user = current_user._get_current_object()
    timer = user.running_timer
    task = None
    if timer and timer.source_item_id:
        task = _viewable_task(user, timer.source_item_id)
        if task is not None and not task.can_be_viewed_by(user):
            task = None
    discard_timer(user)
    return _capture_response(user, task)


@timetracking_bp.route("/entry/<bucket_id>/<entry_id>/cancel", methods=["POST"])
@login_required
@team_required
def entry_cancel(bucket_id, entry_id):
    """Soft-cancel a time entry. Allowed for the person who logged it or a team
    manager/biller. Refused on locked (billed) entries (handled in the service)."""
    user = current_user._get_current_object()
    # Derive the team from the bucket (the cancel button carries no team) and
    # verify the user belongs to it — works regardless of which team is "current".
    bucket = TimeBucket.objects(pk=to_oid(bucket_id) or ObjectId()).first()
    if not bucket:
        abort(404)
    team = bucket.team
    if str(team.id) not in _team_ids(user):
        abort(403)
    entry = next((e for e in bucket.entries if e.entry_id == entry_id), None)
    if entry is None:
        abort(404)
    if not may_edit_entry(user, team, entry):
        abort(403)
    cancel_entry(bucket=bucket, entry_id=entry_id, user=user)
    task = None
    if entry.source_item_id:
        task = Item.objects(pk=to_oid(entry.source_item_id) or ObjectId()).first()
    return _capture_response(user, task)


@timetracking_bp.route("/entry/<bucket_id>/<entry_id>/edit", methods=["POST"])
@login_required
@team_required
def entry_edit(bucket_id, entry_id):
    """Correct a logged entry — duration, date, note, billable — until it is
    billed. mite allows the same; the money is recomputed from the frozen rate."""
    user = current_user._get_current_object()
    bucket = TimeBucket.objects(pk=to_oid(bucket_id) or ObjectId()).first()
    if not bucket:
        abort(404)
    team = bucket.team
    if str(team.id) not in _team_ids(user):
        abort(403)
    entry = next((e for e in bucket.entries if e.entry_id == entry_id), None)
    if not may_edit_entry(user, team, entry):
        abort(403)
    project = project_for_user(user, entry.project_id)
    if project is not None:
        minutes, start, end, custom = _booking(request.form, project)
    else:  # project since deleted — the entry is still correctable
        minutes = parse_duration(request.form.get("duration") or "")
        start = end = ""
        custom = None
    spent_on = (_spent_on(request.form.get("spent_on"), user)
                if request.form.get("spent_on") else None)
    # A cleared note arrives as whitespace (an empty field would be dropped in
    # transit) — normalise it back to "no note".
    note = request.form.get("note")
    if note is not None and not note.strip():
        note = ""
    edit_entry(bucket=bucket, entry_id=entry_id, user=user, minutes=minutes,
               spent_on=spent_on, note=note, custom=custom,
               start_time=start, end_time=end,
               billable=request.form.get("billable") != "0")
    task = None
    if entry.source_item_id:
        task = Item.objects(pk=to_oid(entry.source_item_id) or ObjectId()).first()
    return _capture_response(user, task)


# ---------------------------------------------------------------------------
# The day sheet — mite's "Zeiten erfassen": today's time, week at a glance
# ---------------------------------------------------------------------------

@timetracking_bp.route("/day")
@login_required
@team_required
def day():
    user = current_user._get_current_object()
    d = _parse_day(request.args.get("d"), user)
    from application.services.tz import user_zone
    return render_template(
        "timetracking/day.html", d=day_sheet(user, d),
        today=datetime.now(user_zone(user)).date(),
        all_teams=_user_teams(user))


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
        viewer=user,
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
        "timetracking/report.html", team=team, all_teams=_user_teams(user),
        f=f, presets=PERIOD_PRESETS,
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
    # Preserve the report filters (incl. the selected team) from the hidden form.
    args = {k: v for k, v in request.form.items()
            if k in ("team", "period", "from", "to", "customer", "project",
                     "user", "billable", "locked")}
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
    # Custom fields differ per project, so they share one column rather than
    # sprouting a sparse column per field across the whole team's export.
    w.writerow(["Date", "Start", "End", "Customer", "Project", "Service", "User",
                "Minutes", "Hours", "Billable", "Billed", "Amount (EUR)",
                "Note", "Fields"])
    for r in sorted(data["rows"], key=lambda r: r["spent_on"]):
        w.writerow([
            r["spent_on"].isoformat(), r.get("start_time", ""),
            r.get("end_time", ""), r["customer"], r["project"],
            r.get("service", ""), r["user_name"], r["minutes"],
            f"{r['minutes'] / 60:.2f}",
            "yes" if r["billable"] else "no", "yes" if r["locked"] else "no",
            f"{(r['amount_cents'] or 0) / 100:.2f}",
            (f"#{r['external_ref']} " if r.get("external_ref") else "") + r["note"],
            "; ".join(f"{c['label']}: {c['value']}" for c in r.get("custom", [])),
        ])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition":
            f"attachment; filename=cairn-time-"
            f"{f['date_from']:%Y%m%d}-{f['date_to']:%Y%m%d}.csv",
    })


# ---------------------------------------------------------------------------
# Migrating off mite
# ---------------------------------------------------------------------------

@timetracking_bp.route("/import/mite", methods=["POST"])
@login_required
@team_required
def import_mite():
    """Pull a mite account's customers, projects and time history into this team.

    Manager-only: it creates billing data for the whole team. The API key is
    used for this request and never stored. Runs inline — a big account is
    better served by ``cairn import-mite``, which reports as it goes."""
    from datetime import date as _date
    from application.services.mite_import import MiteError, import_for_user

    team = _team()
    user = current_user._get_current_object()
    if not team.is_managed_by(user):
        abort(403)

    def _day(raw):
        try:
            return _date.fromisoformat((raw or "").strip())
        except ValueError:
            return None

    # "projects" = just the current structure, no time history — the normal
    # way to start on Cairn while the old hours stay in mite.
    projects_only = request.form.get("scope") == "projects"
    try:
        stats = import_for_user(
            user,
            account=request.form.get("account") or "",
            api_key=request.form.get("api_key") or "",
            team=team,
            date_from=_day(request.form.get("from")),
            date_to=_day(request.form.get("to")),
            with_entries=not projects_only,
            include_archived=not projects_only,
            dry_run=request.form.get("dry_run") == "1",
        )
    except MiteError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("timetracking.index", team=team.pk))

    summary = (f"{stats.customers_created} new customer(s), "
               f"{stats.projects_created} new project(s)")
    if stats.with_entries:
        summary += (f", {stats.entries_imported} time "
                    f"entr{'y' if stats.entries_imported == 1 else 'ies'}")
        if stats.entries_skipped:
            summary += f" ({stats.entries_skipped} already there)"
    else:
        summary += " — current projects only, no time imported"
    flash(f"{'Dry run — nothing written. Would import' if stats.dry_run else 'Imported'}: "
          f"{summary}.", "success")
    if stats.unmatched_users:
        flash("No Cairn account for these mite users — their entries keep the "
              "mite name: " + ", ".join(sorted(set(stats.unmatched_users))), "info")
    for err in stats.errors[:5]:
        flash(err, "warning")
    if len(stats.errors) > 5:
        flash(f"…and {len(stats.errors) - 5} more issues — run "
              f"“cairn import-mite” for the full list.", "warning")
    return redirect(url_for("timetracking.index", team=team.pk))


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
        return redirect(url_for("timetracking.index", team=team.pk))
    Customer(team=team, name=name[:200],
             note=(request.form.get("note") or "").strip()).save()
    flash(f"Customer “{name}” created.", "success")
    return redirect(url_for("timetracking.index", team=team.pk))


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
    return redirect(url_for("timetracking.index", team=team.pk))


@timetracking_bp.route("/customers/<cid>/delete", methods=["POST"])
@login_required
@team_required
def customer_delete(cid):
    """Hard-delete a customer with all its projects + logged time. Destructive,
    irreversible — restricted to team managers."""
    from application.services.timetracking import delete_customer
    team = _team()
    user = current_user._get_current_object()
    if not team.is_managed_by(user):
        abort(403)
    c = _customer_or_404(cid, team)
    name = c.name
    stats = delete_customer(c)
    flash(f"Deleted “{name}” — {stats['projects']} project(s), "
          f"{stats['entries']} time entr{'y' if stats['entries'] == 1 else 'ies'}.",
          "success")
    return redirect(url_for("timetracking.index", team=team.pk))


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
        return redirect(url_for("timetracking.index", team=team.pk))
    budget_value, budget_type = _parse_budget(request.form)
    TimeTrackingProject(
        team=team, customer=customer, name=name[:200],
        note=(request.form.get("note") or "").strip(),
        hourly_rate_cents=_parse_rate_cents(request.form.get("hourly_rate")),
        budget_value=budget_value, budget_type=budget_type,
        custom_fields=parse_project_fields(_field_rows(request.form)),
    ).save()
    flash(f"Project “{name}” created.", "success")
    return redirect(url_for("timetracking.index", team=team.pk))


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
    p.custom_fields = parse_project_fields(_field_rows(request.form))
    p.archived = request.form.get("archived") == "1"
    p.save()
    flash("Project updated.", "success")
    return redirect(url_for("timetracking.index", team=team.pk))


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
    return redirect(url_for("timetracking.index", team=team.pk))


# ---------------------------------------------------------------------------
# Linking a Cairn folder/project to a billing project
# ---------------------------------------------------------------------------

@timetracking_bp.route("/link", methods=["GET", "POST"])
@login_required
@team_required
def link_item():
    """Bind a Cairn folder/project to a billing project (or clear the link).

    Tasks below the linked container inherit the billing project automatically
    (see services.timetracking.resolve_time_project). GET is not a form — it
    only exists so the URL isn't a dead end when someone lands on it."""
    team = _team()
    user = current_user._get_current_object()
    if request.method == "GET":
        return redirect(url_for("timetracking.index", team=team.pk))
    item_id = to_oid(request.form.get("item_id"))
    if item_id is None:
        # The picker's placeholder option — nothing was chosen.
        flash("Pick a folder or project to link.", "warning")
        return redirect(url_for("timetracking.index", team=team.pk))
    item = Item.objects(pk=item_id, owner=user,
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
    return redirect(url_for("timetracking.index", team=team.pk))
