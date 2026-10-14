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
from mongoengine import Q

import re
from datetime import datetime, timezone
from urllib.parse import quote

from application.models.item import Item
from application.models.timetracking import Customer, TimeBucket, TimeTrackingProject
from application.models.user import User
from application.services.permissions import team_required
from application.services.timetracking import (
    add_entry,
    ref_label,
    to_oid,
    can_bill,
    can_report,
    cancel_entry,
    custom_values,
    day_sheet,
    default_project_id,
    discard_timer,
    edit_entry,
    relinked_source,
    may_edit_entry,
    may_touch_entry,
    booking_overlap,
    overlap_warning,
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


def _team(args=None):
    """The team the module currently operates on.

    Honours ``team=<id>`` (query string OR form body, so it survives POSTs;
    ``args`` for the report's own filters, which travel as one packed string)
    when the user belongs to it; otherwise the team they pinned as their
    default. 404s if they ask for a team they're not in, 403 if they have none."""
    from application.services.timetracking import default_team
    user = current_user._get_current_object()
    teams = _user_teams(user)
    if not teams:
        abort(403)
    raw = (args or request.values).get("team")
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


def _billing_manager(team):
    """Who may shape the team's books: a manager, or a member the manager has
    named a biller.

    A biller settles the month — they already see the whole book
    (``reachable_project_ids``) and lock time on it. Filing that book is the
    same job: archiving a customer that stopped being one, folding two records
    for one client into one, and moving hours onto the project they actually
    belong to — the intake pile, or the duplicate an import created. Manager-
    only there meant the person doing the work had to fetch somebody else to
    tidy up behind them. Reopening billed time is theirs too — correcting a
    booking that went out wrong is part of settling the month. Deleting a
    customer or a project outright stays with the managers."""
    if not can_bill(current_user._get_current_object(), team):
        abort(403)


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


def _project_choices(team):
    """Every open billing project of the team as ``(id, "Customer › Project")``,
    for the selects that pick one. Archived ones are left out: nothing new
    should be booked onto them."""
    return sorted(
        ((str(o.id), f"{o.customer.name} › {o.name}")
         for o in TimeTrackingProject.objects(team=team, archived=False)),
        key=lambda r: r[1].lower())


def _member_choices(team):
    """The team's people as ``(id, name)``, for the selects that pick one."""
    from application.services.permissions import team_members

    return sorted(((str(m.id), m.display_name or m.username)
                   for m in team_members(team)), key=lambda r: r[1].lower())


def _back(team, **kw):
    """Where a form on a project's page should return to.

    Only a same-site path is honoured, so ``back`` can never be turned into an
    open redirect; anything else falls back to the management list."""
    target = (request.form.get("back") or "").strip()
    if target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect(url_for("timetracking.index", team=team.pk, **kw))


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
    """The management list: customers, their projects, and how to reach them.

    A *list*. Everything about one project — rate, budget, custom fields, the
    container links, the customer's share link, the destructive bits — lives on
    that project's own page. Rendering it per row is what turned this page into
    a wall (each row carried a picker holding every folder the user owns).

    ``?archive=1`` shows what is archived instead of hiding it: archived
    customers with their projects, and the archived projects of customers that
    are still active. Archived means "out of the way", so the default view is
    the working set only."""
    team = _team()
    show_archive = request.args.get("archive") == "1"
    all_customers = list(Customer.objects(team=team).order_by("+archived", "+name"))
    all_projects = list(TimeTrackingProject.objects(team=team)
                        .order_by("+archived", "+name"))
    dead_customers = {str(c.id) for c in all_customers if c.archived}

    def _is_archived(p):
        return bool(p.archived) or str(p.customer.id) in dead_customers

    # A customer whose projects have ALL been archived is out of circulation
    # too: nothing under it can be booked on, so it belongs in the archive
    # rather than as an empty card in the working set (which is where it read
    # as "here, but unusable" — you find it here and then not in the picker).
    # NOT the same as a customer with no projects at all: that one is new, and
    # this list is exactly where you go to give it its first project.
    counted: dict[str, list[bool]] = {}
    for p in all_projects:
        counted.setdefault(str(p.customer.id), []).append(bool(p.archived))
    spent = {cid for cid, flags in counted.items() if flags and all(flags)}

    def _retired(c):
        return bool(c.archived) or str(c.id) in spent

    archived_count = sum(1 for c in all_customers if _retired(c)) + \
        sum(1 for p in all_projects if p.archived)
    if show_archive:
        projects = [p for p in all_projects if _is_archived(p)]
        with_archived = {str(p.customer.id) for p in projects}
        customers = [c for c in all_customers
                     if _retired(c) or str(c.id) in with_archived]
    else:
        projects = [p for p in all_projects if not _is_archived(p)]
        customers = [c for c in all_customers if not _retired(c)]

    # Group projects under their customer, and collect the linked GTD items per
    # project in one query (no per-row lookups in the template).
    by_customer: dict[str, list] = {}
    for p in projects:
        by_customer.setdefault(str(p.customer.id), []).append(p)

    # Containers only: a link lives on a folder/project and the tasks below it
    # inherit (services.timetracking.resolve_time_project). Bindings an older
    # import left on individual tasks are inert and stay out of this list.
    linked = list(Item.objects(time_project__in=[p.id for p in projects],
                               kind__in=["project", "folder"])
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

    user = current_user._get_current_object()
    # Budget vs. actual — one bucket pass for the whole team, then a status per
    # project (None when no budget set).
    from application.services.tz import user_zone
    from application.services.timetracking import budget_status, team_consumption
    today = datetime.now(user_zone(user)).date()
    consumption = team_consumption(team, f"{today.year:04d}-{today.month:02d}")
    budgets = {str(p.id): budget_status(p, consumption.get(str(p.id)))
               for p in projects}
    # The row's "this month" figure, from the same consumption pass — the
    # month's minutes, not the project's lifetime total. Reading the wrong one
    # here is what made this list disagree with the report.
    logged = {str(p.id): consumption.get(str(p.id), {}).get("month_minutes", 0)
              for p in projects}

    return render_template(
        "timetracking/index.html",
        team=team,
        all_teams=_user_teams(user),
        can_manage=team.is_managed_by(user),
        can_file=can_bill(user, team),
        can_import=can_bill(user, team), can_report=can_report(user, team),
        customers=customers,
        show_archive=show_archive,
        archived_count=archived_count,
        # Archived by consequence, not by hand — the archive view says which.
        retired=spent,
        by_customer=by_customer,
        links_by_project=links_by_project,
        link_info=link_info,
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


NO_BOOKING = "Enter a duration (1h30m, 1:30, 90) or a start and end time."


def _unconfirmed_overlap(user, project, spent_on, start, end, exclude=()):
    """The question to ask before saving ``start`` to ``end``, or ``""``.

    An overlap with another entry of the same customer that day is asked
    about, not refused: the page confirms and sends the booking again with
    ``overlap_ok=1``, which saves it as it is."""
    if request.form.get("overlap_ok") == "1":
        return ""
    return booking_overlap(user, project, spent_on, start, end,
                           exclude=exclude)


def _ask_overlap(user, task, question):
    """Nothing saved: the page asks ``question`` and may send it again."""
    if request.headers.get("X-Partial"):
        return jsonify({"confirm_overlap": question})
    return _capture_response(user, task,
                             warning=f"{question} Nothing was saved.")


def _capture_response(user, task=None, fallback=None, warning=""):
    """AJAX → JSON payload; plain POST → back where the user came from.

    ``warning`` is a hint that does not undo anything (the entry overlaps
    another one): the page shows it as a banner, a plain POST as a flash."""
    if request.headers.get("X-Partial"):
        out = _ajax_payload(user, task)
        if warning:
            out["warning"] = warning
        return jsonify(out)
    if warning:
        flash(warning, "warning")
    if task is not None:
        # Time books against any item — a note tracks its own hours too, and
        # sending that one back to the task page would 404.
        if task.kind == "note":
            return redirect(url_for("notes.detail", note_id=str(task.pk)) + "#time")
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


def _live_task(user, task_id):
    """The task a running stopwatch points at, or ``None`` when it is in the
    trash, gone or not this user's to see. Unlike :func:`_viewable_task` this
    never answers 404: the stopwatch has to stop and log its time whatever
    happened to the task meanwhile (the entry keeps the task's id)."""
    tid = to_oid(task_id)
    task = Item.objects(pk=tid).first() if tid is not None else None
    return task if task is not None and task.can_be_viewed_by(user) else None


def _resolve_project(user, task=None, explicit=None):
    """The billing project a capture action targets: an explicitly picked one,
    else the task's inherited/last-used default. ``None`` when nothing fits."""
    if explicit:
        return project_for_user(user, explicit)
    pid = default_project_id(user, task)
    return project_for_user(user, pid) if pid else None


@timetracking_bp.route("/projects/search")
@login_required
@team_required
def project_search():
    """Projects matching ``?q=`` — what the capture bar's picker asks for.

    The bar itself only carries a shortlist, so the rest of a large team's
    projects are found by typing. Returns the same shape the picker's
    ``<option>``s carry, including the custom-field definitions, so a project
    chosen from here behaves exactly like one that was on the page already."""
    from application.services.timetracking import pickable_projects

    user = current_user._get_current_object()
    q = (request.args.get("q") or "").strip()
    rows = pickable_projects(user, query=q, limit=30) if q else \
        pickable_projects(user, limit=30)
    return jsonify({"projects": [
        {"id": r["id"], "label": f"{r['customer']} › {r['name']}",
         "mode": r["mode"], "fields": r["fields"]} for r in rows]})


@timetracking_bp.route("/projects/for-customer")
@login_required
@team_required
def projects_for_customer():
    """The Billing-project picker's options for one customer — the same list the
    project inspector renders, asked for again when the Customer dropdown
    changes.

    It exists so the scope is decided in ONE place: the form that comes back on
    a save and the list the browser swaps in mid-edit are both
    ``services.customers.billing_choices``, so the pair the user ends up with
    always validates. ``?customer=`` empty means no customer chosen, which is
    the whole list, exactly as the form renders it then."""
    from application.services.customers import billing_choices

    user = current_user._get_current_object()
    scope = billing_choices(user,
                            customer_id=(request.args.get("customer") or "").strip(),
                            bound_id=(request.args.get("bound") or "").strip())
    return jsonify({
        "projects": [{"id": pid, "label": label} for pid, label in scope["choices"]],
        "out_of_scope": scope["out_of_scope"],
        "empty": scope["empty"],
    })


@timetracking_bp.route("/task/<task_id>")
@login_required
@team_required
def task_block(task_id):
    """A task's time section (``_task_time.html``) on its own, for a page that
    lists many tasks and opens one's block on demand (the Plan work list).

    The same partial the inspector embeds and every capture endpoint answers
    with, so what the list opens is exactly the inspector's section. Loaded
    lazily because its project shortlist and entry list cost queries per task.
    """
    user = current_user._get_current_object()
    task = _viewable_task(user, task_id)
    if task is None:
        abort(404)
    return render_template("timetracking/_task_time.html", task=task,
                           tt=task_time_context(user, task))


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
    warning = ""
    if project is not None:
        minutes, start, end, custom = _booking(request.form, project)
        if minutes:
            spent_on = _spent_on(request.form.get("spent_on"), user)
            question = _unconfirmed_overlap(user, project, spent_on, start, end)
            if question:
                return _ask_overlap(user, task, question)
            add_entry(
                project=project, user=user, minutes=minutes,
                spent_on=spent_on,
                start_time=start, end_time=end, custom=custom,
                note=request.form.get("note") or "", source_item=task,
                billable=request.form.get("billable") != "0",
            )
            user.tt_last_project = str(project.id)
            user.save()
        else:
            warning = NO_BOOKING
    return _capture_response(user, task, warning=warning)


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
            spent_on = _spent_on(request.form.get("spent_on"), user)
            question = _unconfirmed_overlap(user, project, spent_on, start, end)
            if question:
                return _ask_overlap(user, None, question)
            add_entry(
                project=project, user=user, minutes=minutes,
                spent_on=spent_on,
                start_time=start, end_time=end, custom=custom,
                note=request.form.get("note") or "",
                billable=request.form.get("billable") != "0",
            )
            user.tt_last_project = str(project.id)
            user.save()
    if not minutes:
        flash(NO_BOOKING, "warning")
    return _capture_response(user)


def _context_item(user, raw):
    """The item a quick-log was opened on, or ``None`` — context is a hint, so
    an id that doesn't resolve (or isn't the user's to see) is simply no
    context rather than an error."""
    tid = to_oid(raw)
    if tid is None:
        return None
    item = Item.objects(pk=tid).first()
    return item if item is not None and item.can_be_viewed_by(user) else None


@timetracking_bp.route("/quick")
@login_required
@team_required
def quick_form():
    """The body of the global "Log time" dialog (``t``), rendered when it opens.

    Loaded lazily rather than baked into every page: the project shortlist
    costs queries, and most page views never open the dialog. ``item`` /
    ``tp`` / ``customer`` are the page's context (see ``quick-log.js``)."""
    from application.services.timetracking import quick_log_context
    from application.services.tz import user_zone
    user = current_user._get_current_object()
    ctx = quick_log_context(
        user, item=_context_item(user, request.args.get("item")),
        project_id=(request.args.get("tp") or "").strip(),
        customer_id=(request.args.get("customer") or "").strip())
    return render_template("timetracking/_quick_form.html", q=ctx,
                           today=datetime.now(user_zone(user)).date())


@timetracking_bp.route("/quick", methods=["POST"])
@login_required
@team_required
def quick_log():
    """Book one entry from the "Log time" dialog. JSON either way: the dialog
    stays open on a refusal and says why, and nothing is written then."""
    user = current_user._get_current_object()
    pid = (request.form.get("project_id") or "").strip()
    project = project_for_user(user, pid) if pid else None
    if project is None:
        return jsonify({"ok": False, "error": "Pick a billing project."}), 400
    minutes, start, end, custom = _booking(request.form, project)
    if not minutes:
        return jsonify({"ok": False, "error": NO_BOOKING}), 400
    spent_on = _spent_on(request.form.get("spent_on"), user)
    question = _unconfirmed_overlap(user, project, spent_on, start, end)
    if question:
        # Not an error: the dialog asks and sends the same booking again.
        return jsonify({"ok": False, "confirm_overlap": question}), 409
    source = None
    if request.form.get("link_item") == "1":
        source = _context_item(user, request.form.get("item_id"))
    add_entry(
        project=project, user=user, minutes=minutes,
        spent_on=spent_on,
        start_time=start, end_time=end, custom=custom,
        note=(request.form.get("note") or "").strip(), source_item=source,
        billable=request.form.get("billable") != "0",
    )
    user.tt_last_project = str(project.id)
    user.save()
    customer = project.customer.name if project.customer else ""
    out = _ajax_payload(user)
    out.update({"ok": True, "minutes": minutes,
                "project": f"{customer} › {project.name}" if customer else project.name})
    return jsonify(out)


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
        task = _live_task(user, timer.source_item_id)
    entry = stop_timer(user)
    if entry and not request.headers.get("X-Partial"):
        flash(f"Logged {entry.minutes // 60}h {entry.minutes % 60}m "
              f"on {entry.project_name}.", "success")
    return _capture_response(user, task, warning=overlap_warning(entry))


@timetracking_bp.route("/timer/discard", methods=["POST"])
@login_required
@team_required
def timer_discard():
    """Throw the running stopwatch away without logging it."""
    user = current_user._get_current_object()
    timer = user.running_timer
    task = None
    if timer and timer.source_item_id:
        task = _live_task(user, timer.source_item_id)
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
    task = None
    if entry.source_item_id:
        task = Item.objects(pk=to_oid(entry.source_item_id) or ObjectId()).first()
    # Both times typed but no span in them (the same twice, or unreadable):
    # saving would quietly drop the times, so nothing is saved and it says why.
    if ((request.form.get("start_time") or "").strip()
            and (request.form.get("end_time") or "").strip()
            and parse_clock(request.form.get("start_time"),
                            request.form.get("end_time")) is None):
        return _capture_response(
            user, task, warning="Start and end must be two different times "
                                "of day. Nothing was changed.")
    # The corrected times on top of another entry of the same customer: asked
    # first, before the move below changes anything.
    target_id = (request.form.get("project_id") or "").strip()
    target = project_for_user(user, target_id or entry.project_id)
    if target is not None:
        _, start, end, _ = _booking(request.form, target)
        author = (User.objects(pk=entry.user_id).first()
                  if entry.user_id else None) or user
        spent_on = (_spent_on(request.form.get("spent_on"), author)
                    if request.form.get("spent_on") else entry.spent_on)
        question = _unconfirmed_overlap(author, target, spent_on, start, end,
                                        exclude={entry_id})
        if question:
            return _ask_overlap(user, task, question)
    # Re-filing the entry onto another billing project — the per-row version of
    # "Move selected" on the report, and a biller's call for the same reason.
    # It happens *first*: a move to another customer moves the entry to that
    # customer's bucket, which is the bucket the correction below has to write.
    if target_id and target_id != entry.project_id:
        from application.services.timetracking import move_entries
        _billing_manager(team)
        target = _project_or_404(target_id, team)
        move_entries(team=team, pairs=[(str(bucket.id), entry_id)],
                     target=target, actor=user)
        moved_to = TimeBucket.objects(team=team, customer=target.customer,
                                      period=bucket.period,
                                      entries__entry_id=entry_id).first()
        if moved_to is not None:
            bucket = moved_to
            entry = next((e for e in bucket.entries
                          if e.entry_id == entry_id), entry)
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
    # The task link: only sent when the user changed it. "none" (or empty)
    # removes it, an id moves the entry onto that task (one the user can see).
    source = None
    if "source_item_id" in request.form:
        raw = (request.form.get("source_item_id") or "").strip()
        linked = None
        if raw and raw != "none":
            linked = _viewable_task(user, raw)
            if linked is None or linked.kind not in ("task", "note"):
                abort(400)
        if (str(linked.pk) if linked else None) != entry.source_item_id:
            source = relinked_source(entry, linked, user)
    edit_entry(bucket=bucket, entry_id=entry_id, user=user, minutes=minutes,
               spent_on=spent_on, note=note, custom=custom,
               start_time=start, end_time=end, source=source,
               billable=request.form.get("billable") != "0")
    return _capture_response(user, task)


# ---------------------------------------------------------------------------
# The day sheet — mite's "Zeiten erfassen": today's time, week at a glance
# ---------------------------------------------------------------------------

def _default_can_report(user) -> bool:
    """Whether the team the module opens in grants the reporting right, for
    the pages that link into it without naming a team."""
    from application.services.timetracking import default_team
    team = default_team(user)
    return bool(team) and can_report(user, team)


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
        all_teams=_user_teams(user),
        # The day sheet names no team, and its links open the default one.
        can_report=_default_can_report(user))


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
    # Dates given without a preset ARE the period — a hand-built link with
    # ?from=&to= means the range it names, not the current month.
    preset = args.get("period") or (
        "custom" if (args.get("from") or args.get("to")) else "this_month")
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
        # Archived projects are finished business: out of the report by
        # default, one click away, and never silently — what they hold is
        # summarised where the totals are.
        "archived": args.get("archived") == "1",
        # "entries" lists every booking; "month" pivots the same result into
        # one column per calendar month (a customer's year at a glance).
        "view": "month" if args.get("view") == "month" else "entries",
        # Export shape: one line per day (and per run of back-to-back clock
        # times) instead of one per booking — see _merge_per_day.
        "merge": args.get("merge") == "1",
    }


def _report_context(args=None):
    """Everything the report body renders from, for a page load or a swap."""
    from application.services.timetracking import project_fields
    team = _team(args)
    user = current_user._get_current_object()
    f = _report_filters(args)
    data = _grouped_report(team, f)
    header, rows, _cols = _export_table(data, _filter_project(f), f["merge"])
    monthly = None
    if f["view"] == "month":
        from application.services.timetracking import monthly_breakdown
        monthly = monthly_breakdown(data["rows"], f["date_from"], f["date_to"])
    return dict(
        team=team, f=f, can_bill=can_bill(user, team), monthly=monthly,
        can_import=can_bill(user, team), can_report=can_report(user, team),
        # Field definitions for the inline edit forms — for the projects on
        # screen, not for every project the team has, and the export exactly as
        # it will be written: the preview is the export, not a description of it.
        fields_by_project=project_fields(
            r["project_id"] for r in data["rows"]),
        export_header=header, export_rows=rows,
        **data)


def _field_columns(rows):
    """The custom fields worth a column of their own, in a stable order.

    Taken from the projects the result actually contains, not from the whole
    team: a report filtered to one customer then carries exactly that
    customer's fields. They used to be crammed into the description as badges
    and squashed into one semicolon-separated cell in the export, which is the
    same as not having them — a "Ticket-Nr." column you can sort by is the
    entire point of defining the field."""
    pids = {r["project_id"] for r in rows if r.get("project_id")}
    if not pids:
        return []
    out, seen = [], set()
    for p in TimeTrackingProject.objects(
            id__in=[to_oid(i) for i in pids if to_oid(i)]
    ).only("id", "name", "custom_fields").order_by("+name"):
        for fd in p.custom_fields or []:
            if fd.key not in seen:
                seen.add(fd.key)
                # Type and choices ride along so the bulk editor can offer a
                # field's actual dropdown instead of a text box that invites a
                # fourth spelling of "Pauschal".
                out.append({"key": fd.key, "label": fd.label,
                            "type": fd.field_type or "text",
                            "options": list(fd.options or [])})
    # A value can outlive its definition (the field was removed from the
    # project after it was booked); those keep their own column too, labelled
    # as the entry remembers them.
    for r in rows:
        for c in r.get("custom", []):
            if c["key"] not in seen:
                seen.add(c["key"])
                out.append({"key": c["key"], "label": c["label"] or c["key"],
                            "type": "text", "options": []})
    return out


def _values_by_field(rows) -> dict:
    """field key → the values already booked under it in this result."""
    out: dict = {}
    for r in rows:
        for c in r.get("custom", []):
            if c["value"]:
                out.setdefault(c["key"], set()).add(c["value"])
    return out


# The export's column vocabulary: every key the customer sheet knows
# (services.timetracking.SHEET_BASE_COLUMNS) plus the ones only an internal
# report has — who it is for, and the money. Sharing the keys is what lets a
# project's sheet configuration drive this table too.
REPORT_COLUMNS = [
    {"key": "date",     "label": "Date"},
    {"key": "start",    "label": "Start"},
    {"key": "end",      "label": "End"},
    {"key": "customer", "label": "Customer"},
    {"key": "project",  "label": "Project"},
    {"key": "ref",      "label": "Ticket"},
    {"key": "source",   "label": "Task"},
    {"key": "service",  "label": "Service"},
    {"key": "user",     "label": "User"},
    {"key": "minutes",  "label": "Minutes"},
    {"key": "hours",    "label": "Hours"},
    {"key": "billable", "label": "Billable"},
    {"key": "billed",   "label": "Billed"},
    {"key": "amount",   "label": "Amount (EUR)"},
    {"key": "note",     "label": "Description"},
]

# The report as it has always downloaded, for every filter that isn't a single
# configured project. Ticket and Task are absent on purpose — they ride inside
# the description unless a column asks for them, exactly as on the sheet.
DEFAULT_REPORT_KEYS = ("date", "start", "end", "customer", "project", "service",
                       "user", "minutes", "hours", "billable", "billed",
                       "amount", "note")

# Money has no key in the sheet's vocabulary and never will — that link goes to
# the customer. The gap is a property of THAT vocabulary, though, not a wish to
# read an internal billing report without its amounts, so these follow a
# project's column list instead of being dropped by it.
MONEY_KEYS = ("billable", "billed", "amount")

# Cells Excel only reads as numbers when they carry a decimal comma.
DECIMAL_KEYS = ("hours", "amount")


def _filter_project(f):
    """The single project the report is filtered to, or None.

    Only the explicit project filter counts. A customer who happens to have one
    project would otherwise change the shape of the file from month to month,
    which is the opposite of what a configured column order is for."""
    pid = f.get("project_id")
    oid = to_oid(pid) if pid else None
    return TimeTrackingProject.objects(id=oid).first() if oid else None


def _report_columns(data, project=None):
    """The export's columns, in order.

    A project that has arranged its customer sheet (Time → the project → the
    share link) has already said how its time should be tabulated. A report
    narrowed to exactly that project has no reason to disagree — so its column
    choice and order are followed here, with the money columns appended (see
    MONEY_KEYS). Any other filter keeps the report's own default order."""
    fields = data.get("field_columns") or []
    by_key = {c["key"]: c for c in REPORT_COLUMNS}
    for fd in fields:
        by_key[f"cf:{fd['key']}"] = {"key": f"cf:{fd['key']}", "label": fd["label"]}

    wanted = [k for k in (project.share_columns or []) if k in by_key] if project else []
    if not wanted:
        wanted = list(DEFAULT_REPORT_KEYS) + [f"cf:{fd['key']}" for fd in fields]
        return [by_key[k] for k in wanted if k in by_key]
    return [by_key[k] for k in wanted + [k for k in MONEY_KEYS if k not in wanted]]


def _report_cell(row, key, values, shown):
    """One cell, by column key. ``shown`` is every key this table has, because
    the description gives the ticket and the task away to a column of their own
    rather than repeating them — the sheet's ``_NOTE_ABSORBS`` rule."""
    if key == "date":
        return row["spent_on"].isoformat()
    if key == "start":
        return row.get("start_time", "")
    if key == "end":
        return row.get("end_time", "")
    if key == "customer":
        return row["customer"]
    if key == "project":
        return row["project"]
    if key == "ref":
        return ref_label(row.get("external_ref") or "")
    if key == "source":
        return row.get("source_label", "")
    if key == "service":
        return row.get("service", "")
    if key == "user":
        return row["user_name"]
    if key == "minutes":
        return row["minutes"]
    if key == "hours":
        return f"{row['minutes'] / 60:.2f}"
    if key == "billable":
        return "yes" if row["billable"] else "no"
    if key == "billed":
        return "yes" if row["locked"] else "no"
    if key == "amount":
        return f"{(row['amount_cents'] or 0) / 100:.2f}"
    if key == "note":
        note = row["note"] if "source" in shown else row.get("description", row["note"])
        lead = ref_label(row.get("external_ref") or "") if "ref" not in shown else ""
        return f"{lead} {note}".strip() if lead else note
    if key.startswith("cf:"):
        return values.get(key[3:], "")
    return ""


def _export_table(data, project=None, merge=False):
    """``(header, rows, columns)`` for the report export — one flat table.

    The CSV, the Excel-flavoured CSV and the on-page preview all render this,
    so what you see before downloading is what the file contains. ``merge``
    folds a day's bookings into one line (see :func:`_merge_per_day`)."""
    cols = _report_columns(data, project)
    if merge:
        # Custom fields don't survive a merge: five bookings have five values,
        # and the merged line speaks through its description alone.
        cols = [c for c in cols if not c["key"].startswith("cf:")]
    shown = {c["key"] for c in cols}
    ordered = sorted(data["rows"],
                     key=lambda r: (r["spent_on"], r.get("start_time") or ""))
    if merge:
        return [c["label"] for c in cols], _merge_per_day(ordered, cols, shown), cols
    rows = []
    for r in ordered:
        values = {c["key"]: c["value"] for c in r.get("custom", [])}
        rows.append([_report_cell(r, c["key"], values, shown) for c in cols])
    return [c["label"] for c in cols], rows, cols


def _clock_min(hhmm):
    """``"09:30"`` → 570, anything else → None."""
    try:
        h, m = (hhmm or "").split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def _merge_per_day(rows, cols, shown):
    """The export with one line per day instead of one per booking.

    Bookings merge when they share the day, customer, project, user, billable
    and billed state — anything else would put two invoices' worth of time on
    one line. Within that, bookings with clock times merge only where they
    touch or overlap (one ends when the next starts, or later), so every
    separate block of the day keeps its own line with its own start and end;
    bookings without clock times make one line of their own. Hours and money
    are the SUM of the bookings, not the clock span; the description is the
    bookings' descriptions, comma-separated, each once."""
    groups: dict = {}
    for r in rows:
        key = (r["spent_on"], r["customer"], r["project"], r["user_name"],
               r["billable"], r["locked"])
        groups.setdefault(key, []).append(r)

    lines = []   # (sort key, start, end, bookings)
    for key, members in groups.items():
        loose, blocks = [], []
        for r in members:          # already sorted by start time
            lo = _clock_min(r.get("start_time"))
            hi = _clock_min(r.get("end_time"))
            if lo is None or hi is None:
                loose.append(r)
                continue
            if hi < lo:            # crosses midnight
                hi += 24 * 60
            if blocks and lo <= blocks[-1][1]:
                blocks[-1][1] = max(blocks[-1][1], hi)
                blocks[-1][2].append(r)
            else:
                blocks.append([lo, hi, [r]])
        for lo, hi, members_ in blocks:
            lines.append(((key[0], lo), lo, hi, members_))
        if loose:
            lines.append(((key[0], -1), None, None, loose))
    lines.sort(key=lambda ln: ln[0])

    def hhmm(m):
        return "" if m is None else f"{m // 60 % 24:02d}:{m % 60:02d}"

    out = []
    for _sk, lo, hi, members in lines:
        minutes = sum(r["minutes"] for r in members)
        cents = sum(r["amount_cents"] or 0 for r in members)
        first = members[0]
        line = []
        for c in cols:
            k = c["key"]
            if k == "start":
                line.append(hhmm(lo))
            elif k == "end":
                line.append(hhmm(hi))
            elif k == "minutes":
                line.append(minutes)
            elif k == "hours":
                line.append(f"{minutes / 60:.2f}")
            elif k == "amount":
                line.append(f"{cents / 100:.2f}")
            elif k in ("note", "ref", "source", "service"):
                seen = []
                for r in members:
                    v = _report_cell(r, k, {}, shown)
                    if v and v not in seen:
                        seen.append(v)
                line.append(", ".join(seen))
            else:
                line.append(_report_cell(first, k, {}, shown))
        out.append(line)
    return out


def _monthly_table(monthly):
    """``(header, rows)`` for the per-month export, the same rows the page
    shows: a customer with several projects gets a line of its own above
    them, one with a single project is just that project's line."""
    header = (["Customer", "Project"]
              + [m["label"] for m in monthly["months"]] + ["Total"])

    def hours(mins):
        return f"{mins / 60:.2f}"

    rows = []
    for c in monthly["customers"]:
        if len(c["projects"]) > 1:
            rows.append([c["name"], "All projects"]
                        + [hours(v) for v in c["minutes"]] + [hours(c["total"])])
        for p in c["projects"]:
            rows.append([c["name"], p["name"]]
                        + [hours(v) for v in p["minutes"]] + [hours(p["total"])])
    rows.append(["Total", ""] + [hours(v) for v in monthly["totals"]]
                + [hours(monthly["total"])])
    return header, rows


def _grouped_report(team, f):
    """Build the customer → project → user grouping + totals for the filters."""
    from application.services.timetracking import (
        build_report_rows, report_bounds)
    from application.services.permissions import team_members

    from application.services.timetracking import archived_project_ids

    user = current_user._get_current_object()
    # Without the reporting right the report is the user's own time, whatever
    # ``?user=`` says: the bound is applied here, never taken from the request.
    scope, own_id = report_bounds(user, team)
    dead = archived_project_ids(team)
    rows = build_report_rows(
        team=team, scope_ids=scope,
        date_from=f["date_from"], date_to=f["date_to"],
        customer_id=f["customer_id"], project_id=f["project_id"],
        user_id=own_id,
        billable=f["billable"], locked=f["locked"],
        state=f["state"], archived_ids=dead, viewer=user,
    )
    # Who actually appears in this result. Imported time can carry an author
    # with no Cairn account behind it (a mite or Zammad user nobody matched) —
    # they show up in the "Who" column but a member-only filter can never find
    # them, which reads as "the user filter is broken". They are offered by
    # name, and picked by name.
    authors = []
    seen_names = set()
    for r in rows:
        if r["user_id"] or not r["user_name"] or r["user_name"] in seen_names:
            continue
        seen_names.add(r["user_name"])
        authors.append({"key": f"name:{r['user_name']}", "label": r["user_name"]})
    authors.sort(key=lambda a: a["label"].lower())
    # The user filter is a plain post-filter, applied here so the options above
    # describe the whole result rather than what survived the filter.
    pick = None if own_id else f["user_id"]
    if pick == "none":
        rows = [r for r in rows if not r["user_id"]]
    elif (pick or "").startswith("name:"):
        want = pick[5:]
        rows = [r for r in rows if not r["user_id"] and r["user_name"] == want]
    elif pick:
        rows = [r for r in rows if r["user_id"] == pick]
    # Held back rather than dropped: the count and the hours are reported next
    # to the totals, so an invoice is never short by a number nobody mentioned.
    hidden = []
    if not f["archived"]:
        rows, hidden = ([r for r in rows if not r["archived"]],
                        [r for r in rows if r["archived"]])

    customers = {}
    grand_min = grand_amt = 0
    for r in rows:
        grand_min += r["minutes"]
        grand_amt += r["amount_cents"] or 0
        c = customers.setdefault(r["customer"], {
            "name": r["customer"], "id": r["customer_id"],
            "minutes": 0, "amount": 0, "projects": {}})
        c["minutes"] += r["minutes"]; c["amount"] += r["amount_cents"] or 0
        p = c["projects"].setdefault(r["project_id"], {
            "name": r["project"], "id": r["project_id"],
            "minutes": 0, "amount": 0, "users": {}})
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

    # Filter dropdown data — the working set, plus whatever is currently
    # selected (so an archived project picked on purpose stays picked).
    # "Archived" here means the project's own flag *or* its customer's: see
    # archived_project_ids, which is the same rule the report body uses.
    customers_list = list(Customer.objects(
        Q(team=team) & (Q(archived__ne=True) | Q(id=f["customer_id"] or None))
    ).order_by("+name"))
    # Projects follow the chosen customer: picking "Acme" and then scrolling
    # through every project of every other client to find theirs is a list
    # asking the reader to do the filtering it was given.
    pq = Q(team=team)
    if f["customer_id"]:
        pq &= Q(customer=f["customer_id"])
    projects_list = [p for p in TimeTrackingProject.objects(pq).order_by("+name")
                     if str(p.id) not in dead or str(p.id) == f["project_id"]]
    members = team_members(team) if own_id is None else []
    # Grouped for the picker, so a project is always shown under the client it
    # bills to — two customers with a "Support" project are otherwise two
    # identical-looking options.
    grouped = {}
    for p in projects_list:
        grouped.setdefault(p.customer.name, []).append(p)
    project_groups = [{"customer": name, "projects": ps}
                      for name, ps in sorted(grouped.items(),
                                             key=lambda kv: kv[0].lower())]
    move_targets = sorted(
        ({"id": str(p.id), "label": f"{p.customer.name} › {p.name}"}
         for p in TimeTrackingProject.objects(team=team, archived=False)),
        key=lambda r: r["label"].lower())
    # Which snapshots still have a record behind them: an entry keeps its
    # project's name and id even after the project is gone, and a link to a
    # deleted project is a 404 waiting to be clicked.
    live = {
        "customers": {str(c["_id"]) for c in Customer.objects(
            team=team).only("id").as_pymongo()},
        "projects": {str(p["_id"]) for p in TimeTrackingProject.objects(
            team=team).only("id").as_pymongo()},
    }
    return {
        "groups": out, "rows": rows,
        "grand_minutes": grand_min, "grand_amount": grand_amt,
        "hidden_count": len(hidden),
        "hidden_minutes": sum(r["minutes"] for r in hidden),
        "customers": customers_list, "projects": projects_list,
        "project_groups": project_groups, "members": members,
        "authors": authors,
        "move_targets": move_targets, "live": live,
        "field_columns": _field_columns(rows),
        # Values already in use, per field, so setting one is a pick rather
        # than a typo waiting to happen.
        "field_values": {
            k: sorted(v) for k, v in _values_by_field(rows).items()},
    }


@timetracking_bp.route("/people")
@login_required
@team_required
def people():
    """Who booked this team's time, and who that is in Cairn.

    Imported hours arrive with the author the source system knew — a mite user,
    a Zammad agent — and matching them is guesswork until someone says
    otherwise. Unmatched, their time shows a bare name: it is in the team report
    but in nobody's day sheet, and the report's user filter can only find it by
    that name. This is where that gets decided, once, for everything they have
    booked and everything they will."""
    from application.services.permissions import team_members
    from application.services.timetracking import people_in_time
    from application.services.zammad_billing import agent_rows

    team = _team()
    user = current_user._get_current_object()
    # Everybody's hours, per person: that is reading the team's book.
    if not can_report(user, team):
        abort(403)
    rows = people_in_time(team)
    # The roster holds people an importer met but who may have no time here yet
    # (or whose time is already attributed) — show them too, so a mapping can be
    # made or corrected before the next import rather than after.
    seen = {r["external_user"] for r in rows if r["external_user"]}
    extra = [{"key": a["zammad_id"] and f"{a['source']}:user:{a['zammad_id']}",
              "external_user": f"{a['source']}:user:{a['zammad_id']}",
              "name": a["label"], "user_id": a["user_id"], "entries": 0,
              "minutes": 0, "locked": 0, "source": a["source"],
              "first": None, "last": None}
             for a in agent_rows(team)
             if f"{a['source']}:user:{a['zammad_id']}" not in seen]
    return render_template(
        "timetracking/people.html", team=team, all_teams=_user_teams(user),
        can_manage=team.is_managed_by(user), rows=rows + extra,
        can_import=can_bill(user, team), can_report=can_report(user, team),
        members=sorted(team_members(team),
                       key=lambda m: (m.display_name or m.username or "").lower()))


@timetracking_bp.route("/people/assign", methods=["POST"])
@login_required
@team_required
def people_assign():
    """Point one imported author at a Cairn account — and re-file their time.

    Manager-only: attribution decides whose hours land on an invoice."""
    from application.models.user import User as UserDoc
    from application.services.permissions import team_members
    from application.services.zammad_billing import (
        assign_agent, attribute_name, record_agent)

    team = _team()
    user = current_user._get_current_object()
    if not team.is_managed_by(user):
        abort(403)
    key = (request.form.get("key") or "").strip()
    target_id = (request.form.get("user_id") or "").strip()
    target = None
    if target_id:
        target = UserDoc.objects(pk=target_id).first()
        if target is None or not any(str(m.pk) == target_id
                                     for m in team_members(team)):
            flash("That account isn't a member of this team.", "danger")
            return redirect(url_for("timetracking.people", team=team.pk))

    if key.startswith("name:"):
        # No source identity to remember — only the name the time carries.
        name = key[5:]
        fixed = attribute_name(team, name, target) if target else 0
        if target is None:
            flash("Pick a Cairn account — a name on its own can only be "
                  "assigned, not un-assigned.", "warning")
        elif fixed:
            flash(f"“{name}” → {target.display_name or target.username}: "
                  f"{fixed} entr{'y' if fixed == 1 else 'ies'} re-filed. "
                  f"Time imported later under that name lands correctly only if "
                  f"the importer can match it — check the address it uses.",
                  "success")
        else:
            flash(f"Nothing to re-file for “{name}” — its entries already "
                  f"belong to an account.", "info")
        return redirect(url_for("timetracking.people", team=team.pk))

    source, _, ident = key.partition(":user:")
    source = source or "zammad"
    # The page lists people found in the *entries*, which may include someone no
    # importer ever wrote to the roster (an import that predates it). Recording
    # them here is what makes the mapping outlast this one fix-up and reach the
    # next import.
    if team.zammad_agent(ident, source) is None:
        record_agent(team, zammad_id=ident, source=source,
                     name=(request.form.get("name") or "").strip())
    agent, fixed = assign_agent(team, ident, target, source)
    if agent is None:
        flash("Unknown person.", "warning")
    elif target is None:
        flash(f"“{agent.label}” is no longer mapped to a Cairn account"
              + (f" — {fixed} entr{'y' if fixed == 1 else 'ies'} re-filed under "
                 f"their {source} name." if fixed else "."), "info")
    else:
        flash(f"“{agent.label}” → {target.display_name or target.username}"
              + (f" — {fixed} existing entr{'y' if fixed == 1 else 'ies'} "
                 f"re-attributed." if fixed
                 else " — no existing entry needed changing.")
              + " Future imports follow this mapping.", "success")
    return redirect(url_for("timetracking.people", team=team.pk))


@timetracking_bp.route("/unassigned")
@login_required
@team_required
def unassigned():
    """Imported time nobody has filed yet — one row per ticket, with the
    question the import left open.

    Time whose ticket sits in no linked subtree lands in the customer's intake
    project. That keeps the hours (the alternative is losing them), but the
    open question was only ever visible as a number on a sync result page. Here
    it is a list you can work through."""
    from application.services.timetracking import unassigned_groups

    team = _team()
    user = current_user._get_current_object()
    # Imported time of the whole team, waiting to be filed: the team's book.
    if not can_report(user, team):
        abort(403)
    rows = unassigned_groups(team)
    # Where the time can go: every live project of the team, labelled by
    # customer — an intake project's own customer is often not the one the
    # hours belong to.
    from mongoengine import DoesNotExist

    def _label(o):
        # A project whose customer was deleted can't be a target — and must not
        # take the page down with it when its reference is dereferenced.
        try:
            return f"{o.customer.name} › {o.name}"
        except DoesNotExist:
            return ""

    targets = sorted(
        (t for t in ({"id": str(o.id), "label": _label(o),
                      "intake": bool(o.intake)}
                     for o in TimeTrackingProject.objects(team=team,
                                                          archived=False))
         if t["label"]),
        key=lambda r: r["label"].lower())
    # Rows that lie entirely before the day the connection went live. They are
    # not waiting to be filed — they are what an import that ran without a
    # cutoff dragged in, and the answer to them is the purge, not a project.
    cutoff = (getattr(team, "zammad_time_from", "") or "").strip()
    stale = [r for r in rows if cutoff and r["last"] < cutoff]
    return render_template(
        "timetracking/unassigned.html", team=team, all_teams=_user_teams(user),
        can_manage=team.is_managed_by(user), rows=rows, targets=targets,
        can_import=can_bill(user, team), can_report=can_report(user, team),
        cutoff=cutoff, stale=len(stale),
        stale_minutes=sum(r["minutes"] for r in stale),
        total_minutes=sum(r["minutes"] for r in rows))


@timetracking_bp.route("/unassigned/assign", methods=["POST"])
@login_required
@team_required
def unassigned_assign():
    """File one ticket's time onto a real billing project.

    Optionally link the folder above the ticket to that project in the same
    click — without it the next sync resolves nothing again and books the next
    hours straight back into the intake pile, which is how a list like this
    fills up faster than anyone empties it."""
    from application.models.item import Item
    from application.services.timetracking import move_entries, unassigned_groups

    team = _team()
    user = current_user._get_current_object()
    if not team.is_managed_by(user):
        abort(403)
    target = _project_or_404(request.form.get("target_id"), team)
    key = (request.form.get("project_id") or "",
           request.form.get("item_id") or "",
           request.form.get("external_ref") or "")
    row = next((r for r in unassigned_groups(team)
                if (str(r["project"].id), r["item_id"], r["external_ref"]) == key),
               None)
    if row is None or not row["pairs"]:
        flash("That time has already been filed.", "info")
        return redirect(url_for("timetracking.unassigned", team=team.pk))

    res = move_entries(team=team, pairs=row["pairs"], target=target, actor=user)
    msg = (f"Moved {res['moved']} entr{'y' if res['moved'] == 1 else 'ies'} "
           f"to “{target.name}”.")
    if res["locked"]:
        msg += (f" {res['locked']} billed entr"
                f"{'y' if res['locked'] == 1 else 'ies'} stayed where they were "
                f"settled.")
    if request.form.get("link_container") == "1" and row["container"] is not None:
        container = Item.all_objects(pk=row["container"].pk).first()
        if container is not None and container.can_be_edited_by(user):
            container.time_project = target
            container.save()
            msg += (f" “{container.title}” now books to it, so the next sync "
                    f"files these tickets straight away.")
    flash(msg, "success")
    return redirect(url_for("timetracking.unassigned", team=team.pk))


@timetracking_bp.route("/trash")
@login_required
@team_required
def trash():
    """Deleted time, gathered in one place — and the only door out of the
    module's append-only rule.

    Cancelled entries are kept forever and counted nowhere, which makes them
    invisible rather than gone: this lists them, restores them, or removes them
    for good."""
    from application.services.timetracking import cancelled_entries

    team = _team()
    user = current_user._get_current_object()
    rows = cancelled_entries(team=team, viewer=user)
    return render_template(
        "timetracking/trash.html", team=team, all_teams=_user_teams(user),
        can_manage=team.is_managed_by(user), rows=rows,
        can_import=can_bill(user, team), can_report=can_report(user, team),
        total_minutes=sum(r["minutes"] for r in rows))


@timetracking_bp.route("/trash/act", methods=["POST"])
@login_required
@team_required
def trash_act():
    """Restore or permanently delete entries from the trash.

    ``action`` ∈ restore / purge / empty. Everything is scoped by
    ``may_touch_entry`` inside the services, so "empty" empties what *you* may
    empty — a member's own deleted time, a manager's whole team."""
    from application.services.timetracking import cancelled_entries, purge_entries

    team = _team()
    user = current_user._get_current_object()
    action = request.form.get("action")
    if action == "empty":
        pairs = [(r["bucket_id"], r["entry_id"])
                 for r in cancelled_entries(team=team, viewer=user)]
    else:
        pairs = []
        for raw in request.form.getlist("entry"):
            bid, _, eid = raw.partition(":")
            if bid and eid:
                pairs.append((bid, eid))
    if not pairs:
        flash("Nothing selected.", "info")
        return redirect(url_for("timetracking.trash", team=team.pk))

    if action == "restore":
        done = 0
        for bucket_id, entry_id in pairs:
            bucket = TimeBucket.objects(pk=to_oid(bucket_id), team=team).first()
            if bucket is None:
                continue
            entry = next((e for e in bucket.entries
                          if e.entry_id == entry_id), None)
            if not may_touch_entry(user, team, entry, status="cancelled"):
                continue
            if restore_entry(bucket=bucket, entry_id=entry_id, user=user):
                done += 1
        flash(f"{done} entr{'y' if done == 1 else 'ies'} restored."
              if done else "Nothing restored.",
              "success" if done else "info")
        return redirect(url_for("timetracking.trash", team=team.pk))

    res = purge_entries(team=team, pairs=pairs, viewer=user)
    if res["purged"]:
        flash(f"{res['purged']} entr{'y' if res['purged'] == 1 else 'ies'} "
              f"deleted for good."
              + (f" {res['skipped']} left — not yours to delete, or already "
                 f"billed." if res["skipped"] else ""), "success")
    else:
        flash("Nothing deleted.", "info")
    return redirect(url_for("timetracking.trash", team=team.pk))


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
    if action == "field":
        # Setting "Abrechnungsart" on the twenty entries you just filtered to is
        # the same edit twenty times; the per-row editor made that a chore.
        from application.services.timetracking import set_custom_value
        raw = (request.form.get("field_key") or "").strip()
        key, _, label = raw.partition("|")
        res = set_custom_value(team=team, pairs=pairs, key=key, label=label,
                               value=(request.form.get("field_value") or "").strip(),
                               viewer=user)
        if res["changed"]:
            flash(f"{label or key} set on {res['changed']} entr"
                  f"{'y' if res['changed'] == 1 else 'ies'}"
                  + (f"; {res['skipped']} skipped (their project doesn't have "
                     f"that field)" if res["skipped"] else "")
                  + (f"; {res['locked']} billed" if res["locked"] else "")
                  + ".", "success")
        else:
            flash("Nothing changed — the selected entries either have no such "
                  "field or are already billed.", "info")
        args = {k: v for k, v in request.form.items()
                if k in ("team", "period", "from", "to", "customer", "project",
                         "user", "billable", "locked", "state", "archived")}
        return redirect(url_for("timetracking.report", **args))
    if action == "move":
        # Re-filing time onto another project is settling the month, not
        # undoing it — a biller's call, same as billing it. Billed entries are
        # frozen where they were settled (``move_entries``), so this can't
        # rewrite what a customer already saw on an invoice.
        _billing_manager(team)
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
                         "user", "billable", "locked", "state", "archived")}
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
                     "user", "billable", "locked", "state", "archived")}
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
                     "user", "billable", "locked", "state", "archived")}
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
    excel = request.args.get("excel") == "1"
    if f["view"] == "month":
        from application.services.timetracking import monthly_breakdown
        header, rows = _monthly_table(monthly_breakdown(
            data["rows"], f["date_from"], f["date_to"]))
        # Every cell past Customer and Project is hours.
        decimal_at = set(range(2, len(header)))
    else:
        header, rows, cols = _export_table(data, _filter_project(f), f["merge"])
        # Which cells need the decimal comma is read off the columns, never
        # off fixed positions — a project's own column order moves them.
        decimal_at = {i for i, c in enumerate(cols) if c["key"] in DECIMAL_KEYS}

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";" if excel else ",")
    w.writerow(header)
    for row in rows:
        if excel:
            # Hours and amounts are numbers to Excel only with a decimal comma.
            row = [str(v).replace(".", ",") if i in decimal_at else v
                   for i, v in enumerate(row)]
        w.writerow(row)

    body = ("\ufeff" + buf.getvalue()) if excel else buf.getvalue()
    name = _export_name(team, f)
    # Both forms: the plain one for anything old, and RFC 5987 for the name as
    # it really reads. A header is latin-1 on the wire, so "Flughafen München"
    # would otherwise be a 500 rather than a download.
    fallback = name.encode("ascii", "replace").decode("ascii")
    return Response(body, mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": (f'attachment; filename="{fallback}"; '
                                f"filename*=UTF-8''{quote(name)}"),
    })


def _export_name(team, f) -> str:
    """``"2026-08 Flughafen München.csv"`` — the name the file is filed under.

    Month and customer, because that is what an export *is*: one client's month,
    the thing you attach to an invoice. Whoever downloads it should not have to
    rename it before saving. Without a customer filter the team stands in, and a
    range that isn't one month says so rather than pretending."""
    lo, hi = f["date_from"], f["date_to"]
    if (lo.year, lo.month) == (hi.year, hi.month):
        period = f"{lo:%Y-%m}"
    else:
        period = f"{lo:%Y-%m-%d}_{hi:%Y-%m-%d}"
    who = team.name
    if f["customer_id"]:
        c = Customer.objects(pk=to_oid(f["customer_id"]), team=team).first()
        if c is not None:
            who = c.name
    # Only the characters a filesystem (and a Content-Disposition header)
    # tolerate everywhere; umlauts are fine, slashes and quotes are not.
    who = re.sub(r'[\\/:*?"<>|\r\n]+', " ", who).strip() or "export"
    if f.get("view") == "month":
        who += " per month"
    return f"{period} {who}.csv"


# ---------------------------------------------------------------------------
# Migrating off mite
# ---------------------------------------------------------------------------

@timetracking_bp.route("/import")
@login_required
@team_required
def import_page():
    """The mite import on its own page — it is a one-off migration, not part of
    keeping customers and projects.

    Open to a biller, who sees only the CSV card: reading an exported sheet of
    hours back in is keeping the book, which is their job already (see
    :func:`_billing_manager`). Pulling a whole mite account — new customers,
    new projects, an API key — and wiping the team's books are not, and the
    template hides both from them."""
    team = _team()
    user = current_user._get_current_object()
    _billing_manager(team)
    return render_template("timetracking/import_mite.html", team=team,
                           all_teams=_user_teams(user), can_report=True,
                           can_manage=team.is_managed_by(user),
                           projects=_project_choices(team),
                           members=_member_choices(team))


@timetracking_bp.route("/reset", methods=["POST"])
@login_required
@team_required
def reset_billing():
    """Wipe the team's customers, projects and logged time, ready for a fresh
    import. Manager-only, and the team's name has to be typed out."""
    from application.services.timetracking import reset_team_billing

    team = _team()
    user = current_user._get_current_object()
    if not team.is_managed_by(user):
        abort(403)
    if (request.form.get("confirm") or "").strip() != team.name:
        flash(f"Type the team name exactly (“{team.name}”) to reset — nothing "
              "was deleted.", "warning")
        return redirect(url_for("timetracking.import_page", team=team.pk))
    res = reset_team_billing(team)
    flash(f"Time tracking reset: {res['customers']} customer(s), "
          f"{res['projects']} project(s) and {res['entries']} time entr"
          f"{'y' if res['entries'] == 1 else 'ies'} deleted"
          + (f"; {res['unlinked']} link(s) in your task tree cleared"
             if res["unlinked"] else "") + ". Import again to rebuild it.",
          "success")
    return redirect(url_for("timetracking.import_page", team=team.pk))


@timetracking_bp.route("/import/mite", methods=["POST"])
@login_required
@team_required
def import_mite():
    """Pull a mite account's customers, projects and time history into this team.

    Manager-only: it creates billing data for the whole team. The API key is
    used for this request and never stored. Runs inline — a big account is
    better served by ``cairn import-mite``, which reports as it goes."""
    from datetime import date as _date
    from application.services import sync_log
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
        sync_log.record("mite", team=team, user=user, error=str(exc))
        flash(str(exc), "danger")
        return redirect(url_for("timetracking.import_page", team=team.pk))
    sync_log.record("mite", team=team, user=user, stats=stats)

    summary = (f"{stats.customers_created} new customer(s), "
               f"{stats.projects_created} new project(s)")
    if stats.with_entries:
        summary += (f", {stats.entries_imported} time "
                    f"entr{'y' if stats.entries_imported == 1 else 'ies'}")
        if stats.entries_skipped:
            # Each of these means something different, and lumping them
            # together produced the memorable "487 already there" on an import
            # into an empty team.
            asides = []
            if stats.entries_skipped:
                asides.append(f"{stats.entries_skipped} already there")
            if stats.entries_empty:
                asides.append(f"{stats.entries_empty} with no time booked on them")
            if stats.entries_undated:
                asides.append(f"{stats.entries_undated} with an unreadable date")
            if stats.entries_updated:
                asides.append(f"{stats.entries_updated} updated from mite")
            if stats.entries_frozen:
                asides.append(f"{stats.entries_frozen} changed in mite but "
                              f"already billed here")
            if asides:
                summary += f" ({', '.join(asides)})"
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


@timetracking_bp.route("/import/csv", methods=["POST"])
@login_required
@team_required
def import_time_csv():
    """A mite CSV export, read into this team — optionally straight into one
    billing project.

    ``cairn import-time-csv`` on the page: the sheet a colleague exports for the
    days that never made it across. Pinning a project is what ``--project``
    does, and here it is the normal case — the file usually covers one project,
    and mite's per-person names ("Büro") rarely match Cairn's ("Büro Jasmin").
    Idempotent: a row is recognised by day, author, project, duration and note,
    so re-uploading the same file imports nothing twice. A biller may do this,
    not only a manager: these are hours on projects that already exist, which
    is the book they keep anyway (see :func:`_billing_manager`).
    """
    from application.services import time_csv
    from application.services.permissions import team_members
    from application.services.tz import user_zone

    team = _team()
    user = current_user._get_current_object()
    _billing_manager(team)

    project = None
    pid = (request.form.get("project") or "").strip()
    if pid:
        project = _project_or_404(pid, team)
    author = None
    uid = (request.form.get("user") or "").strip()
    if uid:
        author = next((m for m in team_members(team) if str(m.id) == uid), None)
        if author is None:
            abort(404)

    fileobj = request.files.get("file")
    if not fileobj or not fileobj.filename:
        flash("No file uploaded.", "danger")
        return _back(team)
    parsed = time_csv.parse(fileobj.read().decode("utf-8-sig", errors="replace"))
    if not parsed.rows:
        flash(parsed.errors[0] if parsed.errors
              else "Nothing to import — no readable row in that file.", "danger")
        return _back(team)

    dry_run = request.form.get("dry_run") == "1"
    stats = time_csv.import_rows(
        team=team, rows=parsed.rows, project=project, author=author,
        zone=user_zone(author or user), dry_run=dry_run)

    hours = f"{stats['minutes'] // 60}h {stats['minutes'] % 60}m"
    flash(f"{'Dry run — nothing written. Would import' if dry_run else 'Imported'}: "
          f"{stats['imported']} entr{'y' if stats['imported'] == 1 else 'ies'} "
          f"({hours})"
          + (f", {stats['duplicates']} already there" if stats["duplicates"] else "")
          + (f", {stats['failed']} without a project" if stats["failed"] else "")
          + (f" — into {project.customer.name} › {project.name}" if project else "")
          + ".", "success" if stats["imported"] else "warning")
    # The file's own unreadable lines and the rows no project could be found
    # for are two different problems, and both are the operator's to fix.
    for err in (parsed.errors + stats["errors"])[:5]:
        flash(err, "warning")
    if len(parsed.errors) + len(stats["errors"]) > 5:
        flash(f"…and {len(parsed.errors) + len(stats['errors']) - 5} more issues.",
              "warning")
    return _back(team)

# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

@timetracking_bp.route("/bulk-delete", methods=["POST"])
@login_required
@team_required
def bulk_delete():
    """Delete the ticked customers and projects in one go.

    Cleaning up after an import that produced the wrong shape means removing
    twenty things, and doing that one confirmation dialog at a time is how
    people give up halfway and leave a half-cleaned list. Same rights and same
    consequences as deleting them individually — manager-only, and everything
    booked on them goes with them — just not the same twenty clicks. A customer
    takes its projects along, so a project ticked under a ticked customer isn't
    counted twice."""
    from application.services.timetracking import delete_customer, delete_project

    team = _team()
    user = current_user._get_current_object()
    if not team.is_managed_by(user):
        abort(403)

    customer_ids, project_ids = [], []
    for raw in request.form.getlist("pick"):
        kind, _, ident = raw.partition(":")
        if kind == "customer" and ident:
            customer_ids.append(ident)
        elif kind == "project" and ident:
            project_ids.append(ident)
    if not customer_ids and not project_ids:
        flash("Nothing was ticked.", "info")
        return redirect(url_for("timetracking.index", team=team.pk,
                                **({"archive": 1} if request.form.get("archive") else {})))

    done = {"customers": 0, "projects": 0, "entries": 0}
    doomed = [c for c in Customer.objects(team=team, id__in=[
        to_oid(i) for i in customer_ids if to_oid(i)])]
    doomed_ids = {str(c.id) for c in doomed}
    for p in TimeTrackingProject.objects(team=team, id__in=[
            to_oid(i) for i in project_ids if to_oid(i)]):
        if str(p.customer.id) in doomed_ids:
            continue  # its customer is going anyway
        res = delete_project(p, purge=True)
        done["projects"] += 1
        done["entries"] += res.get("entries", 0)
    for c in doomed:
        res = delete_customer(c)
        done["customers"] += 1
        done["projects"] += res.get("projects", 0)
        done["entries"] += res.get("entries", 0)

    flash(f"Deleted {done['customers']} customer(s) and {done['projects']} "
          f"project(s) with {done['entries']} time entr"
          f"{'y' if done['entries'] == 1 else 'ies'}.", "success")
    return redirect(url_for("timetracking.index", team=team.pk,
                            **({"archive": 1} if request.form.get("archive") else {})))


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
    # Who this one could be merged into. Archived customers are offered too:
    # a duplicate is quite often archived out of the way instead of merged,
    # and that is exactly the record whose hours need to come back.
    others = list(Customer.objects(team=team, id__ne=c.id))
    # A duplicate is by definition a name that appears twice, so on the one
    # page where that matters the name alone cannot identify the target. Those
    # entries — and only those — carry what tells them apart: the key an
    # importer knows the record by, and how many projects hang under it.
    seen = {}
    for o in others + [c]:
        key = (o.name or "").strip().lower()
        seen[key] = seen.get(key, 0) + 1
    counts = {}
    if any(n > 1 for n in seen.values()):
        for p in TimeTrackingProject.objects(team=team).only(
                "customer").as_pymongo():
            cid = p.get("customer")
            counts[cid] = counts.get(cid, 0) + 1

    def _label(o):
        label = o.name + (" (archived)" if o.archived else "")
        if seen.get((o.name or "").strip().lower(), 0) < 2:
            return label
        n = counts.get(o.id, 0)
        marks = [f"{n} project{'' if n == 1 else 's'}"]
        if o.external_id:
            marks.append(o.external_id)
        return f"{label} — {' · '.join(marks)}"

    merge_targets = sorted(
        ({"id": str(o.id), "label": _label(o)} for o in others),
        key=lambda r: r["label"].lower())

    # What in the rest of Cairn points at this customer. A customer is not a
    # billing record with a name on it any more: it is the thing folders,
    # projects and meetings are filed under, so the page has to say which.
    from application.services.containers import describe_containers
    bound = list(Item.objects(Q(customer=c.id)
                              | Q(time_project__in=[p.id for p in projects]),
                              kind__in=["project", "folder"])
                 .only("id", "title", "kind", "parent", "owner", "shared_teams",
                       "time_project", "customer"))
    bound_info = describe_containers(bound, team=team)
    bound.sort(key=lambda i: (bound_info.get(str(i.pk), {}).get("path") or "").lower())

    # Meetings booked by this customer's people. Upcoming first, then the recent
    # past — which is the half you bill from, so it belongs on the page rather
    # than only in the bookings list.
    from application.models.booking import Booking
    now = datetime.now(timezone.utc)
    booked = list(Booking.objects(customer=c.id).order_by("-start").limit(50))
    meetings = sorted((b for b in booked if b.start >= now),
                      key=lambda b: b.start) + \
        sorted((b for b in booked if b.start < now),
               key=lambda b: b.start, reverse=True)

    from application.services.customers import is_freemail
    return render_template(
        "timetracking/customer.html", team=team, all_teams=_user_teams(user),
        can_manage=team.is_managed_by(user), can_file=can_bill(user, team),
        can_import=can_bill(user, team), can_report=can_report(user, team),
        customer=c, projects=projects,
        minutes_by_project=minutes_by_project,
        merge_targets=merge_targets,
        bound=bound, bound_info=bound_info,
        meetings=meetings, now=now,
        # Named so the page can say which of the typed domains will never be
        # matched on, instead of leaving it to be discovered on a wrong invoice.
        freemail={d for d in (c.email_domains or []) if is_freemail(d)},
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
    # ``archived`` is deliberately not read here: it is a statement about the
    # book, not a detail of the customer, so it has its own route and its own
    # permission (customer_archive).

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
    # The mail domains that identify this customer's people — what an inbound
    # appointment is matched on. Parsed rather than stored as typed (commas,
    # spaces, a whole address pasted in), because a domain that never matches
    # is a field that silently does nothing.
    if "email_domains" in request.form:
        from application.services.customers import parse_domains
        c.email_domains = parse_domains(request.form.get("email_domains"))
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


@timetracking_bp.route("/customers/<cid>/archive", methods=["POST"])
@login_required
@team_required
def customer_archive(cid):
    """Archive a customer, or bring one back. Reversible, hence one click.

    Archived means "out of the way", not "gone": the history stays, still
    bills, and still appears in a report that asks for it. What changes is that
    the customer *and every project under it* drop out of the pickers — see
    ``archived_project_ids``, where archiving a customer is the broader of the
    two statements. The same move a billing project offers, one level up."""
    team = _team()
    _billing_manager(team)
    c = _customer_or_404(cid, team)
    c.archived = request.form.get("archived") == "1"
    c.save()
    flash(f"“{c.name}” {'archived' if c.archived else 'is active again'}.",
          "success")
    return _back(team)


@timetracking_bp.route("/projects/<pid>/archive", methods=["POST"])
@login_required
@team_required
def project_archive(pid):
    """Archive a billing project, or bring one back — see customer_archive."""
    team = _team()
    _billing_manager(team)
    p = _project_or_404(pid, team)
    p.archived = request.form.get("archived") == "1"
    p.save()
    flash(f"“{p.name}” {'archived' if p.archived else 'is active again'}.",
          "success")
    return _back(team)


@timetracking_bp.route("/customers/<cid>/merge", methods=["POST"])
@login_required
@team_required
def customer_merge(cid):
    """Fold this customer into another one and delete it.

    The way out of the state an importer leaves behind when it met a client it
    already had under a key it didn't recognise: one company, two files, the
    hours split across both. Manager only and the name typed out, like every
    other path here that a customer cannot be un-done from — the time is not
    destroyed (it moves), but the record it hung under is."""
    from application.services.timetracking import merge_customers

    team = _team()
    user = current_user._get_current_object()
    _billing_manager(team)
    src = _customer_or_404(cid, team)
    if not (request.form.get("target_id") or "").strip():
        flash("Pick the customer to merge into.", "warning")
        return redirect(url_for("timetracking.customer_detail", cid=str(src.id),
                                team=team.pk))
    target = _customer_or_404(request.form.get("target_id"), team)
    if str(target.id) == str(src.id):
        flash("Pick a different customer to merge into.", "warning")
        return redirect(url_for("timetracking.customer_detail", cid=str(src.id),
                                team=team.pk))
    if (request.form.get("confirm") or "").strip() != src.name:
        flash(f"Type the customer name exactly (“{src.name}”) to merge it — "
              "nothing was changed.", "warning")
        return redirect(url_for("timetracking.customer_detail", cid=str(src.id),
                                team=team.pk))
    name = src.name
    res = merge_customers(src, target)
    msg = (f"Merged “{name}” into “{target.name}”: "
           f"{res['entries_moved']} time entr"
           f"{'y' if res['entries_moved'] == 1 else 'ies'} and "
           f"{res['projects_moved'] + res['projects_folded']} project(s) moved")
    if res["projects_folded"]:
        msg += (f" ({res['projects_folded']} folded into a project of the same "
                f"name)")
    msg += "."
    if res["duplicates_cancelled"]:
        msg += (f" {res['duplicates_cancelled']} entr"
                f"{'y was' if res['duplicates_cancelled'] == 1 else 'ies were'} "
                f"the same booking imported under both customers and arrived "
                f"cancelled — see the trash.")
    if res["duplicates_kept"]:
        msg += (f" {res['duplicates_kept']} duplicate(s) were already billed "
                f"and were left active — check them.")
    flash(msg, "success")
    return redirect(url_for("timetracking.customer_detail", cid=str(target.id),
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

@timetracking_bp.route("/projects/<pid>")
@login_required
@team_required
def project_detail(pid):
    """One billing project on its own page — everything that used to be a
    <details> on the management list, where it was rendered for every project
    whether or not anyone opened it."""
    from application.services.containers import describe_containers
    from application.services.timetracking import budget_status, team_consumption
    from application.services.tz import user_zone

    team = _team()
    user = current_user._get_current_object()
    p = _project_or_404(pid, team)

    linked = list(Item.objects(time_project=p, kind__in=["project", "folder"]).only(
        "id", "title", "kind", "parent", "owner", "shared_teams", "time_project"))
    link_info = describe_containers(linked, team=team)
    # Which of those rows the viewer may walk into — the binding is changed on
    # the container itself, and a colleague's private folder is named here but
    # not opened. Read off the raw reference rather than ``it.owner``, which
    # would be a dereference per row for one link.
    uid = str(user.pk)
    link_open = {str(i.pk) for i in linked
                 if str(getattr(i._data.get("owner"), "pk", "")
                        or getattr(i._data.get("owner"), "id", "")) == uid
                 or link_info.get(str(i.pk), {}).get("shared")}
    today = datetime.now(user_zone(user)).date()
    consumption = team_consumption(team, f"{today.year:04d}-{today.month:02d}")
    move_targets = sorted(
        ({"id": str(o.id), "label": f"{o.customer.name} › {o.name}"}
         for o in TimeTrackingProject.objects(team=team, archived=False)),
        key=lambda r: r["label"].lower())
    # The customer sheet's columns, in this project's order, each flagged with
    # whether it is currently shown — the form below is the whole configuration.
    from application.services.timetracking import (
        available_sheet_columns, sheet_columns)
    all_cols = available_sheet_columns(p)
    chosen = [k for k in (p.share_columns or []) if k in {c["key"] for c in all_cols}]
    ordered = ([c for k in chosen for c in all_cols if c["key"] == k]
               + [c for c in all_cols if c["key"] not in chosen])
    # With nothing configured, tick what the *default* sheet actually shows —
    # ticking everything would claim the ticket and the service have columns
    # when the default folds them into the description.
    fallback = {c["key"] for c in
                sheet_columns(p, has_times=(p.time_mode == "clock"))}
    share_cols = [{**c, "on": (c["key"] in chosen) if chosen
                   else (c["key"] in fallback)}
                  for c in ordered]

    return render_template(
        "timetracking/project.html", team=team, all_teams=_user_teams(user),
        can_manage=team.is_managed_by(user), can_file=can_bill(user, team),
        can_import=can_bill(user, team), can_report=can_report(user, team),
        p=p, share_cols=share_cols,
        budget=budget_status(p, consumption.get(str(p.id))),
        # Two different numbers, and the danger zone needs the other one: the
        # header reports the month, moving or purging takes everything.
        logged=consumption.get(str(p.id), {}).get("month_minutes", 0),
        logged_total=consumption.get(str(p.id), {}).get("minutes", 0),
        links=linked, link_info=link_info, link_open=link_open,
        members=_member_choices(team) if can_bill(user, team) else [],
        move_targets=move_targets)


@timetracking_bp.route("/projects", methods=["POST"])
@login_required
@team_required
def project_create():
    team = _team()
    name = (request.form.get("name") or "").strip()
    customer = _customer_or_404(request.form.get("customer_id"), team)
    if not name:
        flash("Project name is required.", "warning")
        return _back(team)
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
    # The overview posts without ``back`` and lands on the overview; the
    # customer's own page sends you back to the customer you were looking at.
    return _back(team)


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
    # See customer_edit: archiving has its own route and permission.
    p.save()
    # One intake project per customer, so switching it on has to switch it off
    # elsewhere — hence the service call rather than a plain field assignment.
    # The customer sheet's columns: every key comes back with a position, only
    # the ticked ones are kept, and the order is theirs. An empty selection
    # means "the default set" rather than an empty sheet — a customer link
    # showing nothing at all is never what someone meant.
    if request.form.getlist("share_col_key"):
        # The order is the order the rows arrive in — same-named fields are
        # submitted in document order, so dragging a row *is* the edit and
        # there is no position to keep in sync with it.
        picked = set(request.form.getlist("share_col"))
        p.share_columns = [k for k in request.form.getlist("share_col_key")
                           if k in picked]
        p.save()
    set_intake_project(p, request.form.get("intake") == "1")
    flash("Project updated.", "success")
    return _back(team)


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
        return _back(team)
    stats = purge_project_entries(p)
    if stats["entries"]:
        flash(f"Reset “{p.name}”: {stats['entries']} entr"
              f"{'y' if stats['entries'] == 1 else 'ies'} deleted "
              f"({stats['minutes'] // 60}h {stats['minutes'] % 60}m"
              + (f", {stats['locked']} of them already billed" if stats["locked"] else "")
              + "). The project itself is untouched.", "success")
    else:
        flash(f"“{p.name}” had no time logged on it.", "info")
    return _back(team)


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
    _billing_manager(team)
    src = _project_or_404(pid, team)
    target = _project_or_404(request.form.get("target_id"), team)
    if str(target.id) == str(src.id):
        flash("Pick a different project to move the time to.", "warning")
        return _back(team)
    pairs = project_entries_for_move(src)
    if not pairs:
        flash(f"“{src.name}” has no time logged on it.", "info")
        return _back(team)
    res = move_entries(team=team, pairs=pairs, target=target, actor=user)
    msg = (f"Moved {res['moved']} entr{'y' if res['moved'] == 1 else 'ies'} "
           f"from “{src.name}” to “{target.name}”.")
    if res["locked"]:
        msg += (f" {res['locked']} billed entr"
                f"{'y' if res['locked'] == 1 else 'ies'} stayed — reopen them "
                f"first if they should move too.")
    flash(msg, "success")
    return _back(team)


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
        return _back(team)
    name = p.name
    res = delete_project(p, purge=request.form.get("purge") == "1")
    if not res["deleted"]:
        flash(f"“{name}” still has {res['entries']} entr"
              f"{'y' if res['entries'] == 1 else 'ies'} on it. Move them to "
              f"another project first, or tick “delete the time too”.",
              "warning")
        return _back(team)
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
    return _back(team)


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
    only exists so the URL isn't a dead end when someone lands on it.

    **No page posts here any more.** The binding is set on the container, in its
    own inspector (Customer + Billing project); the billing project's page used
    to ask the same question from the other end, which read as a second
    mechanism for one link. Kept because it is one of the paths
    ``Item._follow_billing_customer`` exists for and is pinned by a test — and
    because putting the button back is then a template change, not a rewrite."""
    team = _team()
    user = current_user._get_current_object()
    if request.method == "GET":
        return _back(team)
    item_id = to_oid(request.form.get("item_id"))
    if item_id is None:
        # The picker's placeholder option — nothing was chosen.
        flash("Pick a folder or project to link.", "warning")
        return _back(team)
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
    return _back(team)
