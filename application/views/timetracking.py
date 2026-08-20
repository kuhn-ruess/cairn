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
    ref_label,
    to_oid,
    cancel_entry,
    custom_values,
    day_sheet,
    default_project_id,
    discard_timer,
    edit_entry,
    may_edit_entry,
    may_touch_entry,
    restore_entry,
    parse_clock,
    parse_duration,
    parse_project_fields,
    pickable_projects,
    project_for_user,
    set_intake_project,
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
                  .only("id", "title", "kind", "parent", "owner", "shared_teams",
                        "time_project"))
    links_by_project: dict[str, list] = {}
    for it in linked:
        links_by_project.setdefault(str(it.time_project.id), []).append(it)
    # Colleagues link their own containers too, so this list is not limited to
    # what the viewer owns: resolve each one's path, and whose private area it
    # sits in when it isn't shared with the team.
    from application.services.containers import describe_containers
    link_info = describe_containers(linked, team=team)

    # Containers the user can attach, labelled with their full breadcrumb
    # ("🗂️  Work / Acme / Website") — a bare title doesn't say *which* of the
    # three "Website" projects you are about to bill. Same picker builder the
    # task and project inspectors use. Owner-only, matching what link_item
    # accepts.
    user = current_user._get_current_object()
    from application.services.containers import container_choices
    linkable = container_choices(user, selectable_kinds=("project", "folder"))

    # Budget vs. actual — one bucket pass for the whole team, then a status per
    # project (None when no budget set).
    from application.services.tz import user_zone
    from application.services.timetracking import budget_status, team_consumption
    today = datetime.now(user_zone(user)).date()
    consumption = team_consumption(team, f"{today.year:04d}-{today.month:02d}")
    budgets = {str(p.id): budget_status(p, consumption.get(str(p.id)))
               for p in projects}
    # What a reset would destroy, from the same consumption pass — no query per
    # project just to label a danger-zone button.
    logged = {str(p.id): consumption.get(str(p.id), {}).get("minutes", 0)
              for p in projects}

    # Flat "customer › project" list for the move picker — the target of a
    # move is usually under a *different* customer, so the per-customer
    # grouping above can't serve it.
    all_projects = [{"id": str(p.id),
                     "label": f"{p.customer.name} › {p.name}"}
                    for p in projects if not p.archived]
    all_projects.sort(key=lambda r: r["label"].lower())

    return render_template(
        "timetracking/index.html",
        team=team,
        all_teams=_user_teams(user),
        can_manage=team.is_managed_by(user),
        customers=customers,
        all_projects=all_projects,
        by_customer=by_customer,
        links_by_project=links_by_project,
        link_info=link_info,
        linkable=linkable,
        logged=logged,
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
    elif want == "report":
        # The report's filters ride along in the POST, so a corrected entry
        # comes back into the same view you were looking at.
        from urllib.parse import parse_qsl
        from werkzeug.datastructures import MultiDict
        args = MultiDict(parse_qsl(request.form.get("report_args") or ""))
        out["block"] = render_template("timetracking/_report_body.html",
                                       **_report_context(args))
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


@timetracking_bp.route("/entry/<bucket_id>/<entry_id>/restore", methods=["POST"])
@login_required
@team_required
def entry_restore(bucket_id, entry_id):
    """Take back a cancelled entry. Nothing was ever deleted, so this is just
    the reverse of the ✕ — same right as cancelling it in the first place."""
    user = current_user._get_current_object()
    bucket = TimeBucket.objects(pk=to_oid(bucket_id) or ObjectId()).first()
    if not bucket:
        abort(404)
    team = bucket.team
    if str(team.id) not in _team_ids(user):
        abort(403)
    entry = next((e for e in bucket.entries if e.entry_id == entry_id), None)
    if not may_touch_entry(user, team, entry, status="cancelled"):
        abort(403)
    restore_entry(bucket=bucket, entry_id=entry_id, user=user)
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


def _report_filters(args=None):
    """Parse the report query string into normalised filter values + the
    resolved date range, shared by the HTML report, the exports and the
    in-place edit (which posts the filters back so the swapped-in body shows
    the same view you were correcting)."""
    from application.services.tz import user_zone
    from application.services.timetracking import resolve_period

    args = request.args if args is None else args
    user = current_user._get_current_object()
    today = datetime.now(user_zone(user)).date()
    preset = args.get("period") or "this_month"
    date_from, date_to = resolve_period(
        preset, args.get("from"), args.get("to"), today)

    def _tri(name):
        v = args.get(name)
        return True if v == "1" else False if v == "0" else None

    return {
        "preset": preset,
        "date_from": date_from,
        "date_to": date_to,
        "customer_id": args.get("customer") or None,
        "project_id": args.get("project") or None,
        "user_id": args.get("user") or None,
        "billable": _tri("billable"),
        "locked": _tri("locked"),
        # Deleted time is cancelled, never dropped — so it has to be findable.
        "state": (args.get("state")
                  if args.get("state") in ("active", "cancelled", "all")
                  else "active"),
    }


def _report_context(args=None):
    """Everything the report body renders from, for a page load or a swap."""
    from application.services.timetracking import can_bill, pickable_projects
    team = _team()
    user = current_user._get_current_object()
    f = _report_filters(args)
    data = _grouped_report(team, f)
    projects = pickable_projects(user)
    header, rows = _export_table(data)
    return dict(
        team=team, f=f, can_bill=can_bill(user, team),
        can_unlock=team.is_managed_by(user),
        # Field definitions for the inline edit forms, and the export exactly as
        # it will be written — the preview is the export, not a description of it.
        fields_by_project={p["id"]: p["fields"] for p in projects},
        export_header=header, export_rows=rows,
        **data)


def _export_table(data):
    """``(header, rows)`` for the report export — one flat table.

    The CSV, the Excel-flavoured CSV and the on-page preview all render this,
    so what you see before downloading is what the file contains."""
    from application.services.timetracking import ref_label
    header = ["Date", "Start", "End", "Customer", "Project", "Service", "User",
              "Minutes", "Hours", "Billable", "Billed", "Amount (EUR)",
              "Description", "Fields"]
    rows = []
    for r in sorted(data["rows"], key=lambda r: (r["spent_on"], r.get("start_time") or "")):
        rows.append([
            r["spent_on"].isoformat(), r.get("start_time", ""), r.get("end_time", ""),
            r["customer"], r["project"], r.get("service", ""), r["user_name"],
            r["minutes"], f"{r['minutes'] / 60:.2f}",
            "yes" if r["billable"] else "no", "yes" if r["locked"] else "no",
            f"{(r['amount_cents'] or 0) / 100:.2f}",
            (ref_label(r["external_ref"]) + " " if r.get("external_ref") else "")
            + r.get("description", r["note"]),
            "; ".join(f"{c['label']}: {c['value']}" for c in r.get("custom", [])),
        ])
    return header, rows


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
        state=f["state"], viewer=user,
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
    move_targets = sorted(
        ({"id": str(p.id), "label": f"{p.customer.name} › {p.name}"}
         for p in projects_list if not p.archived),
        key=lambda r: r["label"].lower())
    return {
        "groups": out, "rows": rows,
        "grand_minutes": grand_min, "grand_amount": grand_amt,
        "customers": customers_list, "projects": projects_list, "members": members,
        "move_targets": move_targets,
    }


@timetracking_bp.route("/report")
@login_required
@team_required
def report():
    from application.services.timetracking import PERIOD_PRESETS
    user = current_user._get_current_object()
    return render_template(
        "timetracking/report.html", all_teams=_user_teams(user),
        presets=PERIOD_PRESETS, **_report_context())


@timetracking_bp.route("/report/bill", methods=["POST"])
@login_required
@team_required
def report_bill():
    """Bulk lock (bill) or unlock the selected entries, then back to the report
    with the same filters preserved."""
    from application.services.timetracking import move_entries, set_locked
    team = _team()
    user = current_user._get_current_object()
    pairs = []
    for raw in request.form.getlist("entry"):
        bid, _, eid = raw.partition(":")
        if bid and eid:
            pairs.append((bid, eid))
    action = request.form.get("action")
    if action == "move":
        # Re-filing time onto another project changes what gets invoiced to
        # whom — a manager's call, same as billing it.
        if not team.is_managed_by(user):
            abort(403)
        target = _project_or_404(request.form.get("target_id"), team)
        res = move_entries(team=team, pairs=pairs, target=target, actor=user)
        if res["moved"]:
            flash(f"{res['moved']} entr{'y' if res['moved'] == 1 else 'ies'} "
                  f"moved to “{target.name}”"
                  + (f"; {res['locked']} billed one(s) left where they were"
                     if res["locked"] else "") + ".", "success")
        else:
            flash("Nothing moved." + (" Billed entries are frozen."
                                      if res["locked"] else ""), "info")
        args = {k: v for k, v in request.form.items()
                if k in ("team", "period", "from", "to", "customer", "project",
                         "user", "billable", "locked", "state")}
        return redirect(url_for("timetracking.report", **args))
    lock = action != "unlock"
    n = set_locked(team=team, user=user, items=pairs, locked=lock)
    if n:
        flash(f"{n} entr{'y' if n == 1 else 'ies'} "
              f"marked {'billed' if lock else 'open'}.", "success")
    else:
        flash("Nothing changed.", "info")
    # Preserve the report filters (incl. the selected team) from the hidden form.
    args = {k: v for k, v in request.form.items()
            if k in ("team", "period", "from", "to", "customer", "project",
                     "user", "billable", "locked", "state")}
    return redirect(url_for("timetracking.report", **args))


@timetracking_bp.route("/report/restore", methods=["POST"])
@login_required
@team_required
def report_restore():
    """Take a deleted entry back from the report.

    The day sheet can only offer this to the person the entry belongs to, and
    imported time booked for an agent with no Cairn account belongs to nobody —
    the report is the only place it is visible at all, so it is also where it
    has to be recoverable."""
    team = _team()
    user = current_user._get_current_object()
    bucket = TimeBucket.objects(pk=to_oid(request.form.get("bucket_id")),
                                team=team).first()
    entry_id = (request.form.get("entry_id") or "").strip()
    entry = next((e for e in (bucket.entries if bucket else [])
                  if e.entry_id == entry_id), None)
    if bucket is None or entry is None:
        abort(404)
    if not may_touch_entry(user, team, entry, status="cancelled"):
        abort(403)
    if restore_entry(bucket=bucket, entry_id=entry_id, user=user):
        flash("Entry restored.", "success")
    else:
        flash("Nothing to restore.", "info")
    args = {k: v for k, v in request.form.items()
            if k in ("team", "period", "from", "to", "customer", "project",
                     "user", "billable", "locked", "state")}
    return redirect(url_for("timetracking.report", **args))


@timetracking_bp.route("/report.csv")
@login_required
@team_required
def report_csv():
    """The report as a file. ``?excel=1`` writes the flavour Excel opens on a
    double-click in a German locale — BOM, semicolons, decimal commas — while
    the plain form stays a neutral comma CSV for anything that parses it."""
    import csv
    import io
    from flask import Response

    team = _team()
    f = _report_filters()
    data = _grouped_report(team, f)
    header, rows = _export_table(data)
    excel = request.args.get("excel") == "1"

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";" if excel else ",")
    w.writerow(header)
    for row in rows:
        if excel:
            # Hours and amounts are numbers to Excel only with a decimal comma.
            row = [str(v).replace(".", ",") if i in (8, 11) else v
                   for i, v in enumerate(row)]
        w.writerow(row)

    body = ("\ufeff" + buf.getvalue()) if excel else buf.getvalue()
    name = (f"cairn-time-{f['date_from']:%Y%m%d}-{f['date_to']:%Y%m%d}"
            f"{'-excel' if excel else ''}.csv")
    return Response(body, mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename={name}",
    })


# ---------------------------------------------------------------------------
# Migrating off mite
# ---------------------------------------------------------------------------

@timetracking_bp.route("/import")
@login_required
@team_required
def import_page():
    """The mite import on its own page — it is a one-off migration, not part of
    keeping customers and projects."""
    team = _team()
    user = current_user._get_current_object()
    if not team.is_managed_by(user):
        abort(403)
    return render_template("timetracking/import_mite.html", team=team,
                           all_teams=_user_teams(user))


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
        return redirect(url_for("timetracking.import_page", team=team.pk))

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
    return redirect(url_for("timetracking.import_page", team=team.pk))


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


@timetracking_bp.route("/customers/<cid>")
@login_required
@team_required
def customer_detail(cid):
    """One customer on its own page: who they are, how they are billed, what
    they have. The overview stays a list — everything an invoice needs is too
    much for a dropdown inside a card."""
    from application.services.timetracking import team_consumption
    from application.services.tz import user_zone

    team = _team()
    user = current_user._get_current_object()
    c = _customer_or_404(cid, team)
    projects = list(TimeTrackingProject.objects(team=team, customer=c)
                    .order_by("+archived", "+name"))
    today = datetime.now(user_zone(user)).date()
    consumption = team_consumption(team, f"{today.year:04d}-{today.month:02d}")
    minutes_by_project = {str(p.id): consumption.get(str(p.id), {}).get("minutes", 0)
                          for p in projects}
    return render_template(
        "timetracking/customer.html", team=team, all_teams=_user_teams(user),
        can_manage=team.is_managed_by(user), customer=c, projects=projects,
        minutes_by_project=minutes_by_project,
        total_minutes=sum(minutes_by_project.values()))


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

    # Billing details — stored as given (see the model): this is what someone
    # reads off when invoicing, not something to normalise behind their back.
    for field, limit in (("legal_name", 200), ("vat_id", 64), ("tax_number", 64),
                         ("invoice_email", 200), ("contact_name", 200),
                         ("phone", 64), ("customer_number", 64)):
        if field in request.form:
            setattr(c, field, (request.form.get(field) or "").strip()[:limit] or None)
    for field in ("address", "billing_note"):
        if field in request.form:
            setattr(c, field, (request.form.get(field) or "").strip()[:4000])
    if "payment_terms_days" in request.form:
        raw = (request.form.get("payment_terms_days") or "").strip()
        try:
            c.payment_terms_days = max(0, int(raw)) if raw else None
        except ValueError:
            c.payment_terms_days = None
    # The key an importer recognises this customer by. Editable on purpose:
    # moving a Zammad organisation's key onto the customer you already had is
    # what stops the import from re-creating its own every sync.
    if "external_id" in request.form:
        c.external_id = (request.form.get("external_id") or "").strip()[:100] or None
    c.save()
    flash("Customer updated.", "success")
    # Back where the editing happened — the overview only ever sent the name.
    if request.form.get("from") == "index":
        return redirect(url_for("timetracking.index", team=team.pk))
    return redirect(url_for("timetracking.customer_detail", cid=str(c.id),
                            team=team.pk))


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
        time_mode=("clock" if request.form.get("time_mode") == "clock"
                   else "duration"),
        show_source=request.form.get("show_source") == "1",
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
    p.time_mode = "clock" if request.form.get("time_mode") == "clock" else "duration"
    p.show_source = request.form.get("show_source") == "1"
    p.archived = request.form.get("archived") == "1"
    p.save()
    # One intake project per customer, so switching it on has to switch it off
    # elsewhere — hence the service call rather than a plain field assignment.
    set_intake_project(p, request.form.get("intake") == "1")
    flash("Project updated.", "success")
    return redirect(url_for("timetracking.index", team=team.pk))


@timetracking_bp.route("/projects/<pid>/purge", methods=["POST"])
@login_required
@team_required
def project_purge(pid):
    """Delete every time entry on a billing project — the reset for a project
    that was set up wrong or used for a trial.

    Guarded three ways, because it destroys billing history: only a team manager
    may do it, the project's exact name has to be typed to confirm, and the
    result says how much went (including how much of it was already billed)."""
    from application.services.timetracking import purge_project_entries

    team = _team()
    user = current_user._get_current_object()
    if not team.is_managed_by(user):
        abort(403)
    p = _project_or_404(pid, team)
    if (request.form.get("confirm") or "").strip() != p.name:
        flash(f"Type the project name exactly (“{p.name}”) to reset it — "
              "nothing was deleted.", "warning")
        return redirect(url_for("timetracking.index", team=team.pk))
    stats = purge_project_entries(p)
    if stats["entries"]:
        flash(f"Reset “{p.name}”: {stats['entries']} entr"
              f"{'y' if stats['entries'] == 1 else 'ies'} deleted "
              f"({stats['minutes'] // 60}h {stats['minutes'] % 60}m"
              + (f", {stats['locked']} of them already billed" if stats["locked"] else "")
              + "). The project itself is untouched.", "success")
    else:
        flash(f"“{p.name}” had no time logged on it.", "info")
    return redirect(url_for("timetracking.index", team=team.pk))


@timetracking_bp.route("/projects/<pid>/move", methods=["POST"])
@login_required
@team_required
def project_move_entries(pid):
    """Move every entry booked on this project onto another one.

    The way to take an auto-created intake project out of the picture without
    losing an hour: move its time onto the project you actually bill, then
    delete it. Crossing customers is allowed — an imported Zammad organisation
    is quite often a customer you already had under another name."""
    from application.services.timetracking import (
        move_entries, project_entries_for_move)

    team = _team()
    user = current_user._get_current_object()
    if not team.is_managed_by(user):
        abort(403)
    src = _project_or_404(pid, team)
    target = _project_or_404(request.form.get("target_id"), team)
    if str(target.id) == str(src.id):
        flash("Pick a different project to move the time to.", "warning")
        return redirect(url_for("timetracking.index", team=team.pk))
    pairs = project_entries_for_move(src)
    if not pairs:
        flash(f"“{src.name}” has no time logged on it.", "info")
        return redirect(url_for("timetracking.index", team=team.pk))
    res = move_entries(team=team, pairs=pairs, target=target, actor=user)
    msg = (f"Moved {res['moved']} entr{'y' if res['moved'] == 1 else 'ies'} "
           f"from “{src.name}” to “{target.name}”.")
    if res["locked"]:
        msg += (f" {res['locked']} billed entr"
                f"{'y' if res['locked'] == 1 else 'ies'} stayed — reopen them "
                f"first if they should move too.")
    flash(msg, "success")
    return redirect(url_for("timetracking.index", team=team.pk))


@timetracking_bp.route("/projects/<pid>/delete", methods=["POST"])
@login_required
@team_required
def project_delete(pid):
    """Delete a billing project outright.

    Refuses while time is still booked on it — move it somewhere first (that
    is the point of the move above). ``purge=1`` deletes the time with it,
    guarded like the reset: manager only, and the name typed out."""
    from application.services.timetracking import delete_project

    team = _team()
    user = current_user._get_current_object()
    if not team.is_managed_by(user):
        abort(403)
    p = _project_or_404(pid, team)
    if (request.form.get("confirm") or "").strip() != p.name:
        flash(f"Type the project name exactly (“{p.name}”) to delete it — "
              "nothing was deleted.", "warning")
        return redirect(url_for("timetracking.index", team=team.pk))
    name = p.name
    res = delete_project(p, purge=request.form.get("purge") == "1")
    if not res["deleted"]:
        flash(f"“{name}” still has {res['entries']} entr"
              f"{'y' if res['entries'] == 1 else 'ies'} on it. Move them to "
              f"another project first, or tick “delete the time too”.",
              "warning")
    else:
        flash(f"“{name}” deleted"
              + (f" with {res['entries']} entr"
                 f"{'y' if res['entries'] == 1 else 'ies'}" if res["entries"] else "")
              + (f"; {res['unlinked']} Cairn link(s) cleared" if res["unlinked"] else "")
              + ".", "success")
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
    ajax = bool(request.headers.get("X-Partial"))
    if action == "disable":
        p.share_enabled = False
        if not ajax:
            flash("Customer link disabled.", "info")
    else:  # enable / regenerate
        if action == "regenerate" or not p.share_token:
            p.regenerate_share_token()
        p.share_enabled = True
        if not ajax:
            flash("Customer link is live.", "success")
    p.save()
    if request.headers.get("X-Partial"):
        # Swap the block in place — a redirect here would collapse the panel
        # the link was just created in.
        return render_template("timetracking/_project_share.html", p=p, team=team)
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
