"""Item views — perspectives, CRUD, comments, assignments."""
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for,
)

def _is_inspector():
    return request.args.get("inspector") == "1" or request.headers.get("X-Inspector-Request") == "1"

def _apply_smart_tokens(task: "Item", raw_title: str, user) -> None:
    """Merge Todoist-style tokens parsed out of ``raw_title`` into ``task``.

    Form fields the user filled explicitly win — the parser only fills
    gaps. Recognised tokens (@tag, #project, pN, !, dates) are stripped
    from the stored title.
    """
    from application.services.quick_parse import parse_task_text

    parsed = parse_task_text(raw_title, user, default_project=task.project)
    if parsed.title == raw_title.strip() and not parsed.tags and not parsed.unresolved_tags:
        # No tokens were present — leave everything alone, including title.
        return

    task.title = parsed.title or raw_title.strip()
    if parsed.project is not None and parsed.project != task.project:
        task.project = parsed.project
    if parsed.tags:
        merged = list(task.tags or [])
        existing = {n.lower() for n in merged}
        for tag in parsed.tags:
            if tag.name.lower() not in existing:
                merged.append(tag.name)
                existing.add(tag.name.lower())
        task.tags = merged
    if parsed.priority and not task.priority:
        task.priority = parsed.priority
    if parsed.due_date and not task.due_date:
        task.due_date = parsed.due_date
    if parsed.flagged:
        task.flagged = True

def _render_empty_task_inspector(user, *, parent_project=None, just_added_title=None):
    """Reset the inspector to a fresh empty new-task form.

    Used after "Add another" so the user can rapid-fire create several
    tasks without leaving the panel.
    """
    form = TaskForm()
    _populate_form_choices(form, user)
    if parent_project is not None:
        form.project.data = str(parent_project.pk)
        form.status.data = "next"
    form.add_another.data = True
    return render_template(
        "tasks/_inspector.html",
        form=form,
        task=None,
        form_action=url_for("tasks.create"),
        just_added_title=just_added_title,
    )

def _build_task_form(task, user):
    """Build a ``TaskForm`` pre-populated with ``task``'s current state.
    Shared by the offcanvas inspector and the inline inspector on the
    task detail page."""
    from application.services.tz import localise_form_dates
    form = TaskForm(obj=task)
    localise_form_dates(form, user)
    _populate_form_choices(form, user)
    form.project.data = str(task.project.pk) if task.project else ""
    form.tags.data = list(task.tags or [])
    form.assigned_to.data = str(task.assigned_to.pk) if task.assigned_to else ""
    form.priority.data = str(task.priority) if task.priority else ""
    return form

def _render_task_inspector(task, user, *, just_saved=False):
    """Render the inspector fragment pre-populated with ``task``'s state."""
    return render_template(
        "tasks/_inspector.html",
        form=_build_task_form(task, user),
        task=task,
        form_action=url_for("tasks.edit", task_id=str(task.pk)),
        just_saved=just_saved,
    )
from flask_login import current_user, login_required
from mongoengine import Q

from application.forms import CommentForm, TaskForm

from application.models.user import User
from application.models.item import ChecklistItem, Item, NoteImage

tasks_bp = Blueprint("tasks", __name__, url_prefix="/tasks")


@tasks_bp.route("/<task_id>/toggle-collapse", methods=["POST"])
@login_required
def toggle_collapse(task_id):
    """Persist a per-user collapse toggle for a task / project row."""
    from flask import jsonify
    user = current_user._get_current_object()
    item = Item.objects(pk=task_id).first()
    if not item or not item.can_be_viewed_by(user):
        return jsonify({"error": "not found"}), 404
    pk_str = str(item.pk)
    current = list(user.collapsed_items or [])
    if pk_str in current:
        current.remove(pk_str)
        collapsed = False
    else:
        current.append(pk_str)
        collapsed = True
    user.collapsed_items = current
    user.save()
    return jsonify({"ok": True, "collapsed": collapsed})

def _scope(user):
    return Q(owner=user) | Q(assigned_to=user)

def _writeback_calendar(task, user) -> list[str]:
    """Push plan/due dates to the user's chosen Google calendar.

    Best-effort: surfaces warnings as flashes for the page render AND
    returns them so JSON endpoints (e.g. /tasks/<id>/schedule) can echo
    them back to the caller. Failures never break the task save.
    """
    from application.services.calendar_writeback import sync_task_to_calendar
    warnings = sync_task_to_calendar(task, user) or []
    for warn in warnings:
        flash(warn, "warning")
    return warnings

def _tag_choices_tree(tags) -> list:
    """Return ``(name, label)`` choices in DFS order with indentation.

    Parent tags appear before their children; children are prefixed with
    a visible bullet so the hierarchy reads at a glance in a flat
    ``<select multiple>``. Children of mutually-exclusive parents get an
    additional marker.
    """
    by_parent: dict = {}
    by_name = {t.name: t for t in tags}
    for t in tags:
        by_parent.setdefault(t.parent or None, []).append(t)
    out: list = []

    def walk(parent_name, depth):
        for t in sorted(by_parent.get(parent_name, []), key=lambda x: x.name.lower()):
            indent = "    " * depth
            marker = "└─ " if depth > 0 else ""
            parent_tag = by_name.get(t.parent) if t.parent else None
            excl = " ◉" if parent_tag and parent_tag.exclusive_children else ""
            label = f"{indent}{marker}{t}{excl}"
            out.append((t.name, label))
            walk(t.name, depth + 1)

    walk(None, 0)
    return out

def _populate_form_choices(form: TaskForm, user: User) -> None:
    # Project picker only lists real projects (kind="project"). Folders
    # are organisational; sections live inline in a project and aren't
    # task containers.
    projects = Item.objects(
        Q(owner=user) | Q(members=user),
        kind="project", status="active",
    ).order_by("+name")
    form.project.choices = [("", "—")] + [(str(p.pk), p.name) for p in projects]
    # Tag dropdown choices use the tag NAME both as value and label.
    # Render in tree order with indentation so nested tags are obvious.
    form.tags.choices = _tag_choices_tree(user.tags or [])

    # collaborators = me + members of any project I share/own
    collab_ids = {str(user.pk)}
    for p in Item.objects(Q(owner=user) | Q(members=user)):
        for m in (p.members or []):
            collab_ids.add(str(m.pk))
        collab_ids.add(str(p.owner.pk))
    users = User.objects(pk__in=list(collab_ids))
    form.assigned_to.choices = [("", "— unassigned —")] + [
        (str(u.pk), str(u)) for u in users
    ]

def _apply_form(form: TaskForm, task: Item, user: User) -> None:
    from application.services.tz import attach_user_tz
    task.title = form.title.data
    task.note = form.note.data or ""
    task.status = form.status.data
    task.flagged = form.flagged.data
    task.is_internal = form.is_internal.data
    task.priority = int(form.priority.data) if form.priority.data else None
    # ``datetime-local`` inputs come in as naive wall-clock values in the
    # user's local time — attach their tz so mongoengine doesn't treat
    # them as UTC and shift everything by the user's offset.
    task.defer_date = attach_user_tz(form.defer_date.data, user)
    task.due_date = attach_user_tz(form.due_date.data, user)
    task.plan_date = attach_user_tz(form.plan_date.data, user)
    task.estimated_minutes = form.estimated_minutes.data
    task.repeat_every_days = form.repeat_every_days.data
    task.repeat_unit = form.repeat_unit.data or "day"
    task.repeat_from_completion = form.repeat_from_completion.data
    # ``form.project.data`` should be a project pk string from the
    # select. Use the shared visibility helper so a tampered POST can't
    # reparent into a project the user doesn't own/share. Invalid /
    # inaccessible pks fall back to "no project" — same lenient
    # behaviour the form had before, but without the access leak.
    from application.services.permissions import resolve_visible_item
    task.project = resolve_visible_item(user, form.project.data) if form.project.data else None
    # Tags are stored as a list of names; the multi-select submits names.
    submitted = list(form.tags.data or [])
    # Enforce exclusive-children groups: at most one child tag from a
    # parent flagged as exclusive_children.
    final, seen_excl = [], set()
    by_name = {t.name: t for t in (user.tags or [])}
    for name in submitted:
        tag = by_name.get(name)
        if tag and tag.parent:
            parent_tag = by_name.get(tag.parent)
            if parent_tag and parent_tag.exclusive_children:
                if tag.parent in seen_excl:
                    continue  # already picked one from this exclusive group
                seen_excl.add(tag.parent)
        final.append(name)
    task.tags = final

    # Label-driven status: if any of the assigned tags carries a
    # ``maps_to_status`` setting, snap the task to that status. The
    # user form supplies an explicit status above, but the label rule
    # is authoritative — if the user adds "Warte auf Kunde" the task
    # should become waiting regardless of what the dropdown said.
    from application.services.label_rules import apply_label_rules
    apply_label_rules(task, user)

    if form.assigned_to.data:
        from application.services.permissions import resolve_assignable_user
        new_assignee = resolve_assignable_user(user, form.assigned_to.data)
        if new_assignee and task.assigned_to != new_assignee:
            task.assign_to(new_assignee, by_user=user)
        # Silently ignore non-shareable pks — the select choices are
        # already filtered, so reaching this branch with a bad id means
        # someone tampered with the POST.
    else:
        task.assign_to(None)

# ---------- perspectives ----------

@tasks_bp.route("/inbox/process", defaults={"task_id": None})
@tasks_bp.route("/inbox/process/<task_id>")
@login_required
def process_inbox(task_id):
    """OmniFocus-style "Process Inbox" — walk through each loose task with
    the full edit form, then jump to the next one. Skip moves on without
    saving; Save moves on after persisting. ``Done`` lands on Home.
    """
    user = current_user._get_current_object()

    def queue():
        return list(
            Item.objects(_scope(user), kind="task", parent=None,
                         status__nin=["done", "dropped"])
            .order_by("-created_at")
        )

    q = queue()
    if not q:
        flash("Inbox empty — well done.", "success")
        return redirect(url_for("dashboard.home"))

    if task_id is None:
        return redirect(url_for("tasks.process_inbox", task_id=str(q[0].pk)))

    task = next((t for t in q if str(t.pk) == task_id), None)
    if task is None:
        # The current task got moved out of the inbox; pick the next one
        # in the queue automatically so the user keeps flowing.
        return redirect(url_for("tasks.process_inbox", task_id=str(q[0].pk)))

    idx = q.index(task)
    nxt = q[idx + 1] if idx + 1 < len(q) else None

    form = TaskForm(obj=task)
    _populate_form_choices(form, user)
    if request.method == "GET":
        from application.services.tz import localise_form_dates
        localise_form_dates(form, user)
        form.project.data = str(task.project.pk) if task.project else ""
        form.tags.data = list(task.tags or [])
        form.assigned_to.data = str(task.assigned_to.pk) if task.assigned_to else ""
        form.priority.data = str(task.priority) if task.priority else ""

    return render_template(
        "tasks/process_inbox.html",
        form=form, task=task, queue=q, idx=idx, next_task=nxt,
    )


@tasks_bp.route("/inbox/process/<task_id>/save", methods=["POST"])
@login_required
def process_inbox_save(task_id):
    """Save the edit form, then bounce to the next inbox task."""
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    form = TaskForm()
    _populate_form_choices(form, user)
    if not form.validate_on_submit():
        flash("Couldn't save — please review the form.", "danger")
        return redirect(url_for("tasks.process_inbox", task_id=task_id))
    _apply_form(form, task, user)
    _apply_smart_tokens(task, form.title.data, user)
    task.save()
    _writeback_calendar(task, user)
    # Recompute the queue *after* save — if the user picked a project,
    # the task has left the inbox, so the next ID we land on is what's
    # left first.
    remaining = list(
        Item.objects(_scope(user), kind="task", parent=None,
                     status__nin=["done", "dropped"])
        .order_by("-created_at")
    )
    if not remaining:
        flash("Inbox cleared — nice work.", "success")
        return redirect(url_for("dashboard.home"))
    return redirect(url_for("tasks.process_inbox", task_id=str(remaining[0].pk)))


def _scoped_project_pks(user, project_id: str | None) -> list | None:
    """Helper: if ``?project=<pk>`` was passed, walk the chain and
    return every descendant pk so a perspective can be filtered to
    "this project + everything nested under it". Returns ``None`` when
    no filter is requested. ``"inbox"`` means parent=None.
    """
    if not project_id:
        return None
    if project_id == "inbox":
        return ["inbox"]
    root = Item.objects(pk=project_id).first()
    if not root or not root.can_be_viewed_by(user):
        return []
    out = [root.pk]
    frontier = [root.pk]
    while frontier:
        kids = list(Item.objects(parent__in=frontier).only("pk"))
        if not kids:
            break
        new_pks = [k.pk for k in kids]
        out.extend(new_pks)
        frontier = new_pks
    return out


def _filter_by_project(qs, user, *, mode="tasks"):
    """Apply ``?project=<id>`` filter to a perspective queryset.

    ``mode='tasks'`` filters Item kind=task to those whose parent is
    in the requested project's subtree.
    """
    pids = _scoped_project_pks(user, (request.args.get("project") or "").strip())
    if pids is None:
        return qs
    if pids == ["inbox"]:
        return qs.filter(parent=None)
    if not pids:
        return qs.none()
    return qs.filter(parent__in=pids)


@tasks_bp.route("/inbox")
@login_required
def inbox():
    """Virtual default project — every task without an explicit project."""
    user = current_user._get_current_object()
    tasks = (Item.objects(_scope(user),
                          kind="task",
                          parent=None,
                          status__nin=["done", "dropped"])
             .order_by("-created_at"))
    return render_template(
        "tasks/list.html", title="Inbox", perspective="inbox", tasks=tasks
    )

@tasks_bp.route("/today")
@login_required
def today():
    """Today's plate.

    By default shows the user's "now": overdue + due today + flagged +
    planned-for-today + next-with-defer-hit. Accepts ``?date=YYYY-MM-DD``
    + ``?days=N`` to navigate to another window (yesterday, tomorrow,
    next week, …); in multi-day mode the filter switches to "tasks with
    a due/plan date inside the window".
    """
    from datetime import date as _date

    user = current_user._get_current_object()
    now = datetime.now(timezone.utc)
    # "Today" must be computed in the *user's* timezone, otherwise a
    # Berlin user dragging a task to the cell labelled "tomorrow"
    # writes plan_date for what the server thinks is "today" once the
    # UTC offset pushes the wall-clock past midnight.
    try:
        from zoneinfo import ZoneInfo
        _today_tz = ZoneInfo(user.timezone or current_app.config.get("DEFAULT_TZ") or "UTC")
    except Exception:
        _today_tz = timezone.utc
    today_date = now.astimezone(_today_tz).date()

    # Window selection ------------------------------------------------
    raw_date = (request.args.get("date") or "").strip()
    try:
        start_date = (
            _date.fromisoformat(raw_date) if raw_date else today_date
        )
    except ValueError:
        start_date = today_date
    try:
        days = int(request.args.get("days") or "1")
    except (TypeError, ValueError):
        days = 1
    days = max(1, min(days, 31))

    # Bucket the day window in the user's tz, otherwise a task planned
    # for 23:00 Berlin (= 21:00 UTC) on day D would fall outside a
    # ``date(D)`` UTC-midnight window — the row showing day D would miss
    # its own task and it would re-appear on day D+1.
    try:
        from zoneinfo import ZoneInfo
        _u_tz = ZoneInfo(user.timezone or current_app.config.get("DEFAULT_TZ") or "UTC")
    except Exception:
        _u_tz = timezone.utc
    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=_u_tz)
    end_dt = start_dt + timedelta(days=days)
    is_today_now = (start_date == today_date and days == 1)

    # Tasks -----------------------------------------------------------
    qs = (
        Item.objects(
            _scope(user),
            status__nin=["done", "dropped", "someday"],
        )
        .order_by("+due_date", "-flagged")
    )

    # Optional single-tag filter (?tag=<pk> persists onto the user)
    tag_param = request.args.get("tag")
    if tag_param is not None:
        user.today_filter_tag = tag_param.strip() or ""
        user.save()
    # today_filter_tag now holds the tag NAME (post-refactor).
    active_tag = user.today_filter_tag or ""
    if active_tag and user.tag_by_name(active_tag):
        qs = qs.filter(tags=active_tag)

    # Pull in any project (incl. section) carrying a relevant date so
    # milestones land in Today alongside tasks. Filtered by the same
    # window logic.
    proj_qs = Item.objects(
        owner=user,
        kind__ne="folder",
        status__nin=["completed", "dropped"],
    )
    if active_tag and user.tag_by_name(active_tag):
        proj_qs = proj_qs.filter(tags=active_tag)

    if is_today_now:
        # "Today" = due/overdue at any time through 23:59 today, plus
        # flagged + planned-for-today + just-unblocked. Using
        # ``now + 24h`` here would scoop up tomorrow-morning items
        # (anything due within 24h of "now"), which isn't what Today
        # means in a GTD sense.
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        filtered = [
            t for t in qs
            if (t.due_date and t.due_date <= today_end)
            or t.flagged
            or (t.plan_date and t.plan_date.date() <= today_date)
            or (t.defer_date and t.defer_date <= now and t.status == "next")
        ]
        projects_with_dates = [
            p for p in proj_qs
            if (p.due_date and p.due_date <= today_end)
            or (p.plan_date and p.plan_date.date() <= today_date)
        ]
    else:
        filtered = [
            t for t in qs
            if (t.due_date and start_dt <= t.due_date < end_dt)
            or (t.plan_date and start_dt <= t.plan_date < end_dt)
        ]
        projects_with_dates = [
            p for p in proj_qs
            if (p.due_date and start_dt <= p.due_date < end_dt)
            or (p.plan_date and start_dt <= p.plan_date < end_dt)
        ]

    # Grouping — same options as the project detail view; persisted on the user.
    grouping = (request.args.get("group") or user.today_grouping or "project").strip()
    if grouping not in ("none", "status", "priority", "tag", "due", "project"):
        grouping = "project"
    if grouping != user.today_grouping:
        user.today_grouping = grouping
        user.save()

    from application.views.projects import _group_tasks
    # In every grouping mode except "project" itself, projects-with-dates
    # need to flow into the chosen grouping (priority / status / tag /
    # due) alongside tasks — the user expects "P1" to include both a
    # P1 task and a P1 project, not a separate "Projects" block.
    #
    # ``filtered`` already includes any project whose own due/plan date
    # matched today, so we de-dupe against pk when merging — otherwise
    # the same project would render once via its task path and once via
    # the projects_with_dates path.
    def _merge_unique(*lists):
        seen, out = set(), []
        for lst in lists:
            for it in lst:
                if it.pk in seen:
                    continue
                seen.add(it.pk)
                out.append(it)
        return out

    if grouping == "project":
        groups = _group_by_project(filtered, projects_with_dates)
    elif grouping == "none":
        merged = _merge_unique(filtered, projects_with_dates)
        groups = [("Today", "bi-calendar-day", merged, None)] if merged else []
    else:
        merged = _merge_unique(filtered, projects_with_dates)
        groups = _group_tasks(merged, grouping)

    # Per-project header chips (due/plan dates) — keyed by str(pk) so
    # the template can look them up while iterating ``groups`` in
    # project-grouping mode without an extra query.
    proj_header_info = {
        str(p.pk): {
            "due_date": p.due_date,
            "plan_date": p.plan_date,
        }
        for p in projects_with_dates
    }

    # Tag list for the filter dropdown.
    all_tags = sorted(user.tags or [], key=lambda t: t.name.lower())

    # External calendar events ---------------------------------------
    from application.services.calendar_sync import (
        collect_next_event, fetch_events,
    )
    events = []
    next_event = None
    fetch_since = start_dt - timedelta(hours=6) if is_today_now else start_dt
    fetch_until = end_dt + (timedelta(hours=6) if is_today_now else timedelta(0))
    for cal in (user.calendars or []):
        if not cal.show_in_today:
            continue
        try:
            events.extend(fetch_events(cal, fetch_since, fetch_until, user=user))
        except Exception as exc:  # noqa: BLE001
            flash(f"{cal.name}: {exc}", "warning")
    events.sort(key=lambda e: e.start)
    if not events and is_today_now:
        try:
            next_event = collect_next_event(user)
        except Exception:
            next_event = None

    # Group events by date when window spans more than one day, so the
    # template can render section headers per day.
    events_by_day = {}
    if days > 1:
        for ev in events:
            key = ev.start.date()
            events_by_day.setdefault(key, []).append(ev)

    # Multi-day LIST view: mix events and tasks into one list per day,
    # sorted by start time. Lets the user see "Tuesday: 09:00 standup,
    # 10:30 do bug fix, 14:00 dentist" without flipping to calendar
    # mode. Skipped for single-day windows (the existing groupings are
    # plenty) and for calendar mode (the timeline already interleaves).
    daily_buckets = None
    if days > 1:
        try:
            from zoneinfo import ZoneInfo
            _list_tz = ZoneInfo(user.timezone or current_app.config.get("DEFAULT_TZ") or "UTC")
        except Exception:
            _list_tz = timezone.utc

        def _to_user_list(dt):
            if not dt:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(_list_tz)

        from datetime import date as _date2
        # OrderedDict so the template iterates days in window order.
        from collections import OrderedDict
        daily_buckets = OrderedDict(
            (start_date + timedelta(days=i), [])
            for i in range(days)
        )
        for ev in events:
            d = _to_user_list(ev.start)
            if not d:
                continue
            day_key = d.date()
            if day_key in daily_buckets:
                daily_buckets[day_key].append({
                    "kind": "event", "obj": ev,
                    "sort": (d, 0, ev.title or ""),
                    "start": d,
                })
        # Filtered already contains the items in the date window.
        # Sort each task into the day matching its plan_date.date()
        # falling back to due_date.date(). Items with no date land in
        # the first bucket (today) so they're never dropped silently.
        for t in filtered:
            anchor = _to_user_list(t.plan_date or t.due_date)
            day_key = anchor.date() if anchor else start_date
            if day_key not in daily_buckets:
                continue
            sort_dt = anchor or datetime.combine(day_key, datetime.min.time(), tzinfo=_list_tz)
            daily_buckets[day_key].append({
                "kind": "task", "obj": t,
                "sort": (sort_dt, 1, t.title or ""),
                "start": anchor,
            })
        for p in projects_with_dates:
            anchor = _to_user_list(p.plan_date or p.due_date)
            day_key = anchor.date() if anchor else start_date
            if day_key not in daily_buckets:
                continue
            sort_dt = anchor or datetime.combine(day_key, datetime.min.time(), tzinfo=_list_tz)
            daily_buckets[day_key].append({
                "kind": "project", "obj": p,
                "sort": (sort_dt, 1, p.title or ""),
                "start": anchor,
            })
        for d in daily_buckets:
            daily_buckets[d].sort(key=lambda x: x["sort"])

    # Title + nav helpers --------------------------------------------
    if is_today_now:
        title = "Today"
    elif days == 1 and start_date == today_date + timedelta(days=1):
        title = "Tomorrow"
    elif days == 1 and start_date == today_date - timedelta(days=1):
        title = "Yesterday"
    elif days == 1:
        title = start_date.strftime("%A %-d %b")
    else:
        end_label = (end_dt - timedelta(days=1)).date()
        title = f"{start_date.strftime('%-d %b')} – {end_label.strftime('%-d %b')}"

    prev_date = (start_date - timedelta(days=days)).isoformat()
    next_date = (start_date + timedelta(days=days)).isoformat()

    # ?view=calendar — timeline layout. Tasks are merged with events
    # so the user sees scheduled work next to meetings. Only 1-day
    # windows get the grid; multi-day stays on the list mode.
    view_mode = (request.args.get("view") or "list").strip()
    if view_mode not in ("list", "calendar"):
        view_mode = "list"

    timeline = None
    if view_mode == "calendar":
        # Bucket events/tasks in the user's configured timezone so
        # they land on the correct day + hour. zoneinfo falls back to
        # UTC if the name is unset or invalid.
        try:
            from zoneinfo import ZoneInfo
            tzname = (user.timezone or current_app.config.get("DEFAULT_TZ") or "UTC")
            user_tz = ZoneInfo(tzname)
        except Exception:
            user_tz = timezone.utc

        def _to_user(dt):
            if not dt:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(user_tz)

        timed_items: list = []
        allday_items: list = []
        for ev in events:
            if ev.all_day:
                allday_items.append({"kind": "event", "obj": ev, "title": ev.title})
            else:
                ev_start = _to_user(ev.start)
                ev_end = _to_user(ev.end) or ev_start
                # Duration drives the pill height alongside task pills so
                # both share the same vertical scale in the timeline.
                dur = 30
                if ev_start and ev_end and ev_end > ev_start:
                    dur = max(5, int((ev_end - ev_start).total_seconds() // 60))
                timed_items.append({
                    "kind": "event", "obj": ev,
                    "start": ev_start, "end": ev_end,
                    "duration_min": dur,
                    "title": ev.title,
                })
        for t in filtered:
            dt = _to_user(t.plan_date or t.due_date)
            if not dt:
                allday_items.append({"kind": "task", "obj": t, "title": t.title})
                continue
            timed_items.append({
                "kind": "task", "obj": t,
                "start": dt,
                "end": dt + timedelta(minutes=t.estimated_minutes or 30),
                "duration_min": t.estimated_minutes or 30,
                "title": t.title,
            })
        timed_items.sort(key=lambda x: x["start"])

        # Per-day buckets so week/month views can group items per day.
        # ``days_in_window`` mirrors the date-nav choice (1, 3, 7, 14)
        # and turns into a list of date objects.
        from datetime import date as _date
        days_in_window = [
            start_date + timedelta(days=i) for i in range(days)
        ]
        by_day: dict = {d: {"hours": {h: [] for h in range(24)},
                            "allday": []}
                        for d in days_in_window}
        for it in timed_items:
            d = it["start"].date()
            if d in by_day:
                by_day[d]["hours"][it["start"].hour].append(it)
        for it in allday_items:
            # Place all-day events under their start day; tasks without
            # a date go under the first day of the window.
            if it["kind"] == "event":
                d = _to_user(it["obj"].start).date()
            else:
                d = days_in_window[0]
            if d in by_day:
                by_day[d]["allday"].append(it)

        # Hour range — adaptive over the whole window so each day's
        # grid uses the same rows.
        used_hours: set = set()
        for d in days_in_window:
            for h, lst in by_day[d]["hours"].items():
                if lst:
                    used_hours.add(h)
        if used_hours:
            lo = max(0, min(min(used_hours), 6))
            hi = min(24, max(max(used_hours) + 1, 22))
        else:
            lo, hi = 8, 18

        timeline = {
            "hours": list(range(lo, hi)),
            "days": days_in_window,
            "by_day": by_day,
        }

    return render_template(
        "tasks/today.html",
        title=title,
        projects_with_dates=projects_with_dates,
        proj_header_info=proj_header_info,
        tasks=filtered,
        groups=groups,
        grouping=grouping,
        all_tags=all_tags,
        active_tag=active_tag,
        calendar_events=events,
        events_by_day=events_by_day,
        daily_buckets=daily_buckets,
        next_event=next_event,
        # Date-nav context
        start_date=start_date,
        days=days,
        today_iso=today_date.isoformat(),
        prev_iso=prev_date,
        next_iso=next_date,
        is_today_now=is_today_now,
        view_mode=view_mode,
        timeline=timeline,
    )

def _group_by_project(tasks, extra_projects=()):
    """Group Today's tasks by their project (Inbox = no project).

    The label is the project's full path (``Parent / Child / Sub``)
    so sub-projects appear under their parent in DFS order. A task
    whose immediate parent is a section bubbles up to the section's
    owning project, since sections are inline structure, not
    navigable nodes.

    ``extra_projects`` are projects that should also surface as
    headers even when they have no matching tasks today (e.g. a
    project whose own due_date hits today)."""

    def _project_root(item):
        """Walk past sections / tasks to the owning project/folder."""
        cur = item
        depth = 0
        while cur is not None and depth < 12:
            if cur.kind in ("project", "folder"):
                return cur
            cur = cur.parent
            depth += 1
        return None

    def _path(p):
        parts = []
        cur = p
        depth = 0
        while cur is not None and depth < 12:
            if cur.kind in ("project", "folder"):
                parts.append(cur.name)
            cur = cur.parent
            depth += 1
        return " / ".join(reversed(parts)) if parts else p.name

    by_proj: dict = {}  # pk -> [tasks]
    proj_obj: dict = {}  # pk -> Item (for path + sorting)
    inbox: list = []
    for t in tasks:
        owner = _project_root(t.project) if t.project else None
        if owner is None:
            inbox.append(t)
            continue
        proj_obj.setdefault(str(owner.pk), owner)
        by_proj.setdefault(str(owner.pk), []).append(t)
    for p in extra_projects or ():
        pk = str(p.pk)
        if pk not in by_proj:
            proj_obj[pk] = p
            by_proj[pk] = []

    # Sorting by path puts children right after their parent
    # alphabetically — DFS order falls out of the path comparison.
    paths = {pk: _path(p) for pk, p in proj_obj.items()}
    groups = []
    for pk in sorted(by_proj.keys(), key=lambda k: paths[k].lower()):
        groups.append((paths[pk], "bi-folder", by_proj[pk], pk))
    if inbox:
        groups.append(("Inbox", "bi-inbox", inbox, None))
    return groups

@tasks_bp.route("/next")
@login_required
def next_actions():
    """All Next-tagged tasks. Blocked (deferred or sequentially-blocked)
    entries are kept in the list but rendered greyed out, so the user
    can still see what's coming next."""
    user = current_user._get_current_object()
    tasks = _filter_by_project(
        Item.objects(_scope(user), status="next"), user,
    ).order_by("+due_date", "+sort_order")
    return render_template(
        "tasks/list.html", title="Next", perspective="next", tasks=tasks
    )

@tasks_bp.route("/forecast")
@login_required
def forecast():
    user = current_user._get_current_object()
    horizon = datetime.now(timezone.utc) + timedelta(days=14)
    tasks = (
        Item.objects(
            _scope(user),
            status__nin=["done", "dropped", "someday"],
            due_date__lte=horizon,
        )
        .order_by("+due_date")
    )
    return render_template(
        "tasks/list.html", title="Forecast (14d)", perspective="forecast", tasks=tasks
    )

@tasks_bp.route("/scheduled")
@login_required
def scheduled():
    """Tasks deferred to a future date.

    Shows every open task whose ``defer_date`` is still ahead of now —
    these are intentionally hidden from Next / Today until the date hits.
    """
    user = current_user._get_current_object()
    now = datetime.now(timezone.utc)
    tasks = (
        Item.objects(
            _scope(user),
            status__nin=["done", "dropped"],
            defer_date__gt=now,
        )
        .order_by("+defer_date")
    )
    return render_template(
        "tasks/list.html", title="Scheduled", perspective="scheduled", tasks=tasks
    )

@tasks_bp.route("/waiting")
@login_required
def waiting():
    user = current_user._get_current_object()
    tasks = _filter_by_project(
        Item.objects(_scope(user), status="waiting"), user,
    ).order_by("+due_date")
    return render_template(
        "tasks/list.html", title="Waiting", perspective="waiting", tasks=tasks
    )

@tasks_bp.route("/flagged")
@login_required
def flagged():
    user = current_user._get_current_object()
    tasks = _filter_by_project(
        Item.objects(_scope(user), status__nin=["done", "dropped"], flagged=True),
        user,
    ).order_by("+due_date")
    return render_template(
        "tasks/list.html", title="Flagged", perspective="flagged", tasks=tasks
    )

@tasks_bp.route("/someday")
@login_required
def someday():
    user = current_user._get_current_object()
    tasks = _filter_by_project(
        Item.objects(_scope(user), status="someday"), user,
    ).order_by("-updated_at")
    return render_template(
        "tasks/list.html", title="Someday / Maybe", perspective="someday", tasks=tasks
    )

@tasks_bp.route("/assigned")
@login_required
def assigned_to_me():
    user = current_user._get_current_object()
    tasks = _filter_by_project(
        Item.objects(assigned_to=user, status__nin=["done", "dropped"]),
        user,
    ).order_by("+due_date")
    return render_template(
        "tasks/list.html", title="Assigned to me", perspective="assigned", tasks=tasks
    )

@tasks_bp.route("/completed")
@login_required
def completed():
    user = current_user._get_current_object()
    tasks = _filter_by_project(
        Item.objects(_scope(user), status="done"), user,
    ).order_by("-completed_at").limit(200)
    return render_template(
        "tasks/list.html", title="Completed", perspective="completed", tasks=tasks
    )

@tasks_bp.route("/search")
@login_required
def search():
    user = current_user._get_current_object()
    q = (request.args.get("q") or "").strip()
    if not q:
        return render_template("tasks/list.html", title="Search", perspective="search", tasks=[])
    tasks = _filter_by_project(
        Item.objects(
            _scope(user) & (
                Q(title__icontains=q)
                | Q(note__icontains=q)
                | Q(external_ref__icontains=q)
            )
        ),
        user,
    ).order_by("-updated_at").limit(200)
    return render_template(
        "tasks/list.html",
        title=f"Search: {q}",
        perspective="search",
        tasks=tasks,
        query=q,
    )


@tasks_bp.route("/quickjump")
@login_required
def quickjump():
    """JSON endpoint backing the cmd-K-style command bar.

    Returns up to 20 items (projects + tasks) matching ``q`` by title.
    Projects come first so navigation by project name is reliable."""
    from flask import jsonify
    user = current_user._get_current_object()
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})
    base = _scope(user) & (
        Q(title__icontains=q) | Q(external_ref__icontains=q)
    )
    projects = list(
        Item.objects(base, kind__in=["project", "folder"]).order_by("+name").limit(8)
    )
    tasks = list(
        Item.objects(base, kind="task").order_by("-updated_at").limit(12)
    )
    out = []
    for p in projects:
        out.append({
            "kind": "folder" if p.is_folder else "project",
            "title": p.title,
            "url": url_for("projects.detail", project_id=str(p.pk)),
            "subtitle": (p.parent.title if p.parent else ""),
        })
    for t in tasks:
        out.append({
            "kind": "task",
            "title": t.title,
            "url": url_for("tasks.detail", task_id=str(t.pk)),
            "subtitle": (t.parent.title if t.parent else "Inbox"),
        })
    return jsonify({"results": out[:20]})

# ---------- CRUD ----------

@tasks_bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    user = current_user._get_current_object()
    form = TaskForm()
    _populate_form_choices(form, user)

    if request.method == "GET":
        project_id = request.args.get("project")
        if project_id:
            form.project.data = project_id
            form.status.data = "next"

    if form.validate_on_submit():
        task = Item(owner=user)
        _apply_form(form, task, user)
        _apply_smart_tokens(task, form.title.data, user)
        task.save()
        _writeback_calendar(task, user)
        if _is_inspector():
            if form.add_another.data:
                # Stay open with a fresh empty form, keeping the same
                # parent project so the user can keep typing.
                return _render_empty_task_inspector(
                    user,
                    parent_project=task.project,
                    just_added_title=task.title,
                )
            # Re-render with the saved task so the inspector stays open and
            # the next submit goes to /edit, not /new.
            return _render_task_inspector(task, user, just_saved=True)
        flash("Item created.", "success")
        return redirect(url_for("tasks.detail", task_id=str(task.pk)))
    context = {"form": form, "task": None, "form_action": url_for("tasks.create")}
    if _is_inspector():
        return render_template("tasks/_inspector.html", **context)
    return render_template("tasks/form.html", **context)

@tasks_bp.route("/<task_id>")
@login_required
def detail(task_id):
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    # Mirror of the projects.detail redirect — if the pk actually
    # points at a project/folder/section, hand off to the right view
    # so the same item never renders as both kinds.
    if task.kind != "task":
        return redirect(url_for("projects.detail", project_id=task_id))
    return render_template(
        "tasks/detail.html",
        task=task,
        comment_form=CommentForm(),
        form=_build_task_form(task, user),
        form_action=url_for("tasks.edit", task_id=str(task.pk)),
    )

@tasks_bp.route("/<task_id>/edit", methods=["GET", "POST"])
@login_required
def edit(task_id):
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)

    form = TaskForm(obj=task)
    _populate_form_choices(form, user)

    if request.method == "GET":
        from application.services.tz import localise_form_dates
        localise_form_dates(form, user)
        # Surface a project pk in the select. If the task's actual
        # parent is a section, hop up to the section's owning project
        # so there's a matching option to highlight (and so a POST
        # without changes round-trips cleanly).
        proj_pk = ""
        if task.project is not None:
            p = task.project
            depth = 0
            while p is not None and p.kind == "section" and depth < 6:
                p = p.parent
                depth += 1
            if p is not None and p.kind == "project":
                proj_pk = str(p.pk)
        form.project.data = proj_pk
        form.tags.data = list(task.tags or [])
        form.assigned_to.data = str(task.assigned_to.pk) if task.assigned_to else ""
        form.priority.data = str(task.priority) if task.priority else ""

    if form.validate_on_submit():
        _apply_form(form, task, user)
        _apply_smart_tokens(task, form.title.data, user)
        task.save()
        _writeback_calendar(task, user)
        if _is_inspector():
            return _render_task_inspector(task, user, just_saved=True)
        flash("Item updated.", "success")
        return redirect(url_for("tasks.detail", task_id=str(task.pk)))
    context = {
        "form": form,
        "task": task,
        "form_action": url_for("tasks.edit", task_id=str(task.pk)),
    }
    if _is_inspector():
        return render_template("tasks/_inspector.html", **context)
    return render_template("tasks/form.html", **context)

@tasks_bp.route("/<task_id>/complete", methods=["POST"])
@login_required
def complete(task_id):
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    task.complete(by_user=user)
    flash("Item completed.", "success")
    return redirect(request.referrer or url_for("dashboard.home"))

@tasks_bp.route("/<task_id>/reopen", methods=["POST"])
@login_required
def reopen(task_id):
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    task.status = "next"
    task.completed_at = None
    task.save()
    return redirect(request.referrer or url_for("tasks.detail", task_id=task_id))

@tasks_bp.route("/<task_id>/flag", methods=["POST"])
@login_required
def toggle_flag(task_id):
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    task.flagged = not task.flagged
    task.save()
    return redirect(request.referrer or url_for("tasks.detail", task_id=task_id))

@tasks_bp.route("/<task_id>/delete", methods=["POST"])
@login_required
def delete(task_id):
    from flask import jsonify
    from application.services.calendar_writeback import delete_task_calendar_events
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or task.owner != user:
        abort(404)
    # Reparent any children (sub-tasks / nested items) up one level so
    # we don't leave dangling references — those would crash
    # mongoengine on the next dereference. Sub-tasks of a deleted
    # task become tasks under the task's former parent (or the inbox
    # if it was top-level).
    Item.objects(parent=task).update(set__parent=task.parent)
    delete_task_calendar_events(task, user)
    task.delete()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    flash("Item deleted.", "info")
    # Stay on whatever list / perspective / project page the user came
    # from. ``request.referrer`` is the previous URL; if it points to
    # this task's own detail page (now gone), fall back to home.
    back = request.referrer or ""
    if not back or f"/tasks/{task_id}" in back:
        back = url_for("dashboard.home")
    return redirect(back)

@tasks_bp.route("/quick-add", methods=["POST"])
@login_required
def quick_add():
    """Todoist-style quick add: parse a single line of free text.

    Body (form or JSON):
        text         — required, the raw line
        project_id   — optional, default project pk (used when no #project
                       token is in the text)

    Response: JSON describing the created task. The "Add another" UX in
    the modal POSTs again with empty input cleared client-side.
    """
    from flask import jsonify
    from application.services.quick_parse import parse_task_text

    user = current_user._get_current_object()
    data = request.get_json(silent=True) or request.form
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Empty input"}), 400

    default_project = None
    default_project_id = (data.get("project_id") or "").strip()
    if default_project_id:
        default_project = Item.objects(pk=default_project_id).first()
        if default_project and not default_project.can_be_viewed_by(user):
            default_project = None

    parsed = parse_task_text(text, user, default_project=default_project)
    if not parsed.title:
        return jsonify({"error": "Couldn't extract a title from the input."}), 400

    task = Item(
        owner=user,
        kind="task",
        title=parsed.title,
        parent=parsed.project,
        tags=parsed.tags,
        priority=parsed.priority,
        due_date=parsed.due_date,
        flagged=parsed.flagged,
        status="open",
    )
    task.save()
    _writeback_calendar(task, user)
    return jsonify({
        "ok": True,
        "task": {
            "id": str(task.pk),
            "title": task.title,
            "project": str(task.project) if task.project else None,
            "tags": [str(t) for t in (task.tags or [])],
            "priority": task.priority,
            "due_date": task.due_date.isoformat() if task.due_date else None,
            "flagged": task.flagged,
        },
        "warnings": {
            "unresolved_tags": parsed.unresolved_tags,
            "unresolved_project": parsed.unresolved_project,
        },
    })

@tasks_bp.route("/<task_id>/convert-to-project", methods=["POST"])
@login_required
def convert_to_project(task_id):
    """Promote a task to a project — single ``kind`` flip on the unified
    Item. Any existing sub-tasks stay attached as children of what's now
    a project; tags, dates, comments survive untouched.
    """
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or task.owner != user:
        abort(404)
    task.kind = "project"
    task.project_type = "parallel"
    task.status = "active"
    task.review_interval_days = task.review_interval_days or 7
    # Tasks track completion via ``completed_at`` — clear it now so the
    # project doesn't look completed when it wasn't.
    task.completed_at = None
    task.save()
    flash(f"Converted task into project: {task.title}", "success")
    return redirect(url_for("projects.detail", project_id=str(task.pk)))

@tasks_bp.route("/merge", methods=["POST"])
@login_required
def merge():
    """Drag-merge: drop task A onto task B → B becomes a project, A is its child.

    The unified Item model lets a task hold children — flipping the
    target's kind to "project" is enough; no extra container is created
    and the project limit doesn't trigger because nothing new gets made.
    """
    from flask import jsonify, render_template
    user = current_user._get_current_object()
    source = Item.objects(pk=request.form.get("source_id"), owner=user).first()
    target = Item.objects(pk=request.form.get("target_id"), owner=user).first()
    if not source or not target or source.pk == target.pk:
        return jsonify({"error": "invalid tasks"}), 400
    # Flip target → project (idempotent) and nest source under it.
    target.ensure_container()
    source.parent = target
    source.save()
    # Render the merged block showing the new project (= target) and
    # all its children, so the client can swap the two rows out for it.
    children = list(
        Item.objects(parent=target, kind="task")
            .order_by("+sort_order", "+created_at")
    )
    html = render_template(
        "tasks/_merged_block.html",
        project=target, tasks=children,
        target_project_id=str(target.pk),
    )
    return jsonify({
        "ok": True,
        "project_id": str(target.pk),
        "project_name": target.title,
        "html": html,
    })

@tasks_bp.route("/<task_id>/schedule", methods=["POST"])
@login_required
def schedule(task_id):
    """Drag a task into a calendar slot → set ``plan_date``.

    Body: ``date=YYYY-MM-DD`` and optional ``hour=0..23``. Without
    hour the task becomes all-day at midnight (still gets a Plan-date
    event when Google Calendar write-back is enabled). Triggers the
    calendar writeback so the event mirrors on Google immediately.
    """
    from flask import jsonify
    from datetime import datetime as _dt, time as _t
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    date_raw = (request.form.get("date") or "").strip()
    hour_raw = (request.form.get("hour") or "").strip()
    if not date_raw:
        return jsonify({"error": "date required"}), 400
    try:
        d = _dt.fromisoformat(date_raw).date()
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    # Interpret the wall-clock time in the user's timezone so it lands
    # on the right hour in their view.
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(user.timezone or current_app.config.get("DEFAULT_TZ") or "UTC")
    except Exception:
        tz = timezone.utc
    if hour_raw:
        try:
            h = max(0, min(23, int(hour_raw)))
        except ValueError:
            return jsonify({"error": "invalid hour"}), 400
        wall = _dt.combine(d, _t(h, 0))
    else:
        wall = _dt.combine(d, _t(0, 0))
    task.plan_date = wall.replace(tzinfo=tz)
    task.save()
    warnings = _writeback_calendar(task, user) or []
    return jsonify({
        "ok": True,
        "plan_date": task.plan_date.isoformat(),
        "warnings": warnings,
    })


@tasks_bp.route("/<task_id>/duration", methods=["POST"])
@login_required
def duration(task_id):
    """Resize a task in the calendar view → set ``estimated_minutes``.

    Body: ``minutes=<int>`` (clamped 5..1440). Triggers GCal write-back
    so the event on Google grows/shrinks to match.
    """
    from flask import jsonify
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    try:
        minutes = int((request.form.get("minutes") or "").strip())
    except ValueError:
        return jsonify({"error": "minutes required"}), 400
    minutes = max(5, min(1440, minutes))
    task.estimated_minutes = minutes
    task.save()
    warnings = _writeback_calendar(task, user) or []
    return jsonify({"ok": True, "estimated_minutes": minutes, "warnings": warnings})


@tasks_bp.route("/<task_id>/move-options")
@login_required
def move_options(task_id):
    """JSON list of suggested projects for moving this task.

    Heuristic: when the task lives anywhere under a top-level
    project / folder X, suggest *every* container in X's subtree
    (sections, sub-projects, sub-folders) — those are the natural
    destinations the user will pick when moving a task around their
    project. Direct siblings of the task's current parent float to
    the top so the most common move ("between sections of the same
    project") is one keystroke. The full search via
    ``/tasks/quickjump`` covers anything outside this subtree.

    When the task is in the inbox we fall back to top-level
    projects / folders.
    """
    from flask import jsonify
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)

    cur_parent = task.parent
    q = (request.args.get("q") or "").strip().lower()

    def _path_of(item) -> str:
        """Full breadcrumb path of an item, root-first."""
        parts: list[str] = []
        cur = item
        seen: set = set()
        while cur and cur.pk not in seen:
            seen.add(cur.pk)
            parts.append(cur.name)
            cur = cur.parent
        return " › ".join(reversed(parts))

    def _entry(p) -> dict:
        return {
            "id": str(p.pk),
            "name": p.name,
            "kind": p.kind,
            "parent": (p.parent.name if p.parent else ""),
            "path": _path_of(p),
        }

    # Free-text search across all the user's containers, regardless
    # of subtree. Path-aware so typing "fmg / back" matches the
    # "Backlog" section inside FMG.
    if q:
        all_containers = list(
            Item.objects(
                (Q(owner=user) | Q(members=user)),
                kind__in=["project", "folder", "section"],
                status="active",
            ).order_by("+name").limit(800)
        )
        scored = []
        for p in all_containers:
            if cur_parent and p.pk == cur_parent.pk:
                continue
            entry = _entry(p)
            haystack = (entry["path"] + " " + entry["name"]).lower()
            if all(tok in haystack for tok in q.split()):
                scored.append(entry)
        return jsonify({
            "current_project_id": str(cur_parent.pk) if cur_parent else "",
            "current_project_name": cur_parent.name if cur_parent else "Inbox",
            "suggestions": scored[:80],
            "query": q,
        })

    suggestions: list[dict] = []
    if cur_parent is None:
        # Inbox — show top-level active containers.
        for p in Item.objects(
            (Q(owner=user) | Q(members=user)),
            kind__in=["project", "folder"],
            parent=None, status="active",
        ).order_by("+name").limit(50):
            suggestions.append(_entry(p))
        return jsonify({
            "current_project_id": "",
            "current_project_name": "Inbox",
            "suggestions": suggestions,
        })

    # Anchor: walk up from cur_parent until we find the first ancestor
    # whose direct children include something OTHER than cur_parent's
    # chain. That's the "containing project" the user thinks of —
    # e.g. for a task inside FMG/Monitoring/, we anchor on FMG (it
    # holds the sections + sub-projects), not on Monitoring (which
    # only has the task itself) or on the top-level "Arbeit" (which
    # would pull in unrelated projects).
    anchor = cur_parent
    seen: set = set()
    while anchor and anchor.pk not in seen:
        seen.add(anchor.pk)
        other = Item.objects(
            (Q(owner=user) | Q(members=user)),
            parent=anchor.pk,
            kind__in=["project", "folder", "section"],
            status="active",
            pk__ne=cur_parent.pk,
        ).limit(1)
        if other.first():
            break
        anchor = anchor.parent

    # BFS through the anchor's subtree to collect every container, in
    # alphabetical order at each level. Exclude cur_parent itself.
    descendants: list = []
    if anchor and anchor.pk != cur_parent.pk:
        descendants.append(anchor)
    frontier = [anchor] if anchor else []
    visited_pks: set = set()
    while frontier:
        nxt = []
        kids = list(Item.objects(
            (Q(owner=user) | Q(members=user)),
            parent__in=[f.pk for f in frontier],
            kind__in=["project", "folder", "section"],
            status="active",
        ).order_by("+name"))
        for k in kids:
            if k.pk in visited_pks or k.pk == cur_parent.pk:
                continue
            visited_pks.add(k.pk)
            descendants.append(k)
            nxt.append(k)
        frontier = nxt

    # Direct siblings of cur_parent bubble to the top — that's the
    # most common "move between sections of the same project" case.
    sibling_pks = {
        str(s.pk) for s in Item.objects(
            (Q(owner=user) | Q(members=user)),
            parent=cur_parent.parent,
            kind__in=["project", "folder", "section"],
            status="active",
            pk__ne=cur_parent.pk,
        )
    }

    first, rest = [], []
    for p in descendants:
        entry = _entry(p)
        (first if str(p.pk) in sibling_pks else rest).append(entry)
    suggestions = first + rest

    return jsonify({
        "current_project_id": str(cur_parent.pk) if cur_parent else "",
        "current_project_name": cur_parent.name if cur_parent else "Inbox",
        "suggestions": suggestions[:80],
    })


@tasks_bp.route("/<task_id>/move", methods=["POST"])
@login_required
def move(task_id):
    """Move a task: optionally change its project, and renumber sort_order
    for every sibling in the destination container so the displayed order
    actually survives a reload.

    Payload:
        siblings[] - ordered list of task IDs in the new container; each
                     gets sort_order = its index in this list.
        project_id - new project pk; empty string clears, key absent leaves
                     the project untouched.
        sort_order - legacy single-index fallback when no siblings are sent.
    """
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)

    # Accept both form-encoded and JSON bodies.
    json_body = request.get_json(silent=True)
    if json_body is not None:
        data = json_body
        siblings = list(data.get("siblings") or data.get("siblings[]") or [])
    else:
        data = request.form
        siblings = data.getlist("siblings[]") or data.getlist("siblings")

    # 1) Change project on the moved task itself.
    touched = False
    if "project_id" in data:
        pid = (data.get("project_id") or "").strip()
        if pid:
            from application.services.permissions import resolve_visible_item
            proj = resolve_visible_item(user, pid)
            if not proj:
                return jsonify({"error": "destination project not accessible"}), 403
            task.project = proj
        else:
            task.project = None
        touched = True

    # 2) Apply status from a status-tagged drop target. The pseudo-value
    #    "open" means "make it actionable" — reopen done/dropped tasks but
    #    leave already-open ones (inbox/next/waiting/…) alone.
    if "status" in data:
        target_status = (data.get("status") or "").strip()
        if target_status == "open":
            if task.status in ("done", "dropped"):
                task.status = "next"
                task.completed_at = None
                touched = True
        elif target_status in {"open", "next", "waiting", "scheduled",
                                "someday", "done", "dropped"}:
            if target_status != task.status:
                task.status = target_status
                if target_status != "done":
                    task.completed_at = None
                touched = True

    if touched:
        task.save()

    # 2) Renumber the destination container. Bulk-update via raw mongoengine
    #    so we don't accidentally re-save other fields we haven't loaded.
    if siblings:
        scope = Q(owner=user) | Q(assigned_to=user)
        for index, sid in enumerate(siblings):
            Item.objects(pk=sid).filter(scope).update(set__sort_order=index)
    elif "sort_order" in data:
        try:
            task.sort_order = int(data["sort_order"])
            task.save()
        except (TypeError, ValueError):
            pass

    if request.is_json or request.headers.get("X-Inspector-Request") == "1":
        from flask import jsonify
        return jsonify(
            {
                "ok": True,
                "task_id": str(task.pk),
                "project_id": str(task.project.pk) if task.project else None,
            }
        )
    return redirect(request.referrer or url_for("dashboard.home"))

@tasks_bp.route("/bulk", methods=["POST"])
@login_required
def bulk():
    """Apply an action to many tasks at once.

    Form fields:
        task_ids[] - one or more task pks
        action     - delete | complete | reopen | set_status | set_project
        status     - target status string (when action=set_status)
        project_id - target project pk or empty string (when action=set_project)
    """
    user = current_user._get_current_object()
    ids = request.form.getlist("task_ids[]") or request.form.getlist("task_ids")
    action = (request.form.get("action") or "").strip()
    if not ids:
        flash("Nothing selected.", "warning")
        return redirect(request.referrer or url_for("dashboard.home"))

    tasks = list(Item.objects(pk__in=ids))
    count = 0
    skipped = 0

    if action == "delete":
        from application.services.calendar_writeback import delete_task_calendar_events
        for t in tasks:
            if t.owner == user:
                delete_task_calendar_events(t, user)
                t.delete()
                count += 1
            else:
                skipped += 1
    elif action == "complete":
        for t in tasks:
            if t.can_be_edited_by(user):
                t.complete(by_user=user)
                count += 1
            else:
                skipped += 1
    elif action == "reopen":
        for t in tasks:
            if t.can_be_edited_by(user):
                t.status = "next"
                t.completed_at = None
                t.save()
                count += 1
            else:
                skipped += 1
    elif action == "set_status":
        from application.models.item import TASK_STATUS

        target = request.form.get("status")
        if target not in TASK_STATUS:
            flash("Invalid status.", "danger")
            return redirect(request.referrer or url_for("dashboard.home"))
        for t in tasks:
            if t.can_be_edited_by(user):
                t.status = target
                if target != "done":
                    t.completed_at = None
                t.save()
                count += 1
            else:
                skipped += 1
    elif action == "set_project":
        project_id = (request.form.get("project_id") or "").strip()
        project = Item.objects(pk=project_id).first() if project_id else None
        for t in tasks:
            if t.can_be_edited_by(user):
                t.project = project
                t.save()
                count += 1
            else:
                skipped += 1
    elif action == "toggle_flag":
        # Group toggle: if any selected is unflagged → flag all; otherwise unflag all.
        all_flagged = bool(tasks) and all(t.flagged for t in tasks)
        new_state = not all_flagged
        for t in tasks:
            if t.can_be_edited_by(user):
                t.flagged = new_state
                t.save()
                count += 1
            else:
                skipped += 1
    elif action == "set_priority":
        raw = (request.form.get("priority") or "").strip()
        target: int | None
        if raw in ("", "0", "none"):
            target = None
        else:
            try:
                target = int(raw)
            except ValueError:
                flash("Invalid priority.", "danger")
                return redirect(request.referrer or url_for("dashboard.home"))
            if target not in (1, 2, 3, 4):
                flash("Priority must be 1, 2, 3, 4 or empty.", "danger")
                return redirect(request.referrer or url_for("dashboard.home"))
        for t in tasks:
            if t.can_be_edited_by(user):
                t.priority = target
                t.save()
                count += 1
            else:
                skipped += 1
    else:
        flash(f"Unknown action: {action}", "danger")
        return redirect(request.referrer or url_for("dashboard.home"))

    if count and not skipped:
        flash(f"{action.replace('_', ' ').title()}: {count} task(s).", "success")
    elif count and skipped:
        flash(
            f"{action.replace('_', ' ').title()}: {count} task(s). "
            f"⚠️ {skipped} skipped — you don't own them.",
            "warning",
        )
    elif skipped:
        flash(
            f"Nothing changed — {skipped} task(s) skipped because you don't own them. "
            "Ask the owner, or pick tasks you created.",
            "danger",
        )
    else:
        flash("Nothing selected.", "warning")
    return redirect(request.referrer or url_for("dashboard.home"))

@tasks_bp.route("/<task_id>/comment", methods=["POST"])
@login_required
def comment(task_id):
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    form = CommentForm()
    if form.validate_on_submit():
        task.add_comment(user, form.body.data)
        if request.headers.get("X-Inspector-Request") == "1":
            from flask import jsonify
            c = task.comments[-1]
            return jsonify(
                {
                    "ok": True,
                    "index": len(task.comments) - 1,
                    "author": str(c.author),
                    "body": c.body,
                    "created_at": c.created_at.isoformat(),
                    "is_public": c.is_public,
                }
            )
        flash("Comment posted.", "success")
    return redirect(url_for("tasks.detail", task_id=task_id))

@tasks_bp.route("/<task_id>/comments/<int:index>/toggle-visibility", methods=["POST"])
@login_required
def toggle_comment_visibility(task_id, index):
    """Flip a comment's is_public flag. Author or task owner only."""
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    if not (0 <= index < len(task.comments or [])):
        abort(404)
    comment = task.comments[index]
    if comment.author != user and task.owner != user:
        abort(403)
    comment.is_public = not comment.is_public
    task.save()
    if request.headers.get("X-Inspector-Request") == "1" or request.is_json:
        from flask import jsonify
        return jsonify({"index": index, "is_public": comment.is_public})
    return redirect(request.referrer or url_for("tasks.detail", task_id=task_id))


def _editable_task_or_404(task_id):
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    return task


def _checklist_response(task):
    """Return the re-rendered checklist partial for AJAX callers, otherwise
    redirect back to the detail page (no-JS fallback)."""
    if request.headers.get("X-Partial") == "1":
        return render_template("tasks/_checklist.html", task=task)
    return redirect(url_for("tasks.detail", task_id=str(task.pk)) + "#checklist")


@tasks_bp.route("/<task_id>/note", methods=["POST"])
@login_required
def note_edit(task_id):
    """Update just the note from the inline editor on the detail page."""
    task = _editable_task_or_404(task_id)
    task.note = request.form.get("note", "") or ""
    task.save()
    if request.headers.get("X-Partial") == "1":
        return render_template("tasks/_note_body.html", task=task)
    return redirect(url_for("tasks.detail", task_id=task_id) + "#note")


# Pasted note images are compressed client-side and stored inline on the item.
NOTE_IMAGE_MAX_BYTES = 5 * 1024 * 1024     # per-image upload cap
ITEM_WARN_BYTES = 10 * 1024 * 1024         # warn the user past this
ITEM_MAX_BYTES = 15 * 1024 * 1024          # refuse past this (MongoDB caps at 16)


def _sniff_image_type(head: bytes) -> str | None:
    """Canonical mimetype for ``head`` if it's a supported raster image.
    SVG is intentionally unsupported (it can carry script)."""
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _item_bson_size(task) -> int:
    from bson import BSON
    return len(BSON.encode(task.to_mongo()))


@tasks_bp.route("/<task_id>/note-image", methods=["POST"])
@login_required
def note_image_upload(task_id):
    """Store a pasted (already client-compressed) image inline on the item
    and return a clean markdown reference. Warns / refuses as the item grows."""
    import secrets
    task = _editable_task_or_404(task_id)
    fileobj = request.files.get("file")
    if not fileobj:
        return jsonify({"error": "no file"}), 400
    data = fileobj.read()
    if not data:
        return jsonify({"error": "empty file"}), 400
    if len(data) > NOTE_IMAGE_MAX_BYTES:
        return jsonify({"error": f"image too large (max {NOTE_IMAGE_MAX_BYTES // 1024 // 1024} MB)"}), 413
    ctype = _sniff_image_type(data[:12])
    if not ctype:
        return jsonify({"error": "unsupported image format"}), 415

    token = secrets.token_urlsafe(12)
    task.note_images.append(NoteImage(token=token, content_type=ctype, data=data))

    size = _item_bson_size(task)
    if size > ITEM_MAX_BYTES:
        task.note_images.pop()  # don't persist — would risk the 16 MB doc cap
        return jsonify({"error": "item is full — remove some images before adding more"}), 413
    task.save()

    payload = {"markdown": f"![pasted image]({url_for('tasks.note_image', task_id=str(task.pk), token=token)})"}
    if size > ITEM_WARN_BYTES:
        payload["warning"] = f"This item is getting large ({size / 1024 / 1024:.1f} MB of 16 MB max) — consider removing some images."
    return jsonify(payload)


@tasks_bp.route("/<task_id>/note-image/<token>")
@login_required
def note_image(task_id, token):
    """Serve an inline note image stored on the item."""
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    img = next((i for i in task.note_images if i.token == token), None)
    if not img:
        abort(404)
    return Response(
        bytes(img.data),
        mimetype=img.content_type or "image/webp",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@tasks_bp.route("/<task_id>/checklist", methods=["POST"])
@login_required
def checklist_add(task_id):
    """Append a checklist item (checkbox + text) to a task."""
    task = _editable_task_or_404(task_id)
    text = (request.form.get("text") or "").strip()
    if text:
        task.checklist.append(ChecklistItem(text=text))
        task.save()
    return _checklist_response(task)


@tasks_bp.route("/<task_id>/checklist/<int:index>/toggle", methods=["POST"])
@login_required
def checklist_toggle(task_id, index):
    """Flip a checklist item's done flag."""
    task = _editable_task_or_404(task_id)
    if not (0 <= index < len(task.checklist or [])):
        abort(404)
    task.checklist[index].done = not task.checklist[index].done
    task.save()
    return _checklist_response(task)


@tasks_bp.route("/<task_id>/checklist/<int:index>/delete", methods=["POST"])
@login_required
def checklist_delete(task_id, index):
    """Remove a checklist item."""
    task = _editable_task_or_404(task_id)
    if not (0 <= index < len(task.checklist or [])):
        abort(404)
    task.checklist.pop(index)
    task.save()
    return _checklist_response(task)
