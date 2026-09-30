"""Item views — perspectives, CRUD, comments, assignments."""
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for,
)

from application.services.tzutil import local_date, resolve_tz

def _is_inspector():
    return request.args.get("inspector") == "1" or request.headers.get("X-Inspector-Request") == "1"

def _apply_smart_tokens(task: "Item", raw_title: str, user) -> None:
    """Merge Todoist-style tokens parsed out of ``raw_title`` into ``task``.

    Form fields the user filled explicitly win — the parser only fills
    gaps. Recognised tokens (@person, +tag, #project, pN, !, dates) are
    stripped from the stored title.
    """
    from application.services.quick_parse import parse_task_text

    parsed = parse_task_text(raw_title, user, default_project=task.project)
    if parsed.title == raw_title.strip() and not parsed.tags and not parsed.unresolved_tags:
        # No tokens were present — leave everything alone, including title.
        return

    task.title = parsed.title or raw_title.strip()
    if parsed.project is not None and parsed.project != task.project:
        task.project = parsed.project
    added = set()
    if parsed.tags:
        merged = list(task.tags or [])
        existing = {n.lower() for n in merged}
        for name in parsed.tags:
            if name.lower() not in existing:
                merged.append(name)
                existing.add(name.lower())
                added.add(name)
        task.tags = merged
    if parsed.priority and not task.priority:
        task.priority = parsed.priority
    if parsed.due_date and not task.due_date:
        task.due_date = parsed.due_date
    if parsed.flagged:
        task.flagged = True
    # @person fills the gap when the form didn't set one explicitly.
    if parsed.assignee and not task.assigned_to:
        task.assign_to(parsed.assignee, by_user=user)
    # A tag typed into the title (+Buchhaltung) carries its auto-assign rule
    # like one picked in the inspector — filling the gap only, so an explicit
    # @person or a picked assignee above still wins.
    if added and not task.assigned_to:
        from application.services.label_rules import apply_assignee_rule
        apply_assignee_rule(task, user, only_tags=added)

def _render_empty_task_inspector(user, *, parent_project=None, just_added_title=None):
    """Reset the inspector to a fresh empty new-task form.

    Used after "Add another" so the user can rapid-fire create several
    tasks without leaving the panel.
    """
    # ``formdata=None`` is essential: without it Flask-WTF auto-binds to the
    # just-submitted ``request.form`` (we're inside the create POST), so the
    # "fresh" form would echo back the title/fields the user just saved.
    form = TaskForm(formdata=None)
    _populate_form_choices(form, user)
    if parent_project is not None:
        form.project.data = str(parent_project.pk)
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
    _populate_form_choices(form, user, task)
    form.project.data = str(task.project.pk) if task.project else ""
    form.tags.data = list(task.tags or [])
    form.assigned_to.data = str(task.assigned_to.pk) if task.assigned_to else ""
    form.owner.data = str(task.owner.pk) if task.owner else ""
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
from application.models.item import (
    ChecklistItem, Item, NoteImage, NoteFile,
    ITEM_WARN_BYTES, ITEM_MAX_BYTES,
)

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
    from application.services import focus
    from application.services.permissions import visible_items_q
    # owned / assigned / shared-by-name / team-shared. (members also keeps a
    # re-delegated task visible to whoever passed it on.) The WIDE scope —
    # used by search, quickjump and the inbox. Perspective LISTS use
    # ``_perspective_scope`` so they can hide shared work by default.
    return focus.restrict(visible_items_q(user), user)


def _perspective_scope(user):
    """Scope for the perspective lists (Today, Next, Flagged, Waiting, …).

    Personal by default (own/assigned) so team-shared and re-delegated tasks
    don't clutter your action lists; the user opts to include them via the
    toolbar toggle (``perspectives_show_shared``). Same focus.restrict wrapping
    as ``_scope`` so Focus mode keeps working."""
    from application.services import focus
    from application.services.permissions import perspective_base_q
    return focus.restrict(perspective_base_q(user), user)

_PASS_KEYS = frozenset(
    {"today", "next", "waiting", "waiting_reply", "review", "someday",
     "scheduled", "flagged", "forecast", "followup"}
)

def sidebar_counts(user, now=None, keys=None) -> dict:
    """Item counts for the sidebar perspective badges, keyed by perspective
    key. ``keys`` limits the work to the perspectives actually shown in the
    sidebar — a hidden view costs nothing (the single task pass is skipped
    unless a status/date perspective is visible, and the reference-based
    inbox/assignee count() queries only run when those are visible).

    The status/date counts come from one pass over the user's open tasks
    (scalar fields only — no per-doc deref). Mirrors each perspective view's
    filter; completed/home never get a badge."""
    now = now or datetime.now(timezone.utc)
    want = set(keys) if keys is not None else (_PASS_KEYS | {"inbox", "assigned", "delegated"})
    c = {}

    if want & _PASS_KEYS:
        tz = resolve_tz(user)
        today_date = now.astimezone(tz).date()
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        horizon = now + timedelta(days=14)
        c = {k: 0 for k in _PASS_KEYS}
        # Badges must match what each perspective list actually shows, so use
        # the same (toggle-aware) perspective scope.
        qs = (Item.objects(_perspective_scope(user), kind="task", status__nin=["done", "dropped"])
              .only("status", "flagged", "due_date", "plan_date", "defer_date",
                    "followup_at"))
        for t in qs:
            st = t.status
            # Badge = follow-ups that need attention now (due/overdue). The
            # Follow-up view itself lists future ones too, but the badge is a
            # "do something today" counter, like the other date perspectives.
            if t.followup_at and t.followup_at <= today_end:
                c["followup"] += 1
            if st == "next":
                c["next"] += 1
            elif st == "waiting":
                c["waiting"] += 1
            elif st == "waiting_reply":
                c["waiting_reply"] += 1
            elif st == "review":
                c["review"] += 1
            elif st == "someday":
                c["someday"] += 1
            if t.defer_date and t.defer_date > now:
                c["scheduled"] += 1
            if t.flagged:
                c["flagged"] += 1
            if st != "someday":
                if ((t.due_date and t.due_date <= today_end)
                        or t.flagged
                        or (t.plan_date and local_date(t.plan_date, tz) <= today_date)
                        or (t.defer_date and t.defer_date <= now and st == "next")
                        or (t.followup_at and t.followup_at <= today_end)):
                    c["today"] += 1
                if t.due_date and t.due_date <= horizon:
                    c["forecast"] += 1

    # Reference-based perspectives — count() avoids dereferencing per task.
    if "inbox" in want:
        from application.services.permissions import inbox_q
        c["inbox"] = Item.objects((_scope(user) & inbox_q(user)), kind="task",
                                  status__nin=["done", "dropped"]).count()
    if "assigned" in want:
        from application.services import focus
        c["assigned"] = Item.objects(
            focus.restrict(Q(assigned_to=user), user), kind="task",
            status__nin=["done", "dropped"]).count()
    if "delegated" in want:
        from application.services import focus
        c["delegated"] = Item.objects(
            focus.restrict(
                (Q(owner=user) | Q(assigned_by=user))
                & Q(assigned_to__ne=None) & Q(assigned_to__ne=user),
                user),
            kind="task", status__nin=["done", "dropped"]).count()
    return c

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

def _populate_form_choices(form: TaskForm, user: User, task: Item | None = None) -> None:
    # The location picker lists projects, folders AND sections — all valid task
    # containers (a task's parent can be any of them). Labels carry the full
    # breadcrumb so a section reads as "🔖  Work / Website / Backlog" and a
    # project nested in folders is unambiguous (see services/containers.py).
    # Including sections is what lets the section "+" button (which seeds
    # ?project=<section_pk>) actually file the task into that section, and gives
    # the inspector a manual section picker.
    from application.services.containers import container_choices
    form.project.choices = [("", "—")] + container_choices(
        user, selectable_kinds=("project", "folder", "section"),
        include_shared=True, active_projects_only=True,
    )
    # Tag dropdown choices use the tag NAME both as value and label.
    # Render in tree order with indentation so nested tags are obvious.
    form.tags.choices = _tag_choices_tree(user.tags or [])

    # Assignee choices: everyone across *all* of the user's teams (the team
    # they're a member of + every team they admin), with the team name in
    # parentheses so duplicates across teams stay distinguishable.
    from application.services.permissions import teams_of, team_members
    # Shared "people pool" for both the Assignee and the Owner pickers: the
    # actor, everyone across all their teams, plus the task's current owner +
    # assignee (so both stay valid selections even without a shared team).
    people: list = [(str(user.pk), f"{user} (me)")]
    seen = {str(user.pk)}
    for team in teams_of(user):
        for member in team_members(team):
            pk = str(member.pk)
            if pk in seen:
                continue
            seen.add(pk)
            people.append((pk, f"{member} ({team.name})"))
    if task is not None:
        for u in (task.owner, task.assigned_to):
            if u is None:
                continue
            pk = str(u.pk)
            if pk in seen:
                continue
            seen.add(pk)
            people.append((pk, f"{u}"))
    # Assignee: the pool plus "unassigned". Selecting the owner hands the task
    # back to them (see _apply_form: assigning to the owner is an unassign).
    form.assigned_to.choices = [("", "— unassigned —")] + people
    # Owner (Jira-style): the same pool, but a task must always have an owner,
    # so there's no blank option.
    form.owner.choices = people

def resolve_tag_names(user: User, raw_names) -> list:
    """Normalise a submitted list of tag names into the list to store.

    Type-to-create any name the user doesn't have yet (gray default colour,
    mirroring OmniFocus) and enforce exclusive-children groups (at most one
    child from an exclusive parent). Shared by the task inspector, the subtask
    quick-edit modal and the project inspector, which all carry the same
    token field."""
    from application.models.tag import Tag
    # Tags are stored as a list of names; the multi-select submits names.
    # Normalise once (strip + cap at the model's 64-char limit) so a
    # type-created tag and the name stored on the item always match.
    submitted = [n.strip()[:64] for n in (raw_names or []) if n and n.strip()]
    final, seen_excl = [], set()
    by_name = {t.name: t for t in (user.tags or [])}
    created = False
    for name in submitted:
        name = name.strip()
        if name and name not in by_name and not user.tag_by_name(name):
            tag = Tag(name=name[:64])
            user.tags.append(tag)
            by_name[tag.name] = tag
            created = True
    if created:
        user.save()
    for name in submitted:
        tag = by_name.get(name)
        if tag and tag.parent:
            parent_tag = by_name.get(tag.parent)
            if parent_tag and parent_tag.exclusive_children:
                if tag.parent in seen_excl:
                    continue  # already picked one from this exclusive group
                seen_excl.add(tag.parent)
        final.append(name)
    return final


def _apply_tags(task: Item, user: User, raw_names) -> bool:
    """Normalise + apply a list of tag names to ``task``. True if a tag rule
    reassigned it.

    ``resolve_tag_names`` does the type-to-create and the exclusive groups;
    this stores the result and runs the tag-driven rules (auto-status,
    auto-assign)."""
    from application.services.label_rules import apply_label_rules
    # Snapshot the tags already on the task so the label rule below only
    # fires for tags added in *this* edit. Without this, a status-mapping
    # tag re-snaps the status on every save and you can never move the
    # task off that status by hand.
    previously = set(task.tags or [])
    final = resolve_tag_names(user, raw_names)
    task.tags = final
    # Label-driven rules for the tags added in this edit: ``maps_to_status``
    # snaps the task to that status (authoritative over any explicit status
    # the form supplied), ``assigns_to`` hands it to that person. Returns
    # whether the assignee moved, which the caller needs to keep an
    # untouched assignee picker from writing the old value back over it.
    before = task.assigned_to
    apply_label_rules(task, user, only_tags=set(final) - previously)
    return task.assigned_to != before


# What ``tasks/process_inbox.html`` actually renders. That triage form is a
# TRIMMED inspector, so its POST is indistinguishable from one sent by a user
# who hid half their sections — and every field it leaves out would arrive as
# an empty default and be written over the task. That is how filing a task in
# the inbox run unassigned it (visibility is owner-OR-assignee, so the assignee
# then got 404s), and cleared its estimate, location, repeat rule and
# follow-up. See tests/test_process_inbox_filing.py.
PROCESS_INBOX_FIELDS = frozenset({
    "title", "note", "project", "status", "priority", "flagged",
    "defer_date", "due_date", "plan_date", "tags",
})


def _apply_form(form: TaskForm, task: Item, user: User, *,
                fields: frozenset | None = None) -> None:
    from application.services.tz import attach_user_tz
    from application.services.inspector_layout import hidden_sections
    # The inspector only renders the sections the user kept visible, so a
    # hidden section's fields never reach the POST — and every assignment
    # below would then write the form's empty default over live data (an
    # absent ``project`` clears the parent, an absent ``assigned_to``
    # unassigns, an absent checkbox unflags). Presence in ``request.form``
    # can't tell the two apart: a visible-but-unchecked box is absent too.
    # So skip by section instead. ``title`` is pinned and always rendered.
    hidden = hidden_sections(user)

    def shown(section: str, *names: str) -> bool:
        """Did this POST actually carry ``names``? The section has to be one the
        user kept — AND, for a caller whose template is not the inspector, one
        of the fields that template renders (``fields``). A section is not the
        unit there: the triage form shows the priority but not the estimate,
        the project but not the assignee."""
        if section in hidden:
            return False
        return fields is None or all(n in fields for n in names)

    # The assignee the picker was rendered with. A tag's auto-assign rule
    # fires below, while the submitted select still carries the value from
    # before — so an untouched picker must not write that stale value back.
    shown_assignee = str(task.assigned_to.pk) if task.assigned_to else ""
    tag_assigned = False

    task.title = form.title.data
    if shown("note", "note"):
        task.note = form.note.data or ""
    if shown("status_project", "status"):
        task.status = form.status.data
    if shown("status_project", "flagged"):
        task.flagged = form.flagged.data
    if shown("status_project", "is_internal"):
        task.is_internal = form.is_internal.data
    if shown("priority_estimate", "priority"):
        task.priority = int(form.priority.data) if form.priority.data else None
    if shown("priority_estimate", "estimated_minutes"):
        task.estimated_minutes = form.estimated_minutes.data
    # ``datetime-local`` inputs come in as naive wall-clock values in the
    # user's local time — attach their tz so mongoengine doesn't treat
    # them as UTC and shift everything by the user's offset.
    if shown("dates", "defer_date"):
        task.defer_date = attach_user_tz(form.defer_date.data, user)
    if shown("dates", "due_date"):
        task.due_date = attach_user_tz(form.due_date.data, user)
    if shown("dates", "plan_date"):
        task.plan_date = attach_user_tz(form.plan_date.data, user)
    if shown("dates", "start_date", "end_date"):
        task.start_date = attach_user_tz(form.start_date.data, user)
        task.end_date = attach_user_tz(form.end_date.data, user)
    if shown("review", "waiting_on", "followup_at", "followup_every_days",
             "review_interval_days"):
        task.waiting_on = (form.waiting_on.data or "").strip()
        # Takes the defer date with it, when the defer date isn't saying
        # something else (Item.set_followup).
        task.set_followup(attach_user_tz(form.followup_at.data, user))
        task.followup_every_days = form.followup_every_days.data
        # Blank → None → inherit the project's review cadence; a number overrides.
        task.review_interval_days = form.review_interval_days.data
    if shown("location", "location"):
        task.location = (form.location.data or "").strip()
    if shown("repeat", "repeat_every_days", "repeat_unit",
             "repeat_from_completion", "repeat_weekdays", "repeat_monthday"):
        task.repeat_every_days = form.repeat_every_days.data
        task.repeat_unit = form.repeat_unit.data or "day"
        task.repeat_from_completion = form.repeat_from_completion.data
        task.repeat_weekdays = sorted(form.repeat_weekdays.data or [])
        # Fixed day-of-month is mutually exclusive with weekday recurrence in
        # the UI; a submitted monthday clears any weekday picks so the two
        # can't fight.
        task.repeat_monthday = form.repeat_monthday.data or None
        if task.repeat_monthday:
            task.repeat_weekdays = []
    # ``form.project.data`` should be a project pk string from the
    # select. Use the shared visibility helper so a tampered POST can't
    # reparent into a project the user doesn't own/share. Invalid /
    # inaccessible pks fall back to "no project" — same lenient
    # behaviour the form had before, but without the access leak.
    #
    # A SUBTASK's container is its parent TASK, which the project <select>
    # can't represent — so saving the form must NOT touch a subtask's parent
    # (an empty select would orphan it out of its parent). Moving a subtask
    # out to a project happens via drag/move, not this form.
    parent_is_task = (task.parent is not None
                      and getattr(task.parent, "kind", None) == "task")
    if shown("status_project", "project"):
        if not parent_is_task:
            from application.services.permissions import resolve_visible_item
            _dest = resolve_visible_item(user, form.project.data) if form.project.data else None
            # Role-aware: an assignee triaging a delegated task files it under
            # THEIR own project (assignee_parent), never the owner's parent.
            try:
                task.set_home_for(user, _dest)
            except ValueError:
                # Tampered POST picking a project the assignee doesn't own → Inbox.
                task.set_home_for(user, None)
    if shown("status_project", "tags"):
        tag_assigned = _apply_tags(task, user, form.tags.data)

    # Owner (Jira-style, separate from the assignee). Only an existing task has
    # an editable owner — the create form owns itself via the current user, and
    # doesn't render/submit this field.
    if form.owner.data and task.owner is not None \
            and form.owner.data != str(task.owner.pk):
        from application.services.permissions import resolve_assignable_user
        new_owner = resolve_assignable_user(user, form.owner.data)
        # The current assignee is always a valid owner, even without a shared team.
        if (new_owner is None and task.assigned_to is not None
                and form.owner.data == str(task.assigned_to.pk)):
            new_owner = task.assigned_to
        if new_owner is not None and new_owner != task.owner:
            old_owner = task.owner
            task.owner = new_owner
            # Keep the previous owner in the loop so an ownership change never
            # silently drops their access (they may be neither assignee nor member).
            if not any(m == old_owner for m in (task.members or [])):
                task.members = list(task.members or []) + [old_owner]
            # Owner == assignee is the degenerate "unassigned" state — clear it.
            if task.assigned_to is not None and task.assigned_to == new_owner:
                task.assign_to(None, by_user=user)

    if tag_assigned and (form.assigned_to.data or "") == shown_assignee:
        # A tag just delegated the task and the picker was left alone — the
        # tag is the newer instruction. Picking somebody else in the same
        # save still wins: that branch is only skipped for an untouched select.
        pass
    elif form.assigned_to.data:
        from application.services.permissions import resolve_assignable_user
        # Selecting the task's OWNER hands it back to them. The owner already
        # owns it, so that's an unassign (Item.assign_to collapses assign-to-owner
        # to None) — and it works even when the owner isn't in a shared team, so
        # an assignee can always return a delegated task.
        if task.owner is not None and form.assigned_to.data == str(task.owner.pk):
            if task.assigned_to is not None:
                task.assign_to(None, by_user=user)
        else:
            new_assignee = resolve_assignable_user(user, form.assigned_to.data)
            if new_assignee and task.assigned_to != new_assignee:
                task.assign_to(new_assignee, by_user=user)
            # Silently ignore non-shareable pks — the select choices are
            # already filtered, so reaching this branch with a bad id means
            # someone tampered with the POST.
    elif shown("assigned_to", "assigned_to"):
        # Empty select = explicit unassign, but only when the picker was
        # actually rendered — a section the user hid, or a form that simply
        # doesn't show the field, must not silently unassign.
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

    from application.services.permissions import inbox_q

    def queue():
        return list(
            Item.objects((_scope(user) & inbox_q(user)), kind="task",
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
    _populate_form_choices(form, user, task)
    if request.method == "GET":
        from application.services.tz import localise_form_dates
        localise_form_dates(form, user)
        form.project.data = str(task.project.pk) if task.project else ""
        form.tags.data = list(task.tags or [])
        form.assigned_to.data = str(task.assigned_to.pk) if task.assigned_to else ""
        form.owner.data = str(task.owner.pk) if task.owner else ""
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
    _populate_form_choices(form, user, task)
    if not form.validate_on_submit():
        flash("Couldn't save — please review the form.", "danger")
        return redirect(url_for("tasks.process_inbox", task_id=task_id))
    # Cursor into the queue: where we are *now*. The queue is ordered newest
    # first, so "the next task to process" is the first one strictly older than
    # the one we just handled — captured before save so it's stable even if the
    # task leaves the inbox (e.g. the user filed it under a project).
    cursor = task.created_at
    # The triage template renders a subset of the inspector's fields; say which,
    # so the absent ones are left alone instead of cleared (PROCESS_INBOX_FIELDS).
    _apply_form(form, task, user, fields=PROCESS_INBOX_FIELDS)
    _apply_smart_tokens(task, form.title.data, user)
    task.save()
    _writeback_calendar(task, user)
    # Advance *forward* through the queue (like Skip) rather than restarting at
    # the top — otherwise Save would re-present every task the user already
    # skipped past this pass.
    from application.services.permissions import inbox_q
    remaining = list(
        Item.objects((_scope(user) & inbox_q(user)), kind="task",
                     status__nin=["done", "dropped"])
        .order_by("-created_at")
    )
    nxt = next((t for t in remaining if t.created_at < cursor), None)
    if nxt is None:
        flash("Reached the end of the inbox — nice work.", "success")
        return redirect(url_for("dashboard.home"))
    return redirect(url_for("tasks.process_inbox", task_id=str(nxt.pk)))


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
        from application.services.permissions import inbox_q
        return qs.filter(inbox_q(user))
    if not pids:
        return qs.none()
    return qs.filter(parent__in=pids)


@tasks_bp.route("/inbox")
@login_required
def inbox():
    """Virtual default project — every task without an explicit project."""
    user = current_user._get_current_object()
    from application.services.permissions import inbox_q
    tasks = (Item.objects((_scope(user) & inbox_q(user)),
                          kind="task",
                          status__nin=["done", "dropped"])
             .order_by("-created_at"))
    return render_template(
        "tasks/list.html", title="Inbox", perspective="inbox", tasks=prepare_list(tasks)
    )

def _as_tz(dt, tz):
    """UTC-or-naive datetime → tz-aware in ``tz`` (None passes through)."""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


from application.services.prefetch import (
    prefetch_refs as _prefetch_refs,
    annotate_subtask_counts as _annotate_subtask_counts,
    prepare_list,
)


def _plan_mine_q(user):
    """Whose tasks the Plan view plans: mine unassigned + assigned to me. The
    Plan view is a personal day-planner, so a task delegated to somebody else
    stays out — unless ``plan_show_delegated`` is on, which brings back the
    ones I OWN (the same switch as the Next rail, so a delegated task dragged
    from that rail onto the day doesn't vanish there). Shared by the timeline,
    "Plan my day" and "Carry over", so all three agree on what's on the day."""
    mine = Q(assigned_to=None) | Q(assigned_to=user)
    if user.plan_show_delegated:
        mine = mine | Q(owner=user)
    return mine


def _plan_day_tasks(user, day_date, tz, *, is_today):
    """Actionable tasks for one day's Plan timeline: scope-visible, not
    done/dropped/someday, and either planned on that day, due by end of it
    (overdue counts), or flagged (only on the real today). Mirrors today()'s
    single-day plate for an arbitrary date so the timeline + auto-plan share
    one definition of "today's work"."""
    from datetime import time as _time
    day_start = datetime.combine(day_date, _time(0, 0)).replace(tzinfo=tz)
    next_day = day_start + timedelta(days=1)
    day_end = datetime.combine(day_date, _time(23, 59, 59)).replace(tzinfo=tz)
    # Push the date filter into Mongo instead of scanning every open task:
    # planned on this day OR due by end of it (overdue counts) OR a follow-up
    # due by end of it (overdue counts) OR flagged.
    cond = (Q(plan_date__gte=day_start, plan_date__lt=next_day)
            | Q(due_date__ne=None, due_date__lte=day_end)
            | Q(followup_at__ne=None, followup_at__lte=day_end))
    if is_today:
        cond = cond | Q(flagged=True)
    mine = _plan_mine_q(user)
    active = list(Item.objects(_scope(user), kind="task",
                               status__nin=["done", "dropped", "completed", "someday"])
                  .filter(cond).filter(mine))
    # Completed/dropped tasks that were planned (with a time) on this day stay
    # on the timeline struck through — _plan_classify only keeps the timed ones.
    done = list(Item.objects(_scope(user), kind="task",
                             status__in=["done", "completed", "dropped"],
                             plan_date__gte=day_start, plan_date__lt=next_day)
                .filter(mine))
    return active + done


def _plan_classify(tasks, events, tz, day_date, now_utc):
    """Split tasks + events for ``day_date`` into timeline buckets.

    Returns ``{timed, allday, backlog, busy}``:
      - timed: events + tasks planned on this day WITH a time-of-day, each
        ``{kind, obj, title, start, end, duration_min}`` (already placed).
      - allday: all-day events for the day (chips above the track).
      - backlog: tasks with no time yet (auto-plan candidates), not blocked.
      - busy: [(start, end)] intervals the auto-planner must avoid.
    """
    # Warm parent/assignee refs in bulk: is_blocked() below dereferences
    # each task's parent, which would otherwise N+1 down the candidate list.
    _prefetch_refs(tasks)
    timed, allday, backlog, busy = [], [], [], []
    for ev in events:
        s = _as_tz(ev.start, tz)
        if ev.all_day:
            if s and s.date() == day_date:
                allday.append({"kind": "event", "obj": ev, "title": ev.title})
            continue
        e = _as_tz(ev.end, tz) or s
        if not s or s.date() != day_date:
            continue
        dur = max(5, int((e - s).total_seconds() // 60)) if e and e > s else 30
        timed.append({"kind": "event", "obj": ev, "start": s, "end": e,
                      "duration_min": dur, "title": ev.title})
        busy.append((s, e))
    for t in tasks:
        pl = _as_tz(t.plan_date, tz)
        dur = t.estimated_minutes or 30
        is_done = t.status in ("done", "completed", "dropped")
        if pl and pl.date() == day_date and (pl.hour or pl.minute):
            end = pl + timedelta(minutes=dur)
            timed.append({"kind": "task", "obj": t, "start": pl, "end": end,
                          "duration_min": dur, "title": t.title, "done": is_done})
            # A completed task stays on the timeline (struck through) but no
            # longer blocks its slot for auto-plan.
            if not is_done:
                busy.append((pl, end))
        elif is_done:
            continue  # done with no time-of-day → don't clutter the backlog
        elif not t.is_blocked(now_utc):
            backlog.append({"kind": "task", "obj": t, "title": t.title,
                            "duration_min": dur})
    timed.sort(key=lambda x: x["start"])
    return {"timed": timed, "allday": allday, "backlog": backlog, "busy": busy}


def _assign_columns(items):
    """Lay overlapping timeline items side by side. Mutates each item dict
    with ``col`` (0-based slot) and ``cols`` (slots in its overlap cluster),
    so two tasks at the same time render next to each other instead of
    stacking invisibly on top of one another."""
    items = sorted(items, key=lambda x: (x["start"], x["end"]))
    cluster, cluster_end = [], None

    def _flush(group):
        col_ends = []  # running end time of each column
        for it in group:
            for ci, ce in enumerate(col_ends):
                if ce <= it["start"]:
                    col_ends[ci] = it["end"]
                    it["col"] = ci
                    break
            else:
                it["col"] = len(col_ends)
                col_ends.append(it["end"])
        for it in group:
            it["cols"] = len(col_ends)

    for it in items:
        if cluster and it["start"] >= cluster_end:
            _flush(cluster)
            cluster, cluster_end = [], None
        cluster.append(it)
        cluster_end = it["end"] if cluster_end is None else max(cluster_end, it["end"])
    if cluster:
        _flush(cluster)
    return items


def _next_actions_pool(user, now, seen_pks, limit=None):
    """Actionable ``status="next"`` tasks not already on the day.

    Shared by the Plan timeline and the single-day List view: a
    prefetch-warmed slice of the user's next-actions (owned or assigned to
    them), minus anything already shown (``seen_pks``) and anything blocked
    by an incomplete dependency/parent. Returns the same pill dicts the
    templates expect (``obj`` / ``title`` / ``duration_min``).

    **The rail is not capped.** It used to stop at 50 pills behind a
    200-document query window, which silently left next-actions off the page —
    and the rail's own counter then reported the truncated number, so nothing
    said work had been held back. A next-action nobody can see is one that
    doesn't get done, and there is no load argument for the ceiling either:
    ``/tasks/next`` already renders the same set unbounded off the same
    ``owner+status`` / ``assigned_to+status`` index. ``limit`` stays for a
    caller that genuinely wants a preview.

    Tasks you own but DELEGATED to somebody else are left out unless
    ``user.plan_show_delegated`` is on (then each pill carries
    ``delegated_to``). Returns ``(pool, delegated)`` — the second is how many
    delegated tasks there are, shown or not, so the rail can offer the switch.
    """
    candidates = list(
        Item.objects(_scope(user), kind="task", status="next")
        .filter(Q(assigned_to=None) | Q(assigned_to=user) | Q(owner=user))
        .order_by("+sort_order", "+due_date")
    )
    _prefetch_refs(candidates)
    pool = []
    delegated = 0
    for t in candidates:
        if t.pk in seen_pks or t.is_blocked(now):
            continue
        to = t.assigned_to if t.assigned_to and t.assigned_to != user else None
        if to is not None:
            delegated += 1
            if not user.plan_show_delegated:
                continue
        pool.append({"obj": t, "title": t.title,
                     "duration_min": t.estimated_minutes or 30,
                     "delegated_to": to})
        if limit is not None and len(pool) >= limit:
            break
    return pool, delegated


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
    _today_tz = resolve_tz(user)
    today_date = now.astimezone(_today_tz).date()

    # Window selection ------------------------------------------------
    raw_date = (request.args.get("date") or "").strip()
    try:
        start_date = (
            _date.fromisoformat(raw_date) if raw_date else today_date
        )
    except ValueError:
        start_date = today_date
    # Window length (duration): use the URL when given and remember it on the
    # user; otherwise fall back to their last-used value.
    if request.args.get("days") is not None:
        try:
            days = int(request.args.get("days") or "1")
        except (TypeError, ValueError):
            days = 1
        days = max(1, min(days, 31))
        if days != (user.today_days or 1):
            user.today_days = days
            user.save()
    else:
        days = max(1, min(int(user.today_days or 1), 31))

    # Bucket the day window in the user's tz, otherwise a task planned
    # for 23:00 Berlin (= 21:00 UTC) on day D would fall outside a
    # ``date(D)`` UTC-midnight window — the row showing day D would miss
    # its own task and it would re-appear on day D+1.
    _u_tz = resolve_tz(user)
    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=_u_tz)
    end_dt = start_dt + timedelta(days=days)
    is_today_now = (start_date == today_date and days == 1)

    # Tasks -----------------------------------------------------------
    # kind="task" is load-bearing: project-like items carry due/plan dates
    # too, and without it a project would leak into the task plate AND show
    # as its own header — the same item rendered twice. (A recurring task
    # that was turned into a project is exactly such an item.)
    # Today is a personal action list: by default scope to tasks the user owns
    # or that are currently assigned to them (still honouring focus mode) — NOT
    # the wide ``_scope``, so team shares / stale membership after a
    # re-delegation don't surface a task now owned by AND assigned to someone
    # else. The toolbar toggle opts into showing shared work. See
    # _perspective_scope / application/services/today.py.
    qs = (
        Item.objects(
            _perspective_scope(user),
            kind="task",
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
        kind__in=["project", "section"],
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
            if not (t.defer_date and t.defer_date > now)
            and (
                (t.due_date and t.due_date <= today_end)
                or t.flagged
                or (t.plan_date and local_date(t.plan_date, _today_tz) <= today_date)
                or (t.defer_date and t.defer_date <= now and t.status == "next")
                or (t.followup_at and t.followup_at <= today_end)
            )
        ]
        # Flag tasks that earned their spot ONLY through a pending follow-up
        # (no due/flag/plan/defer reason). The list toolbar can hide these.
        for t in filtered:
            other = bool(
                (t.due_date and t.due_date <= today_end)
                or t.flagged
                or (t.plan_date and local_date(t.plan_date, _today_tz) <= today_date)
                or (t.defer_date and t.defer_date <= now and t.status == "next")
            )
            t._followup_only = bool(
                t.followup_at and t.followup_at <= today_end and not other
            )
        projects_with_dates = [
            p for p in proj_qs
            if (p.due_date and p.due_date <= today_end)
            or (p.plan_date and local_date(p.plan_date, _today_tz) <= today_date)
        ]
    else:
        # Multi-day window [start_dt, end_dt). When the window starts TODAY,
        # drop the lower bound so overdue / past-planned actionable items (and
        # flagged) stay visible instead of silently vanishing the moment the
        # window widens past one day. A window paged to another week keeps both
        # bounds. (daily_buckets below folds overdue into the first day.)
        lower = None if start_date == today_date else start_dt

        def _hit(d):
            return (d is not None and (lower is None or d >= lower)
                    and d < end_dt)

        filtered = [
            t for t in qs
            if _hit(t.due_date) or _hit(t.plan_date) or _hit(t.followup_at)
            or (lower is None and t.flagged)
        ]
        for t in filtered:
            other = bool(_hit(t.due_date) or _hit(t.plan_date)
                         or (lower is None and t.flagged))
            t._followup_only = bool(_hit(t.followup_at) and not other)
        projects_with_dates = [
            p for p in proj_qs
            if _hit(p.due_date) or _hit(p.plan_date)
        ]

    # Batch-resolve parent/assignee refs once so the row template doesn't
    # dereference them one-by-one (N+1) while iterating, and batch-count
    # subtasks for the row badge in a single query.
    _prefetch_refs(filtered + projects_with_dates)
    _annotate_subtask_counts(filtered)

    # Grouping — same options as the project detail view; persisted on the user.
    grouping = (request.args.get("group") or user.today_grouping or "project").strip()
    if grouping not in ("none", "status", "priority", "tag", "due", "project",
                        "customer"):
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
    elif grouping == "customer":
        groups = _group_by_customer(_merge_unique(filtered, projects_with_dates))
    elif grouping == "none":
        merged = _merge_unique(filtered, projects_with_dates)
        groups = [("Today", "bi-calendar-day", merged, None)] if merged else []
    else:
        merged = _merge_unique(filtered, projects_with_dates)
        # Priority grouping keeps every level on-screen (even empty ones) so
        # each is a drop target for drag-to-set-priority.
        groups = _group_tasks(merged, grouping, keep_empty=(grouping == "priority"))

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
    # ``[start_dt, end_dt)`` is already the window in the USER's zone, so it is
    # what the page shows. The fetch is widened by FETCH_SLACK only because an
    # all-day event encodes its date as midnight UTC and would otherwise fall
    # outside its own local day; ``events_in_window`` then cuts the slack back
    # off. Without that second step the slack itself leaked — a Berlin user saw
    # yesterday evening's appointments listed under Today.
    from application.services.calendar_sync import (
        FETCH_SLACK, collect_next_event, event_day, events_in_window,
        fetch_events,
    )
    events = []
    next_event = None
    for cal in (user.calendars or []):
        if not cal.show_in_today:
            continue
        try:
            events.extend(fetch_events(cal, start_dt - FETCH_SLACK,
                                       end_dt + FETCH_SLACK, user=user))
        except Exception as exc:  # noqa: BLE001
            flash(f"{cal.name}: {exc}", "warning")
    events = events_in_window(events, start_dt, end_dt)
    events.sort(key=lambda e: e.start)
    if not events and is_today_now:
        try:
            next_event = collect_next_event(user)
        except Exception:
            next_event = None

    # How many notes are linked to each visible event — one query for the
    # whole day (never per-event in the template). Drives the badge on the
    # "notes for this event" button in the calendar/plan/list views.
    # The same pass also collects each note's "## Briefing" section, so an
    # event can carry its prep text inline without opening the note (see
    # application/services/briefing.py).
    event_note_counts = {}
    event_briefings = {}
    uids = list({ev.uid for ev in events if ev.uid})
    if uids:
        from application.services.briefing import extract_briefing
        for n in Item.objects(owner=user, kind="note",
                              linked_events__uid__in=uids).only("linked_events", "note"):
            brief = extract_briefing(n.note or "")
            for le in (n.linked_events or []):
                if le.uid in uids:
                    event_note_counts[le.uid] = event_note_counts.get(le.uid, 0) + 1
                    if brief:
                        event_briefings.setdefault(le.uid, []).append(brief)
        # Two notes briefing the same event → both, in one block.
        event_briefings = {u: "\n\n".join(b) for u, b in event_briefings.items()}

    # How many linked NOTES each shown task has, for its note button badge
    # (one batched query — see Item.note_counts_for).
    task_note_counts = Item.note_counts_for(filtered + projects_with_dates)

    # Group events by date when window spans more than one day, so the
    # template can render section headers per day.
    events_by_day = {}
    if days > 1:
        _day_tz = resolve_tz(user)
        for ev in events:
            events_by_day.setdefault(event_day(ev, _day_tz), []).append(ev)

    # Multi-day LIST view: mix events and tasks into one list per day,
    # sorted by start time. Lets the user see "Tuesday: 09:00 standup,
    # 10:30 do bug fix, 14:00 dentist" without flipping to calendar
    # mode. Skipped for single-day windows (the existing groupings are
    # plenty) and for calendar mode (the timeline already interleaves).
    #
    # Only the "none" grouping gets the per-day split — it's the neutral
    # "just show me each day" view. When the user has explicitly chosen a
    # grouping (priority / status / tag / project / due), that choice wins
    # across the whole window: ``groups`` already spans the full range, so
    # we leave ``daily_buckets`` unset and let the grouped sections render.
    daily_buckets = None
    if days > 1 and grouping == "none":
        _list_tz = resolve_tz(user)

        def _to_user_list(dt):
            return _as_tz(dt, _list_tz)

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
            day_key = event_day(ev, _list_tz)
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
            anchor = _to_user_list(t.plan_date or t.due_date or t.followup_at)
            day_key = anchor.date() if anchor else start_date
            # Overdue / past-dated → fold into the first day (today) so it's
            # never dropped silently. Only days inside the window have buckets.
            if day_key < start_date:
                day_key = start_date
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
            if day_key < start_date:
                day_key = start_date
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
    # Layout (list/calendar): use the URL when given and remember it on the
    # user; otherwise fall back to their last-used value.
    if request.args.get("view") is not None:
        view_mode = (request.args.get("view") or "list").strip()
        if view_mode not in ("list", "calendar", "plan"):
            view_mode = "list"
        if view_mode != (user.today_view_mode or "list"):
            user.today_view_mode = view_mode
            user.save()
    else:
        view_mode = user.today_view_mode if user.today_view_mode in ("list", "calendar", "plan") else "list"

    timeline = None
    if view_mode == "calendar":
        # Bucket events/tasks in the user's configured timezone so
        # they land on the correct day + hour. zoneinfo falls back to
        # UTC if the name is unset or invalid.
        user_tz = resolve_tz(user)

        def _to_user(dt):
            return _as_tz(dt, user_tz)

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
                "title": t.title, "done": False,
            })
        # Completed/dropped tasks that were planned in this window stay on the
        # grid, struck through (filtered excludes them as it drives the list).
        for t in Item.objects(_scope(user), kind="task",
                              status__in=["done", "completed", "dropped"],
                              plan_date__gte=start_dt, plan_date__lt=end_dt):
            dt = _to_user(t.plan_date)
            if not dt:
                continue
            timed_items.append({
                "kind": "task", "obj": t, "start": dt,
                "end": dt + timedelta(minutes=t.estimated_minutes or 30),
                "duration_min": t.estimated_minutes or 30,
                "title": t.title, "done": True,
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

        now_local = now.astimezone(user_tz)
        timeline = {
            "hours": list(range(lo, hi)),
            "days": days_in_window,
            "by_day": by_day,
            "today": today_date,            # real today, for column/now-line marking
            "now_hour": now_local.hour,
            "now_minute": now_local.minute,
        }

    # Single-column "Plan" timeline (structured.app-style day planner).
    plan_data = None
    if view_mode == "plan":
        # Plan is always single-day; decouple from the saved multi-day window
        # so a user whose list view is on "3 d" still gets a clean day planner.
        plan_is_today = (start_date == today_date)
        cls = _plan_classify(
            _plan_day_tasks(user, start_date, _u_tz, is_today=plan_is_today),
            events, _u_tz, start_date, now,
        )
        # One chronological column (structured.app-style). Overlapping items
        # — including a task that clashes with an event — split into
        # side-by-side columns only where they actually overlap.
        timed = _assign_columns(cls["timed"])
        # "Next actions" pool — actionable next-tasks that aren't already on
        # the day (no time, not in the auto-plan backlog). Draggable onto the
        # track but deliberately NOT auto-planned, so the user can pull extra
        # work in by hand without "Plan my day" hoovering it all up.
        seen_pks = ({c["obj"].pk for c in cls["backlog"]}
                    | {it["obj"].pk for it in timed if it["kind"] == "task"})
        next_pool, next_delegated = _next_actions_pool(user, now, seen_pks)
        # Deadline markers: tasks in today's plate whose due date lands on
        # this day get a line on the timeline at their due time.
        due_raw = {}  # minute-of-day → [titles]
        for src in (timed,
                    [{"obj": c["obj"]} for c in cls["backlog"]],
                    [{"obj": c["obj"]} for c in next_pool]):
            for it in src:
                t = it["obj"]
                if it.get("kind") == "event":
                    continue
                du = _as_tz(getattr(t, "due_date", None), _u_tz)
                if du and du.date() == start_date:
                    due_raw.setdefault(du.hour * 60 + du.minute, []).append(t.title)

        start_h = user.plan_day_start if user.plan_day_start is not None else 9
        end_h = user.plan_day_end if user.plan_day_end is not None else 18
        # Render range = the working window. It only ever extends LATER (for
        # any timed item or due marker that runs past the day end). The START
        # stays at the working hour and is pulled earlier ONLY by a task the
        # user actually planned before it — so an odd 02:00 calendar entry
        # doesn't make the whole planner begin at 2am.
        lo_min, hi_min = start_h * 60, end_h * 60
        for it in cls["timed"]:
            s, e = it["start"], it["end"]
            if it["kind"] == "task":
                lo_min = min(lo_min, s.hour * 60 + s.minute)
            hi_min = max(hi_min, e.hour * 60 + e.minute)
        for dm in due_raw:
            hi_min = max(hi_min, dm)
        lo_min = (lo_min // 60) * 60
        hi_min = min(24 * 60, ((hi_min + 59) // 60) * 60)
        due_markers = [
            {"min": m, "label": " · ".join(titles[:3])}
            for m, titles in sorted(due_raw.items())
        ]
        now_min = None
        carryover_count = 0
        if plan_is_today:
            n_local = now.astimezone(_u_tz)
            now_min = n_local.hour * 60 + n_local.minute
            # Unfinished tasks stranded in the past (offered as "carry over").
            from datetime import time as _time2
            day_start_local = datetime.combine(start_date, _time2(0, 0)).replace(tzinfo=_u_tz)
            carryover_count = Item.objects(
                _scope(user), kind="task",
                status__nin=["done", "dropped", "someday"],
                plan_date__ne=None, plan_date__lt=day_start_local,
            ).filter(_plan_mine_q(user)).count()
        # How the rails are split up. Persisted like the view mode itself, so
        # the planner comes back the way it was left.
        pgroup = (request.args.get("pgroup") or user.plan_grouping or "none").strip()
        if pgroup not in ("none", "project", "customer"):
            pgroup = "none"
        if pgroup != user.plan_grouping:
            user.plan_grouping = pgroup
            user.save()
        plan_data = {
            "timed": timed,
            "allday": cls["allday"],
            "backlog": cls["backlog"],
            "next_pool": next_pool,
            "next_delegated": next_delegated,
            "grouping": pgroup,
            "backlog_groups": _group_rail(cls["backlog"], pgroup),
            "next_groups": _group_rail(next_pool, pgroup),
            "due_markers": due_markers,
            "carryover_count": carryover_count,
            "range_start_min": lo_min,
            "range_end_min": hi_min,
            "work_start_min": start_h * 60,
            "work_end_min": end_h * 60,
            "now_min": now_min,
            "date": start_date.isoformat(),
        }

    # Single-day List view gets the same draggable "Next actions" pool the
    # Plan timeline has: next-marked tasks not already on the plate, dragged
    # up onto the list to plan them for the day being viewed (POST /schedule).
    list_next_pool, list_next_delegated = [], 0
    if view_mode == "list" and days == 1:
        list_next_pool, list_next_delegated = _next_actions_pool(
            user, now, {t.pk for t in filtered})

    return render_template(
        "tasks/today.html",
        title=title,
        list_next_pool=list_next_pool,
        list_next_delegated=list_next_delegated,
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
        plan_data=plan_data,
        event_note_counts=event_note_counts,
        event_briefings=event_briefings,
        task_note_counts=task_note_counts,
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

def _group_rail(entries, mode):
    """Split a Plan-view rail's pills into sections.

    Returns ``[(label, icon, [entry])]``. ``mode="none"`` gives one unlabelled
    section, so the template renders exactly one shape either way. The bucket
    for pills that resolve to nothing ("Inbox" / "No customer") goes last.

    Only the RAILS are grouped. The timeline stays chronological — a day
    planner sorted by customer is not a plan, it is a list.
    """
    entries = list(entries or [])
    if mode not in ("project", "customer") or not entries:
        return [(None, None, entries)]
    objs = [e["obj"] for e in entries]
    if mode == "customer":
        from application.services.containers import customer_labels
        labels = customer_labels(objs)
        icon, fallback = "bi-building", "No customer"
    else:
        from application.services.containers import container_paths
        labels = container_paths(objs)
        icon, fallback = "bi-folder", "Inbox"
    buckets: dict = {}
    rest: list = []
    for e in entries:
        name = labels.get(str(e["obj"].pk))
        if name:
            buckets.setdefault(name, []).append(e)
        else:
            rest.append(e)
    groups = [(name, icon, lst)
              for name, lst in sorted(buckets.items(), key=lambda kv: kv[0].lower())]
    if rest:
        groups.append((fallback, "bi-inbox" if mode == "project" else "bi-dash-circle",
                       rest))
    return groups


def _group_by_customer(items):
    """Group Today's work by the CUSTOMER its subtree belongs to.

    A folder/project either names its customer (``Item.customer``) or is bound
    to a billing project whose customer that is — either way the answer is
    inherited down the subtree (``services.containers.customer_labels``).
    Anything whose tree says nothing lands in one bucket at the end — for a
    personal account that is everything, which is why the Today toolbar only
    offers this grouping to somebody on a team.

    ``target_project_id`` is ``None`` for every bucket: a customer is not a
    place, so dropping a task on one may reorder but must never reparent.
    """
    from application.services.containers import customer_labels
    items = list(items)
    labels = customer_labels(items)
    by_customer: dict = {}
    unbilled: list = []
    for it in items:
        name = labels.get(str(it.pk))
        if name:
            by_customer.setdefault(name, []).append(it)
        else:
            unbilled.append(it)
    groups = [(name, "bi-building", lst, None)
              for name, lst in sorted(by_customer.items(),
                                      key=lambda kv: kv[0].lower())]
    if unbilled:
        groups.append(("No customer", "bi-dash-circle", unbilled, None))
    return groups


@tasks_bp.route("/next")
@login_required
def next_actions():
    """All Next-tagged tasks. Blocked (deferred or sequentially-blocked)
    entries are kept in the list but rendered greyed out, so the user
    can still see what's coming next."""
    user = current_user._get_current_object()
    tasks = _filter_by_project(
        Item.objects(_perspective_scope(user), status="next"), user,
    ).order_by("+due_date", "+sort_order")
    return render_template(
        "tasks/list.html", title="Next", perspective="next", tasks=prepare_list(tasks)
    )

@tasks_bp.route("/forecast")
@login_required
def forecast():
    user = current_user._get_current_object()
    horizon = datetime.now(timezone.utc) + timedelta(days=14)
    tasks = _filter_by_project(
        Item.objects(
            _perspective_scope(user),
            status__nin=["done", "dropped", "someday"],
            due_date__lte=horizon,
        ),
        user,
    ).order_by("+due_date")
    return render_template(
        "tasks/list.html", title="Forecast (14d)", perspective="forecast", tasks=prepare_list(tasks)
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
    tasks = _filter_by_project(
        Item.objects(
            _perspective_scope(user),
            status__nin=["done", "dropped"],
            defer_date__gt=now,
        ),
        user,
    ).order_by("+defer_date")
    return render_template(
        "tasks/list.html", title="Scheduled", perspective="scheduled", tasks=prepare_list(tasks)
    )

@tasks_bp.route("/waiting")
@login_required
def waiting():
    user = current_user._get_current_object()
    tasks = _filter_by_project(
        Item.objects(_perspective_scope(user), status="waiting"), user,
    ).order_by("+due_date")
    return render_template(
        "tasks/list.html", title="Waiting", perspective="waiting", tasks=prepare_list(tasks)
    )

@tasks_bp.route("/follow-up")
@login_required
def followup():
    """Follow-ups — anything with a follow-up date set, soonest (most overdue)
    first. Driven by ``followup_at`` rather than a status, so a task can be e.g.
    ``waiting`` AND carry a follow-up cadence at the same time."""
    user = current_user._get_current_object()
    tasks = _filter_by_project(
        Item.objects(_perspective_scope(user), kind="task", followup_at__ne=None,
                     status__nin=["done", "dropped"]),
        user,
    ).order_by("+followup_at")
    return render_template(
        "tasks/list.html", title="Follow-up", perspective="followup",
        tasks=prepare_list(tasks),
    )

@tasks_bp.route("/waiting-reply")
@login_required
def waiting_reply():
    """Tasks where you've asked someone and are waiting on their reply."""
    user = current_user._get_current_object()
    tasks = _filter_by_project(
        Item.objects(_perspective_scope(user), status="waiting_reply"), user,
    ).order_by("+due_date")
    return render_template(
        "tasks/list.html", title="Waiting on reply", perspective="waiting_reply",
        tasks=prepare_list(tasks),
    )

@tasks_bp.route("/review")
@login_required
def review():
    """Tasks parked for review before they can be closed out."""
    user = current_user._get_current_object()
    tasks = _filter_by_project(
        Item.objects(_perspective_scope(user), status="review"), user,
    ).order_by("+due_date")
    return render_template(
        "tasks/list.html", title="Review", perspective="review",
        tasks=prepare_list(tasks),
    )

@tasks_bp.route("/flagged")
@login_required
def flagged():
    user = current_user._get_current_object()
    tasks = _filter_by_project(
        Item.objects(_perspective_scope(user), status__nin=["done", "dropped"], flagged=True),
        user,
    ).order_by("+due_date")
    return render_template(
        "tasks/list.html", title="Flagged", perspective="flagged", tasks=prepare_list(tasks)
    )

@tasks_bp.route("/someday")
@login_required
def someday():
    user = current_user._get_current_object()
    tasks = _filter_by_project(
        Item.objects(_perspective_scope(user), status="someday"), user,
    ).order_by("-updated_at")
    return render_template(
        "tasks/list.html", title="Someday / Maybe", perspective="someday", tasks=prepare_list(tasks)
    )

@tasks_bp.route("/perspectives/show-shared", methods=["POST"])
@login_required
def toggle_show_shared():
    """Flip the per-user 'include shared/team tasks in perspectives' preference,
    then bounce back to the perspective the toggle was flipped from."""
    user = current_user._get_current_object()
    user.perspectives_show_shared = request.form.get("show") == "1"
    user.save()
    return redirect(request.form.get("next") or request.referrer
                    or url_for("dashboard.home"))


@tasks_bp.route("/today/show-delegated", methods=["POST"])
@login_required
def toggle_show_delegated():
    """Flip whether the Next-actions rail on Today / Plan shows tasks you
    delegated to somebody else, then go back to the page it was flipped on."""
    user = current_user._get_current_object()
    user.plan_show_delegated = request.form.get("show") == "1"
    user.save()
    return redirect(request.form.get("next") or request.referrer
                    or url_for("tasks.today"))


@tasks_bp.route("/assigned")
@login_required
def assigned_to_me():
    user = current_user._get_current_object()
    from application.services import focus
    tasks = _filter_by_project(
        Item.objects(focus.restrict(Q(assigned_to=user), user),
                     status__nin=["done", "dropped"]),
        user,
    ).order_by("+due_date")
    return render_template(
        "tasks/list.html", title="Assigned to me", perspective="assigned", tasks=prepare_list(tasks)
    )


@tasks_bp.route("/delegated")
@login_required
def delegated():
    """Tasks I handed off to someone else — the mirror of 'Assigned to me'.

    Two ways to have delegated a task: I own it and assigned it away, OR I
    was the one who assigned it (``assigned_by``) even though someone else
    owns it — e.g. a task assigned to me that I then re-delegated onward.
    Without the ``assigned_by`` arm such a task vanishes from my views until
    it's handed back. ``assigned_to__ne=user`` alone would also match
    unassigned tasks (None != user), so we exclude None explicitly."""
    user = current_user._get_current_object()
    from application.services import focus
    tasks = _filter_by_project(
        Item.objects(
            focus.restrict(
                (Q(owner=user) | Q(assigned_by=user))
                & Q(assigned_to__ne=None) & Q(assigned_to__ne=user),
                user),
            status__nin=["done", "dropped"]),
        user,
    ).order_by("+due_date")
    return render_template(
        "tasks/list.html", title="Assigned to others", perspective="delegated",
        tasks=prepare_list(tasks)
    )

@tasks_bp.route("/completed")
@login_required
def completed():
    user = current_user._get_current_object()
    # Tasks close as "done", projects as "completed" — show both so a
    # project that auto-completed (or was completed by hand) appears here
    # alongside the tasks. Paginated + grouped by completion date.
    from collections import OrderedDict
    PAGE_SIZE = 50
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    base = _filter_by_project(
        Item.objects(_perspective_scope(user), status__in=["done", "completed"]), user,
    ).order_by("-completed_at")
    total = base.count()
    items = prepare_list(base.skip((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))

    tz = resolve_tz(user)
    today = datetime.now(tz).date()

    def _label(d):
        if d is None:
            return "No completion date"
        delta = (today - d).days
        if delta == 0:
            return "Today"
        if delta == 1:
            return "Yesterday"
        return d.strftime("%A, %-d %b %Y")

    buckets = OrderedDict()
    for t in items:
        d = t.completed_at.astimezone(tz).date() if t.completed_at else None
        buckets.setdefault(d, []).append(t)
    date_groups = [{"label": _label(d), "tasks": ts} for d, ts in buckets.items()]

    return render_template(
        "tasks/completed.html", title="Completed", perspective="completed",
        date_groups=date_groups, count=len(items), total=total,
        page=page, has_prev=page > 1, has_next=page * PAGE_SIZE < total,
        page_size=PAGE_SIZE,
    )

@tasks_bp.route("/search")
@login_required
def search():
    user = current_user._get_current_object()
    q = (request.args.get("q") or "").strip()
    if not q:
        return render_template("tasks/list.html", title="Search", perspective="search", tasks=[])
    hits = _scope(user) & (
        Q(title__icontains=q)
        | Q(note__icontains=q)
        | Q(external_ref__icontains=q)
    )
    tasks = _filter_by_project(
        Item.objects(hits, kind="task"), user,
    ).order_by("-updated_at").limit(200)
    # Notes match on the same terms but are a different kind of result — they
    # get their own strip above the task list rather than being rendered as
    # rows in it (they have no due date, project or status to show).
    notes = list(Item.objects(hits, kind="note")
                 .only("pk", "title", "updated_at")
                 .order_by("-updated_at").limit(5))
    note_total = Item.objects(hits, kind="note").count() if notes else 0
    return render_template(
        "tasks/list.html",
        title=f"Search: {q}",
        perspective="search",
        tasks=tasks,
        note_hits=notes,
        note_total=note_total,
        query=q,
    )


@tasks_bp.route("/quickjump")
@login_required
def quickjump():
    """JSON endpoint backing the cmd-K-style command bar.

    Returns up to 20 items matching ``q`` by title. Projects come first so
    navigation by project name is reliable.

    ``?kinds=`` picks what may come back. It defaults to projects + tasks
    because most callers (the Related and Dependency pickers) can only accept
    those — the server drops anything else, so offering a note there would be
    a result that silently does nothing. The command bar opts notes in.

    Every hit carries its ``status`` and the label for it: the search mixes
    every status across every project, so a finished task that looks exactly
    like an open one is a hit you have to open to understand."""
    from flask import jsonify
    from application.views.filters import STATUS_LABELS
    user = current_user._get_current_object()
    q = (request.args.get("q") or "").strip()
    kinds = {k for k in (request.args.get("kinds") or "").split(",") if k}
    if not kinds:
        kinds = {"project", "folder", "task"}
    if not q:
        return jsonify({"results": []})
    base = _scope(user) & (
        Q(title__icontains=q) | Q(external_ref__icontains=q)
    )
    want = [k for k in ("project", "folder") if k in kinds]
    projects = list(
        Item.objects(base, kind__in=want).order_by("+name").limit(8)
    ) if want else []
    tasks = list(
        Item.objects(base, kind="task").order_by("-updated_at").limit(12)
    ) if "task" in kinds else []
    notes = list(
        Item.objects(base, kind="note").order_by("-updated_at").limit(6)
    ) if "note" in kinds else []
    out = []
    def _status(item):
        return {"status": item.status,
                "status_label": STATUS_LABELS.get(item.status, item.status)}

    for p in projects:
        out.append({
            "id": str(p.pk),
            "kind": "folder" if p.is_folder else "project",
            "title": p.title,
            "url": url_for("projects.detail", project_id=str(p.pk)),
            "subtitle": (p.parent.title if p.parent else ""),
            **_status(p),
        })
    for n in notes:
        out.append({
            "id": str(n.pk),
            "kind": "note",
            "title": n.title,
            "url": url_for("notes.detail", note_id=str(n.pk)),
            "subtitle": (n.parent.title if n.parent else "Notes"),
        })
    for t in tasks:
        out.append({
            "id": str(t.pk),
            "kind": "task",
            "title": t.title,
            "url": url_for("tasks.detail", task_id=str(t.pk)),
            "subtitle": (t.parent.title if t.parent else "Inbox"),
            **_status(t),
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
                html = _render_empty_task_inspector(
                    user,
                    parent_project=task.project,
                    just_added_title=task.title,
                )
            else:
                # Re-render with the saved task so the inspector stays open and
                # the next submit goes to /edit, not /new.
                html = _render_task_inspector(task, user, just_saved=True)
            # Surface the new id so the client can upload held attachments to it
            # (the "create then attach" flow) — works even on add-another, whose
            # fragment is a fresh empty form without the id.
            resp = Response(html)
            resp.headers["X-Created-Task-Id"] = str(task.pk)
            # "Open after creating" wins only when the user isn't also
            # queueing another task — the two are contradictory (one navigates
            # away, the other keeps the form open for the next entry).
            if form.open_after_create.data and not form.add_another.data:
                resp.headers["X-Open-Created-Task"] = "1"
            return resp
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
    if not task:
        abort(404)
    if not task.can_be_viewed_by(user):
        abort(403)
    # Mirror of the projects.detail redirect — if the pk actually
    # points at a project/folder/section, hand off to the right view
    # so the same item never renders as both kinds.
    if task.kind != "task":
        return redirect(url_for("projects.detail", project_id=task_id))
    from application.views.projects import _ancestor_chain
    return render_template(
        "tasks/detail.html",
        task=task,
        ancestors=_ancestor_chain(task, viewer=user),
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
    _populate_form_choices(form, user, task)

    if request.method == "GET":
        from application.services.tz import localise_form_dates
        localise_form_dates(form, user)
        # Surface a container pk in the select. If the task's actual
        # parent is a section, hop up to the section's owning project
        # so there's a matching option to highlight (and so a POST
        # without changes round-trips cleanly).
        #
        # A FOLDER counts, and must: the select offers folders
        # (_populate_form_choices), so leaving it empty for a folder-held task
        # showed "—" and the very next save filed the task into the Inbox —
        # it left its folder without anyone asking.
        proj_pk = ""
        if task.project is not None:
            p = task.project
            depth = 0
            while p is not None and p.kind == "section" and depth < 6:
                p = p.parent
                depth += 1
            if p is not None and p.kind in ("project", "folder"):
                proj_pk = str(p.pk)
        form.project.data = proj_pk
        form.tags.data = list(task.tags or [])
        form.assigned_to.data = str(task.assigned_to.pk) if task.assigned_to else ""
        form.owner.data = str(task.owner.pk) if task.owner else ""
        form.priority.data = str(task.priority) if task.priority else ""

    if form.validate_on_submit():
        _apply_form(form, task, user)
        _apply_smart_tokens(task, form.title.data, user)
        task.save()
        _writeback_calendar(task, user)
        # An assignee who isn't the owner can reassign the task away from
        # themselves — which drops them out of the visibility scope (owner OR
        # assigned_to). Don't then bounce them to a detail page they can no
        # longer load (that 404 is exactly what users hit): send them back to
        # the inbox. They can hit "Take ownership" first to keep access.
        if not task.can_be_viewed_by(user):
            flash("Reassigned — it's no longer in your inbox.", "info")
            if _is_inspector():
                resp = Response(status=204)
                resp.headers["X-Inspector-Redirect"] = url_for("tasks.inbox")
                return resp
            return redirect(url_for("tasks.inbox"))
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


@tasks_bp.route("/<task_id>/take-ownership", methods=["POST"])
@login_required
def take_ownership(task_id):
    """Become the owner of a task you can see but don't own — e.g. a ticket
    imported by someone else and merely assigned to you.

    Owner is part of the visibility scope (owner OR assigned_to), so taking
    ownership lets you keep access even after reassigning the task to a third
    person.

    Restricted to the ASSIGNEE, and to tasks. View access alone is far too
    weak: tasks and projects share the ``items`` collection, so without the
    ``kind`` filter a project pk resolved here just as happily, and any member
    of a shared project could seize ownership of the project itself — which
    unlocks the owner-only routes (public share link, delete, unwrap, CSV
    import) and drops the real owner. Even among tasks, "can see it" includes
    every member of an enclosing shared project, so it let a bystander take a
    task out from under its owner."""
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id, kind="task").first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    # The documented purpose is an assignee keeping access across a handoff.
    if task.assigned_to != user:
        abort(403)
    if task.owner != user:
        task.owner = user
        task.save()
    flash("You now own this task — it stays in your inbox even if you reassign it.",
          "success")
    return redirect(request.referrer or url_for("tasks.detail", task_id=str(task.pk)))


@tasks_bp.route("/<task_id>/complete", methods=["POST"])
@login_required
def complete(task_id):
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    task.complete(by_user=user)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        from flask import jsonify
        return jsonify({"ok": True, "id": str(task.pk)})
    flash("Item completed.", "success")
    return redirect(request.referrer or url_for("dashboard.home"))

@tasks_bp.route("/<task_id>/followed-up", methods=["POST"])
@login_required
def followed_up(task_id):
    """Record a follow-up nudge: stamp the time, log a comment, and roll the
    next reminder forward by the item's cadence (see Item.mark_followed_up)."""
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    task.mark_followed_up(user, (request.form.get("note") or "").strip())
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        from flask import jsonify
        return jsonify({
            "ok": True, "id": str(task.pk),
            "followup_at": task.followup_at.isoformat() if task.followup_at else None,
        })
    flash("Followed up — next reminder set.", "success")
    return redirect(request.referrer or url_for("tasks.followup"))

@tasks_bp.route("/<task_id>/reopen", methods=["POST"])
@login_required
def reopen(task_id):
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    # Projects/folders/sections use a different status vocabulary than tasks —
    # reopening one means "active", reopening a task means "next".
    task.status = "next" if task.kind == "task" else "active"
    task.completed_at = None
    task.save()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        from flask import jsonify
        return jsonify({"ok": True, "id": str(task.pk)})
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
    from application.services.trash import trash_item
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or task.owner != user:
        abort(404)
    # Soft-delete: move the task (and any sub-tasks) to the trash. Restorable
    # for trash_retention_days, then purged. Sub-tasks ride along so restore
    # brings the whole thing back intact.
    trash_item(task)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    flash("Moved to trash.", "info")
    # Stay on whatever list / perspective / project page the user came
    # from. ``request.referrer`` is the previous URL; if it points to
    # this task's own detail page (now gone), fall back to home.
    back = request.referrer or ""
    if not back or f"/tasks/{task_id}" in back:
        back = url_for("dashboard.home")
    return redirect(back)

@tasks_bp.route("/tag-names")
@login_required
def tag_names():
    """The user's tags for the ``+name`` quick-add autocomplete. Optional
    ``?q=`` filters by substring.

    Typing a tag name blind is how tags got assigned to nothing: the parser
    only accepts a tag that exists, and half of them have spaces in the name.
    The list answers both — it shows what there is, and it hands back the
    token to write (quoted when the name has a space)."""
    from flask import jsonify
    user = current_user._get_current_object()
    q = (request.args.get("q") or "").strip().lower()
    out = []
    seen = set()
    for tag in user.tags or []:
        name = tag.name or ""
        if q and q not in name.lower():
            continue
        if name.lower() in seen:
            continue                       # older data holds the odd duplicate
        seen.add(name.lower())
        out.append({
            "name": name,
            "color": tag.color or "",
            "token": f'+"{name}"' if " " in name else f"+{name}",
        })
    out.sort(key=lambda t: t["name"].lower())
    return jsonify({"tags": out[:20]})

@tasks_bp.route("/assignees")
@login_required
def assignees():
    """Assignable users for the ``@name`` quick-add autocomplete. Optional
    ``?q=`` prefix-filters by username / display name."""
    from flask import jsonify
    from application.services.permissions import assignable_users
    user = current_user._get_current_object()
    q = (request.args.get("q") or "").strip().lower()
    out = []
    for u in assignable_users(user):
        uname = u.username or ""
        disp = getattr(u, "display_name", "") or ""
        if q and q not in uname.lower() and q not in disp.lower():
            continue
        out.append({
            "username": uname,
            "display_name": disp,
            "is_me": u.pk == user.pk,
        })
    return jsonify({"users": out[:20]})

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

    # Explicit datetime-local pickers (naive wall-clock → attach the user's tz).
    # An explicit Due wins over a date token parsed from the text.
    from application.services.tz import attach_user_tz

    def _parse_local(value):
        s = (value or "").strip()
        if not s:
            return None
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return attach_user_tz(datetime.strptime(s, fmt), user)
            except ValueError:
                continue
        return None

    task = Item(
        owner=user,
        kind="task",
        title=parsed.title,
        note=(data.get("note") or "").strip(),
        parent=parsed.project,
        tags=parsed.tags,
        priority=parsed.priority,
        defer_date=_parse_local(data.get("defer_date")),
        due_date=_parse_local(data.get("due_date")) or parsed.due_date,
        plan_date=_parse_local(data.get("plan_date")),
        flagged=parsed.flagged,
        status="open",
    )
    if parsed.assignee:
        task.assign_to(parsed.assignee, by_user=user)
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
            "assignee": str(task.assigned_to) if task.assigned_to else None,
        },
        "warnings": {
            "unresolved_tags": parsed.unresolved_tags,
            "unresolved_project": parsed.unresolved_project,
            "unresolved_assignee": parsed.unresolved_assignee,
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
    # Recurrence is a task concept (see ensure_container) — drop it so the
    # promoted project never carries repeat flags.
    task.repeat_every_days = None
    task.repeat_unit = "day"
    task.repeat_weekdays = []
    task.repeat_monthday = None
    task.repeat_from_completion = False
    task.save()
    flash(f"Converted task into project: {task.title}", "success")
    return redirect(url_for("projects.detail", project_id=str(task.pk)))

@tasks_bp.route("/merge", methods=["POST"])
@login_required
def merge():
    """Drag-merge: drop task A onto task B → A becomes a SUB-TASK of B.

    B stays a task — a task can hold child tasks directly, so it is NOT
    converted into a project/container (same rule as 'add subtask'). The
    caller re-renders the view so B shows its expand chevron with A nested.
    """
    from flask import jsonify
    user = current_user._get_current_object()
    source = Item.objects(pk=request.form.get("source_id"), owner=user).first()
    target = Item.objects(pk=request.form.get("target_id"), owner=user).first()
    if not source or not target or source.pk == target.pk:
        return jsonify({"error": "invalid tasks"}), 400
    # Reject cycles: target must not be a descendant of source.
    cur, depth = target.parent, 0
    while cur is not None and depth < 50:
        if cur.pk == source.pk:
            return jsonify({"error": "can't nest a task under its own subtask"}), 400
        cur, depth = cur.parent, depth + 1
    source.parent = target
    source.save()
    # Auto-expand the target in the list so the freshly-nested sub-task is
    # visible right away (a pk in collapsed_items marks an EXPANDED parent).
    collapsed = list(user.collapsed_items or [])
    if str(target.pk) not in collapsed:
        collapsed.append(str(target.pk))
        user.collapsed_items = collapsed
        user.save()
    return jsonify({"ok": True})

@tasks_bp.route("/<task_id>/schedule", methods=["POST"])
@login_required
def schedule(task_id):
    """Drag a task into a calendar slot → set ``plan_date``.

    Body: ``date=YYYY-MM-DD`` and optional ``hour=0..23`` + ``minute=0..59``.
    Without hour the task becomes all-day at midnight (still gets a Plan-date
    event when Google Calendar write-back is enabled). ``minute`` lets the
    Plan timeline drop a task at a 15-minute-precise time. Triggers the
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
    minute_raw = (request.form.get("minute") or "").strip()
    if not date_raw:
        return jsonify({"error": "date required"}), 400
    try:
        d = _dt.fromisoformat(date_raw).date()
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    # Interpret the wall-clock time in the user's timezone so it lands
    # on the right hour in their view.
    tz = resolve_tz(user)
    if hour_raw:
        try:
            h = max(0, min(23, int(hour_raw)))
            m = max(0, min(59, int(minute_raw))) if minute_raw else 0
        except ValueError:
            return jsonify({"error": "invalid hour/minute"}), 400
        wall = _dt.combine(d, _t(h, m))
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


@tasks_bp.route("/<task_id>/next", methods=["POST"])
@login_required
def set_next(task_id):
    """One-click Next: put the next-action mark on a task, or take it off.

    Picking work together used to mean opening the inspector and hunting for
    the right entry in a nine-value status select — which is the slow part, not
    the decision. This is the whole gesture in one POST:

        ``minutes=<int>``  also set ``estimated_minutes`` (5..1440, clamped),
                           so "next, and it's about an hour" is one click
        ``off=1``          drop the mark — back to ``open``
        ``clear_plan=1``   also take the task off the day it was planned for

    Taking the mark off only ever touches a task that actually carries it: a
    row is drawn from a list that may be a refresh behind, and a stale click
    must not quietly pull a ``waiting`` task out of its status. The duration is
    deliberately NOT cleared on the way out — how long something takes is a
    property of the task, not of it being next.

    ``clear_plan`` is what the Plan view's "Back to Next" control sends for a
    task the day ran out on: a day that is over is exactly what made the plan
    date stale, so "back into the next list" drops it rather than leaving the
    task parked on yesterday. The ordinary NEXT arrow does not send it — a task
    planned for today and also marked next keeps its slot. A future DEFER date
    is still left alone here, and still holds a next-action back: that question
    is open with the user, and answering it inside this flag would answer it
    everywhere.
    """
    from flask import jsonify
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    if task.kind != "task":
        return jsonify({"error": "not a task"}), 400

    minutes = None
    raw = (request.form.get("minutes") or "").strip()
    if raw:
        try:
            minutes = max(5, min(1440, int(raw)))
        except ValueError:
            return jsonify({"error": "minutes must be an integer"}), 400

    off = (request.form.get("off") or "").strip() in ("1", "true", "yes", "on")
    clear_plan = (request.form.get("clear_plan") or "").strip() in ("1", "true", "yes", "on")
    if off:
        if task.status == "next":
            task.status = "open"
    else:
        task.status = "next"
    if minutes is not None:
        task.estimated_minutes = minutes
    plan_cleared = clear_plan and task.plan_date is not None
    if plan_cleared:
        task.plan_date = None
    task.save()
    # A duration change is what the mirrored calendar event's length is made
    # of, so the same write-back the resize handle triggers applies here — and
    # a dropped plan date has to take the mirrored plan event with it.
    warnings = _writeback_calendar(task, user) or [] \
        if (minutes is not None or plan_cleared) else []
    return jsonify({
        "ok": True,
        "status": task.status,
        "is_next": task.status == "next",
        "estimated_minutes": task.estimated_minutes,
        "plan_cleared": plan_cleared,
        "warnings": warnings,
    })


@tasks_bp.route("/<task_id>/unschedule", methods=["POST"])
@login_required
def unschedule(task_id):
    """Take a task's time-of-day away (drag it off the Plan timeline back into
    the Unplanned rail). Removes the mirrored plan event from Google too.

    With ``date=YYYY-MM-DD`` the task STAYS on that day, without a time — the
    day's midnight, the same value carry-over writes. That is what the rail
    shows, and it is why the pill survives a reload: a task with no plan date,
    no due date and no flag is not part of any day, so clearing the date
    outright made the pill vanish the next time the page was drawn.
    Without a date the plan date is cleared (off the day entirely).
    """
    from flask import jsonify
    from datetime import datetime as _dt, time as _t
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    raw = (request.form.get("date") or "").strip()
    keep_on = None
    if raw:
        try:
            keep_on = _dt.combine(_dt.fromisoformat(raw).date(), _t(0, 0)) \
                .replace(tzinfo=resolve_tz(user))
        except ValueError:
            return jsonify({"error": "invalid date"}), 400
    task.plan_date = keep_on
    task.save()
    warnings = _writeback_calendar(task, user) or []
    return jsonify({"ok": True, "warnings": warnings})


@tasks_bp.route("/<task_id>/quick-edit", methods=["POST"])
@login_required
def quick_edit(task_id):
    """Lightweight partial update for the subtask quick-edit modal: assignee
    plus the plan dates (defer / due). Only the fields present in the body are
    touched, so this never clobbers the rest of the task the way a full form
    round-trip would. Returns JSON for the modal to reflect."""
    from flask import jsonify
    from datetime import datetime as _dt
    from application.services.tz import attach_user_tz
    from application.services.permissions import resolve_assignable_user
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)

    def _parse(name):
        raw = (request.form.get(name) or "").strip()
        if not raw:
            return None
        try:
            return attach_user_tz(_dt.fromisoformat(raw), user)
        except ValueError:
            return "invalid"

    if "defer_date" in request.form:
        v = _parse("defer_date")
        if v == "invalid":
            return jsonify({"error": "invalid defer_date"}), 400
        task.defer_date = v
    if "due_date" in request.form:
        v = _parse("due_date")
        if v == "invalid":
            return jsonify({"error": "invalid due_date"}), 400
        task.due_date = v
    if "assigned_to" in request.form:
        pk = (request.form.get("assigned_to") or "").strip()
        if pk:
            new_assignee = resolve_assignable_user(user, pk)
            # Silently ignore a non-shareable pk — same lenient behaviour as
            # the full edit form; the select never offers such a pk anyway.
            if new_assignee and task.assigned_to != new_assignee:
                task.assign_to(new_assignee, by_user=user)
        else:
            task.assign_to(None)
    # An empty multi-select sends no ``tags`` key, so a hidden marker signals
    # the field was present (→ apply, empty = clear all tags).
    if request.form.get("tags_submitted"):
        _apply_tags(task, user, request.form.getlist("tags"))

    task.save()
    _writeback_calendar(task, user)
    return jsonify({
        "ok": True,
        "assigned_to": str(task.assigned_to) if task.assigned_to else None,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "defer_date": task.defer_date.isoformat() if task.defer_date else None,
        "tags": list(task.tags or []),
    })


@tasks_bp.route("/<task_id>/quick-plan", methods=["POST"])
@login_required
def quick_plan(task_id):
    """Inspector quick-plan: set ``plan_date`` to today / tomorrow / the day
    after (all-day, midnight in the user's tz) together with a short
    ``plan_note``, in one shot — no full form round-trip. ``when=clear`` wipes
    both. The note renders before the task name in list views. Returns JSON;
    the client refreshes the list behind the panel."""
    from flask import jsonify
    from datetime import datetime as _dt, time as _t, timedelta as _td
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    when = (request.form.get("when") or "").strip()
    note = (request.form.get("plan_note") or "").strip()[:200]
    offsets = {"today": 0, "tomorrow": 1, "dayafter": 2}
    if when == "clear":
        task.plan_date = None
        task.plan_note = ""
    elif when in offsets:
        tz = resolve_tz(user)
        day = _dt.now(tz).date() + _td(days=offsets[when])
        task.plan_date = _dt.combine(day, _t(0, 0)).replace(tzinfo=tz)
        task.plan_note = note
    else:
        return jsonify({"error": "invalid when"}), 400
    task.save()
    _writeback_calendar(task, user)
    return jsonify({
        "ok": True,
        "plan_date": task.plan_date.isoformat() if task.plan_date else None,
        "plan_note": task.plan_note,
    })


@tasks_bp.route("/<task_id>/zammad-refresh", methods=["POST"])
@login_required
def zammad_refresh(task_id):
    """The inspector's refresh button beside a Zammad ticket number.

    Asks Zammad for that one ticket right now and merges it in through the
    very code the scheduled sync uses, so the two can't say different things
    about the same ticket. Read-only against Zammad. Returns JSON; the client
    re-renders the inspector so the new title/state/comments are simply there.
    """
    from flask import jsonify
    from application.services import zammad_refresh as zr
    from application.services.zammad_import import ZammadError

    task = _editable_task_or_404(task_id)
    if not zr.ticket_id(task):
        return jsonify({"error": "This task didn't come from Zammad."}), 400
    try:
        stats = zr.refresh_item(task)
    except zr.NotConfigured as exc:
        # The owner's own settings — fixable here, so it is not a gateway fault.
        return jsonify({"error": str(exc)}), 400
    except ZammadError as exc:
        return jsonify({"error": str(exc)}), 502
    # The merge wrote through its own copy of the document; re-read before
    # reporting, so the panel's header gets the title Zammad actually has now.
    task.reload()
    return jsonify({
        "ok": True,
        "title": task.title,
        "ticket": task.external_ref or zr.ticket_id(task),
        "comments_imported": stats.comments_imported,
        "time_entries_imported": stats.time_entries_imported,
        "reassigned": bool(stats.tickets_assigned),
        "errors": list(stats.errors),
        "stats": stats.as_dict(),
    })


# Preset times of day for the Today-list quick snooze. Wall-clock in the
# user's timezone; "tomorrow" is the next day.
SNOOZE_TARGETS = {
    "noon": (13, 0),
    "evening": (19, 0),
    "tomorrow": (8, 0),
}


@tasks_bp.route("/<task_id>/snooze", methods=["POST"])
@login_required
def snooze(task_id):
    """Quick-defer (or reschedule) a Today task to a preset time of day.

    Body: ``target`` ∈ {noon, evening, tomorrow} and ``mode`` ∈ {hide, move}:
      * ``hide`` — set ``defer_date`` so the task drops out of Today until the
        target moment, then resurfaces; its plan/due dates stay untouched.
      * ``move`` — set ``plan_date`` to the target (a genuine reschedule); any
        future ``defer_date`` is cleared so the two don't fight.

    ``noon``/``evening`` land today; if that moment has already passed they roll
    to tomorrow so the task still hides. Returns ``{ok, removed}`` — ``removed``
    tells the caller to drop the row (it left Today) versus refresh it in place.
    """
    from flask import jsonify
    from datetime import datetime as _dt, time as _t
    from application.services.today import is_today_now_task

    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    target = (request.form.get("target") or "").strip()
    mode = (request.form.get("mode") or "hide").strip()
    if target not in SNOOZE_TARGETS:
        return jsonify({"error": "invalid target"}), 400
    if mode not in ("hide", "move"):
        return jsonify({"error": "invalid mode"}), 400

    tz = resolve_tz(user)
    now = datetime.now(timezone.utc)
    h, m = SNOOZE_TARGETS[target]
    day = now.astimezone(tz).date()
    if target == "tomorrow":
        day = day + timedelta(days=1)
    when = _dt.combine(day, _t(h, m)).replace(tzinfo=tz)
    if when <= now:  # same-day target already past → next day
        when = when + timedelta(days=1)

    if mode == "hide":
        task.defer_date = when
    else:  # move
        task.plan_date = when
        if task.defer_date and task.defer_date > now:
            task.defer_date = None
    task.save()
    if mode == "move":
        _writeback_calendar(task, user)

    return jsonify({"ok": True, "removed": not is_today_now_task(task, user, now)})


@tasks_bp.route("/plan-create", methods=["POST"])
@login_required
def plan_create():
    """Quick-add from the Plan timeline: create a task planned at a slot.

    Body: ``text`` (supports @person / +tag / #project / pN tokens), ``date=YYYY-MM-DD``,
    ``hour``, ``minute``, optional ``minutes`` (duration). Returns the new
    task so the client can drop a card in place.
    """
    from flask import jsonify
    from datetime import datetime as _dt, time as _t
    from application.services.quick_parse import parse_task_text
    user = current_user._get_current_object()
    data = request.get_json(silent=True) or request.form
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Empty input"}), 400
    try:
        d = _dt.fromisoformat((data.get("date") or "").strip()).date()
        h = max(0, min(23, int(data.get("hour") or 0)))
        m = max(0, min(59, int(data.get("minute") or 0)))
    except ValueError:
        return jsonify({"error": "invalid slot"}), 400
    try:
        minutes = max(5, min(1440, int(data.get("minutes") or 30)))
    except ValueError:
        minutes = 30
    tz = resolve_tz(user)
    parsed = parse_task_text(text, user)
    if not parsed.title:
        return jsonify({"error": "Couldn't extract a title."}), 400
    task = Item(
        owner=user, kind="task", title=parsed.title, parent=parsed.project,
        tags=parsed.tags, priority=parsed.priority, due_date=parsed.due_date,
        flagged=parsed.flagged, status="next", estimated_minutes=minutes,
        plan_date=_dt.combine(d, _t(h, m)).replace(tzinfo=tz),
    )
    if parsed.assignee:
        task.assign_to(parsed.assignee, by_user=user)
    task.save()
    _writeback_calendar(task, user)
    return jsonify({"ok": True, "id": str(task.pk), "title": task.title,
                    "minutes": minutes,
                    "detail_url": url_for("tasks.detail", task_id=str(task.pk)),
                    "edit_url": url_for("tasks.edit", task_id=str(task.pk))})


@tasks_bp.route("/carry-over", methods=["POST"])
@login_required
def carry_over():
    """Pull forward unfinished tasks whose plan time is before today: their
    ``plan_date`` is reset to today (midnight → today's Plan backlog) so they
    resurface instead of being stranded in the past."""
    from flask import jsonify
    from datetime import time as _t
    user = current_user._get_current_object()
    now = datetime.now(timezone.utc)
    tz = resolve_tz(user)
    today = now.astimezone(tz).date()
    day_start = datetime.combine(today, _t(0, 0)).replace(tzinfo=tz)
    moved, warnings = 0, []
    # Only what the Plan view would then show: pulling a task onto today that
    # the timeline filters out moves it without anybody seeing it arrive.
    for t in Item.objects(_scope(user), kind="task",
                          status__nin=["done", "dropped", "someday"],
                          plan_date__ne=None, plan_date__lt=day_start,
                          ).filter(_plan_mine_q(user)):
        if not t.can_be_edited_by(user):
            continue
        t.plan_date = day_start
        t.save()
        warnings.extend(_writeback_calendar(t, user) or [])
        moved += 1
    return jsonify({"ok": True, "moved": moved, "warnings": warnings})


@tasks_bp.route("/auto-plan", methods=["POST"])
@login_required
def auto_plan():
    """Auto-schedule a day's unplanned tasks into free slots (the Plan
    timeline's "Plan my day" button).

    Body: ``date=YYYY-MM-DD``. Non-destructive: only tasks without a
    time-of-day are placed, around existing calendar events and tasks that
    are already timed. Each placed task gets ``plan_date`` set + Google
    write-back. Returns ``{ok, planned, skipped, warnings}``.

    **Nothing ever drops out of the view.** Two rules make that true, because
    a planner that hides work is worse than one that plans badly:

    * once the working day is over there is no window left, so the call is a
      no-op and answers ``day_over`` instead of re-packing a day into nothing;
    * a task that does not fit keeps its place on the DAY (midnight, i.e. no
      time-of-day) and shows up in the Unplanned rail — it never has its
      ``plan_date`` cleared, which used to take it off the plate entirely.
    """
    from flask import jsonify
    from datetime import datetime as _dt, time as _t
    from application.services.auto_plan import plan_day, _snap_up
    from application.services.calendar_sync import fetch_events

    user = current_user._get_current_object()
    now = datetime.now(timezone.utc)
    date_raw = (request.form.get("date") or "").strip()
    try:
        d = _dt.fromisoformat(date_raw).date() if date_raw else now.date()
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    tz = resolve_tz(user)

    is_today = (d == now.astimezone(tz).date())
    start_h = user.plan_day_start if user.plan_day_start is not None else 9
    end_h = user.plan_day_end if user.plan_day_end is not None else 18
    window_start = _dt.combine(d, _t(start_h, 0)).replace(tzinfo=tz)
    if end_h >= 24:
        window_end = _dt.combine(d, _t(0, 0)).replace(tzinfo=tz) + timedelta(days=1)
    else:
        window_end = _dt.combine(d, _t(end_h, 0)).replace(tzinfo=tz)
    if is_today:
        window_start = max(window_start, _snap_up(now.astimezone(tz)))

    # There is no day left to pack into: after the working hours are over
    # ``window_start`` (= now) has run past ``window_end``, nothing can fit,
    # and a re-pack would strip every task's time for no gain. Do nothing and
    # say so — this is what took the whole day off the view when "Re-pack" was
    # pressed at 19:00 with hours ending at 18:00.
    if window_start >= window_end:
        return jsonify({"ok": True, "planned": 0, "skipped": 0, "day_over": True,
                        "window": f"{start_h:02d}:00–{min(end_h, 24):02d}:00",
                        "warnings": []})

    # Day's calendar events (same opt-in calendars as the Today view).
    day_start = _dt.combine(d, _t(0, 0)).replace(tzinfo=tz)
    events = []
    for cal in (user.calendars or []):
        if not cal.show_in_today:
            continue
        try:
            events.extend(fetch_events(cal, day_start, day_start + timedelta(days=1),
                                       user=user))
        except Exception:  # noqa: BLE001 — a flaky calendar shouldn't block planning
            pass

    cls = _plan_classify(
        _plan_day_tasks(user, d, tz, is_today=is_today),
        events, tz, d, now,
    )
    mode = (request.form.get("mode") or "").strip()
    gap = user.plan_buffer_min or 0

    events_busy = [(it["start"], it["end"]) for it in cls["timed"] if it["kind"] == "event"]
    timed_tasks = [it for it in cls["timed"] if it["kind"] == "task"]
    # Optional lunch break kept free, in minutes since midnight
    # (start == end → disabled).
    ls, le = user.plan_lunch_start_min or 0, user.plan_lunch_end_min or 0
    lunch_busy = []
    if le > ls:
        lunch_busy = [(_dt.combine(d, _t(ls // 60, ls % 60)).replace(tzinfo=tz),
                       _dt.combine(d, _t(le // 60, le % 60)).replace(tzinfo=tz))]

    if mode == "repack":
        # Destructive: re-plan backlog AND already-timed tasks from scratch;
        # only calendar events + lunch stay fixed.
        cands = list(cls["backlog"]) + [
            {"obj": it["obj"], "duration_min": it["duration_min"]} for it in timed_tasks
        ]
        busy = events_busy + lunch_busy
    else:
        # Non-destructive: fill only the backlog around what's already placed.
        cands = list(cls["backlog"])
        busy = events_busy + [(it["start"], it["end"]) for it in timed_tasks] + lunch_busy

    # Candidate order: priority (P1 first), then due date, then manual order.
    FAR = datetime(9999, 1, 1, tzinfo=timezone.utc)
    cands.sort(key=lambda c: (c["obj"].priority or 99,
                              _as_tz(c["obj"].due_date, timezone.utc) or FAR,
                              c["obj"].sort_order or 0))
    starts = plan_day([c["duration_min"] for c in cands], busy,
                      window_start, window_end, gap=gap)

    planned, skipped, warnings = 0, 0, []
    for c, start in zip(cands, starts):
        task = c["obj"]
        if start is None:
            # A re-pack loses the task's time — but it must NOT lose the task.
            # Clearing ``plan_date`` took it off the day altogether, and a task
            # that is neither planned, due nor flagged matches nothing in
            # ``_plan_day_tasks``, so it silently disappeared from the planner.
            # The day's midnight is "on this day, no time yet" (what carry-over
            # writes too), which puts it in the Unplanned rail where an
            # unplaced task belongs.
            cur = _as_tz(task.plan_date, tz)
            if mode == "repack" and cur and (cur.hour or cur.minute):
                task.plan_date = day_start
                task.save()
                warnings.extend(_writeback_calendar(task, user) or [])
            skipped += 1
            continue
        task.plan_date = start
        task.save()
        warnings.extend(_writeback_calendar(task, user) or [])
        planned += 1
    return jsonify({"ok": True, "planned": planned, "skipped": skipped,
                    "warnings": warnings})


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
    from application.services.permissions import container_visibility_q
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)

    cur_parent = task.parent
    cvis = container_visibility_q(user)  # owned / member / team-shared containers
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
                cvis,
                kind__in=["project", "folder", "section"],
                status="active",
                folder_domain__ne="notes",   # a note's folder is not a place for a task
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
            cvis,
            kind__in=["project", "folder"],
            parent=None, status="active",
            folder_domain__ne="notes",
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
            cvis,
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
            cvis,
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
            cvis,
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
    #
    #    Dual-home: if the actor is the ASSIGNEE of someone else's task, the
    #    move files it under one of THEIR OWN projects (``assignee_parent``)
    #    and never touches the owner's canonical ``parent`` — so it can't
    #    vanish from the owner's project. The owner (and own tasks) move the
    #    canonical ``parent`` as before.
    touched = False
    if "project_id" in data:
        pid = (data.get("project_id") or "").strip()
        proj = None
        if pid:
            from application.services.permissions import resolve_visible_item
            proj = resolve_visible_item(user, pid)
            if not proj:
                return jsonify({"error": "destination project not accessible"}), 403
        try:
            # Role-aware: an assignee files into THEIR own project
            # (assignee_parent); the owner moves the canonical parent.
            task.set_home_for(user, proj)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 403
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

    # 3) Apply priority from a priority-tagged drop target (Today grouped by
    #    priority). Empty string = the "No priority" bucket → clear it. Only an
    #    explicit, valid value ever writes, so an unparseable payload is a no-op
    #    rather than silently wiping the task's priority.
    if "priority" in data:
        raw_priority = (data.get("priority") or "").strip()
        new_priority, valid = None, True
        if raw_priority:
            try:
                new_priority = int(raw_priority)
                valid = new_priority in (1, 2, 3, 4)
            except (TypeError, ValueError):
                valid = False
        if valid and new_priority != task.priority:
            task.priority = new_priority
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
                "assignee_parent_id": task.assignee_filing_id() or None,
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
        from application.services.trash import trash_item
        for t in tasks:
            if t.owner == user:
                trash_item(t)
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
        from application.services.permissions import resolve_visible_item
        project_id = (request.form.get("project_id") or "").strip()
        # Only accept a target the user may actually see — otherwise a crafted
        # project_id could reparent tasks into (and confirm the existence of)
        # someone else's project.
        project = resolve_visible_item(user, project_id) if project_id else None
        if project_id and not project:
            flash("That project isn't accessible.", "danger")
            return redirect(request.referrer or url_for("dashboard.home"))
        for t in tasks:
            if t.can_be_edited_by(user):
                # Route through set_home_for like every other move path
                # (tasks.move, _apply_form, the API bulk move): for a task
                # merely delegated TO this user that writes their personal
                # filing, never the OWNER's canonical parent. Assigning
                # ``t.project`` directly wrote ``parent``, which let an
                # assignee pull a delegated task out of the owner's project
                # and under their own — taking its subtree with it.
                try:
                    t.set_home_for(user, project)
                except ValueError:
                    skipped += 1
                    continue
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

def _drop_labels(task, names, user):
    """Take the labels in ``names`` off ``task`` — WITHOUT saving; the caller's
    own save carries it, so answering and handing the item back costs one write.

    Returns the names actually removed. Matching is case-insensitive against
    what the item carries, and a name it doesn't carry is simply ignored: the
    list comes from checkboxes rendered from the item's own labels, and a stale
    form (the label was already taken off elsewhere) must not be an error.
    """
    wanted = {n.strip().lower() for n in (names or []) if n and n.strip()}
    if not wanted or not task.tags or not task.can_be_edited_by(user):
        return []
    kept, removed = [], []
    for name in task.tags:
        (removed if name.lower() in wanted else kept).append(name)
    if removed:
        task.tags = kept
    return removed


@tasks_bp.route("/<task_id>/tags/remove", methods=["POST"])
@login_required
def remove_tag(task_id):
    """Take ONE label off an item, from the list it is showing in.

    The list is where you notice a label is wrong, and opening the inspector to
    drop one is a detour for a one-click thought. Same helper the comment box
    uses, so "which labels does this actually carry" is answered in one place:
    matching is case-insensitive and a name the item no longer carries is a
    no-op, not an error — a list can be seconds stale, and clicking the same ×
    twice (or on a row another tab already cleaned up) must not be a 400.

    Answers JSON to the in-place caller and redirects back for a plain POST, so
    the control still works with JavaScript off.
    """
    from flask import jsonify
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_edited_by(user):
        abort(404)
    removed = _drop_labels(task, [request.form.get("tag") or ""], user)
    if removed:
        task.save()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "removed": removed, "tags": list(task.tags or [])})
    return redirect(request.referrer or url_for("tasks.detail", task_id=task_id))


@tasks_bp.route("/<task_id>/comment", methods=["POST"])
@login_required
def comment(task_id):
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    form = CommentForm()
    if form.validate_on_submit():
        # Time typed beside the comment is checked BEFORE anything is written:
        # a duration that can't be read or booked refuses the whole submit, so
        # the comment never lands while the hours it was about silently don't.
        booking, problem = _comment_time(user, task)
        body = (form.body.data or "").strip()
        if not problem and not body and not booking:
            problem = "Nothing to post — write a comment or enter a time."
        if problem:
            if request.headers.get("X-Inspector-Request") == "1":
                return jsonify({"ok": False, "error": problem}), 400
            flash(problem + (" The comment was not posted." if body else ""), "warning")
            return redirect(request.referrer or url_for("tasks.detail", task_id=task_id))
        if not body:
            # Time only (the box was left empty): book it, post no comment.
            logged = _book_comment_time(user, task, booking, "")
            if request.headers.get("X-Inspector-Request") == "1":
                return jsonify({"ok": True, "comment": False,
                                "logged_minutes": logged,
                                "time_block": _time_block(user, task)})
            flash(f"Logged {current_app.jinja_env.filters['duration'](logged)}.",
                  "success")
            return redirect(request.referrer or url_for("tasks.detail", task_id=task_id))
        # Labels ticked in the comment box come off with the same submit —
        # writing the answer and handing the task back is one gesture (the
        # `Review AI Work` handshake is the case this exists for). Done before
        # add_comment so both land in its single save.
        removed = _drop_labels(task, request.form.getlist("remove_tags"), user)
        task.add_comment(user, form.body.data)
        # Mentions AND the task's own cast — one call so this path and the API
        # cannot drift into notifying different people.
        from application.services.notifications import notify_new_comment
        notify_new_comment(task, form.body.data, by_user=user)
        logged = _book_comment_time(user, task, booking, form.body.data)
        if request.headers.get("X-Inspector-Request") == "1":
            c = task.comments[-1]
            return jsonify(
                {
                    "ok": True,
                    "comment": True,
                    "index": len(task.comments) - 1,
                    "author": c.author_name,
                    "body": c.body,
                    # Pre-rendered markdown so the inspector can show pasted
                    # images / formatting immediately, without a reload.
                    "body_html": current_app.jinja_env.filters["markdown"](c.body),
                    "created_at": c.created_at.isoformat(),
                    "is_public": c.is_public,
                    # So the panel can drop the chips and un-select the same
                    # names in the label picker — an inspector save afterwards
                    # would otherwise put the label straight back on.
                    "removed_tags": removed,
                    "tags": list(task.tags or []),
                    # The time section is re-rendered when the comment booked
                    # time, so its total and entry list don't go stale.
                    "logged_minutes": logged,
                    "time_block": _time_block(user, task) if logged else None,
                }
            )
        msg = "Comment posted."
        if removed:
            msg = "Comment posted. Removed label(s): " + ", ".join(removed) + "."
        if logged:
            msg += f" Logged {current_app.jinja_env.filters['duration'](logged)}."
        flash(msg, "success")
    # Stay where the comment was posted (task detail OR a project page).
    return redirect(request.referrer or url_for("tasks.detail", task_id=task_id))

def _comment_time(user, task):
    """The time booking a comment submit asks for: ``((project, minutes), None)``,
    ``(None, None)`` when the field was left empty, or ``(None, message)`` when
    it can't be booked."""
    raw = (request.form.get("log_duration") or "").strip()
    if not raw:
        return None, None
    from application.services.timetracking import (
        default_project_id, parse_duration, project_for_user,
    )
    minutes = parse_duration(raw)
    if not minutes:
        return None, f"Can't read “{raw}” as a duration (1h30m, 1:30, 90)."
    # The project the box NAMED travels with the form; the default is only the
    # answer for a client that didn't send one.
    pid = (request.form.get("log_project_id") or "").strip() \
        or default_project_id(user, task)
    project = project_for_user(user, pid) if pid else None
    if project is None:
        return None, "No billing project to log this time to."
    return (project, minutes), None


def _book_comment_time(user, task, booking, body):
    """Write the entry ``_comment_time`` approved; the comment is its note.
    Returns the minutes booked (0 when nothing was asked for)."""
    if not booking:
        return 0
    from application.services.timetracking import add_entry, custom_values
    from application.services.tz import user_zone
    project, minutes = booking
    end = datetime.now(user_zone(user))
    spent_on, clock = end, {}
    if project.time_mode == "clock" and minutes < 24 * 60:
        # A project booked by the clock wants the times of day: the work ends
        # now (the moment of the submit) and started ``minutes`` before —
        # dated on the day it started, like the stopwatch.
        start = end - timedelta(minutes=minutes)
        spent_on = start
        clock = {"start_time": start.strftime("%H:%M"),
                 "end_time": end.strftime("%H:%M")}
    add_entry(
        project=project, user=user, minutes=minutes, spent_on=spent_on,
        # Nothing but the duration was asked for, so custom fields take their
        # defaults — the same as the one-click stopwatch start.
        custom=custom_values(project, lambda _key: None),
        note=(body or "").strip(), source_item=task, **clock,
    )
    user.tt_last_project = str(project.id)
    user.save()
    return minutes


def _time_block(user, task):
    from application.services.timetracking import task_time_context
    return render_template("timetracking/_task_time.html", task=task,
                           tt=task_time_context(user, task))


@tasks_bp.route("/<task_id>/comments/peek")
@login_required
def comments_peek(task_id):
    """The comment thread of one item as a bare partial, for the hover panel
    behind the comment chip in a task list (``static/js/comment-peek.js``).

    Fetched on OPEN, not on render: a list renders a hundred rows and the
    comments of the one row a pointer rests on are the only ones anybody
    reads. The client keeps what it got per task for the life of the page, so
    hovering the same chip ten times is still one query — and a page render
    costs exactly what it did before this feature existed.
    """
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    return render_template("tasks/_comments_peek.html", item=task)


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

@tasks_bp.route("/<task_id>/comments/<int:index>/delete", methods=["POST"])
@login_required
def delete_comment(task_id, index):
    """Delete a comment. Its author (the creator) or the task owner only —
    same permission model as the visibility toggle. Index-based like the
    toggle; the client re-indexes the remaining rows after removal."""
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    if not (0 <= index < len(task.comments or [])):
        abort(404)
    comment = task.comments[index]
    if comment.author != user and task.owner != user:
        abort(403)
    task.comments.pop(index)
    task.save()
    if request.headers.get("X-Inspector-Request") == "1" or request.is_json:
        from flask import jsonify
        return jsonify({"ok": True, "index": index})
    flash("Comment deleted.", "success")
    return redirect(request.referrer or url_for("tasks.detail", task_id=task_id))

@tasks_bp.route("/<task_id>/comments/<int:index>/edit", methods=["POST"])
@login_required
def edit_comment(task_id, index):
    """Edit a comment's body. Its author (the creator) or the task owner only —
    same permission model as delete/visibility. Returns the re-rendered markdown
    so the inspector can swap it in without a reload."""
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    if not (0 <= index < len(task.comments or [])):
        abort(404)
    comment = task.comments[index]
    if comment.author != user and task.owner != user:
        abort(403)
    body = (request.form.get("body") or "").strip()
    if not body:
        abort(400)
    comment.body = body
    task.save()
    if request.headers.get("X-Inspector-Request") == "1" or request.is_json:
        from flask import jsonify
        return jsonify({
            "ok": True, "index": index, "body": body,
            "body_html": current_app.jinja_env.filters["markdown"](body),
        })
    flash("Comment updated.", "success")
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


@tasks_bp.route("/<task_id>/toggle-note-task", methods=["POST"])
@login_required
def toggle_note_task(task_id):
    """Tick/untick a checklist line in an item's note straight from the
    rendered body — the same move the standalone notes offer. Works for any
    item with a note (task, project, section), since that is where checklists
    written in the scratchpad or the note editor end up."""
    from application.services import md_tasks
    task = _editable_task_or_404(task_id)
    try:
        idx = int(request.form.get("index", ""))
    except ValueError:
        return jsonify({"error": "bad index"}), 400
    result = md_tasks.toggle(task.note or "", idx)
    if result is None:
        return jsonify({"error": "no such checkbox"}), 404
    task.note, checked = result
    task.save()
    return jsonify({"ok": True, "checked": checked})


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
# ITEM_WARN_BYTES / ITEM_MAX_BYTES (the doc-size guard) live on the model.
NOTE_IMAGE_MAX_BYTES = 5 * 1024 * 1024     # per-image upload cap
NOTE_FILE_MAX_BYTES = 10 * 1024 * 1024     # per-attachment upload cap


def _safe_attachment_name(name: str) -> str:
    """Strip any path and control/quote chars from a client-supplied filename
    so it's safe in a Content-Disposition header and as a Markdown link label."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch >= " " and ch != '"').strip()
    return name or "attachment"


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
    return task.storage_bytes()


def _append_markdown_to_note(task, markdown: str) -> None:
    """Append an uploaded attachment's markdown reference to the task note.

    Used by the "create then attach" flow: the file is uploaded right after the
    task is created, so there's no open editor to insert the reference into —
    the server appends it to the stored note instead."""
    note = (task.note or "").rstrip()
    task.note = (note + "\n\n" + markdown) if note else markdown
    task.save()


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

    markdown = f"![pasted image]({url_for('tasks.note_image', task_id=str(task.pk), token=token)})"
    # "create then attach" flow: append the reference to the note server-side,
    # since there's no open editor to insert it into.
    if request.form.get("append_to_note") == "1":
        _append_markdown_to_note(task, markdown)
    payload = {
        "markdown": markdown,
        "storage": task.storage_info(),
    }
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


def _store_note_file(item, fileobj):
    """Append an arbitrary uploaded file to ``item.note_files`` (shared by the
    task and note routes). Returns a JSON-ready payload dict, or an
    ``(error, status)`` tuple on failure."""
    import secrets
    if not fileobj:
        return ("no file", 400)
    data = fileobj.read()
    if not data:
        return ("empty file", 400)
    if len(data) > NOTE_FILE_MAX_BYTES:
        return (f"file too large (max {NOTE_FILE_MAX_BYTES // 1024 // 1024} MB)", 413)
    name = _safe_attachment_name(fileobj.filename)
    token = secrets.token_urlsafe(12)
    item.note_files.append(NoteFile(
        token=token, filename=name,
        content_type=fileobj.mimetype or "application/octet-stream", data=data,
    ))
    size = _item_bson_size(item)
    if size > ITEM_MAX_BYTES:
        item.note_files.pop()  # don't persist — would risk the 16 MB doc cap
        return ("item is full — remove some attachments before adding more", 413)
    item.save()
    return {"token": token, "filename": name, "size": size,
            "storage": item.storage_info()}


# Types we never render inline: same-origin HTML/SVG/XML can execute script in
# the user's session. These are always forced to download; everything else is
# served ``inline`` so the browser opens it in place (PDFs, images, text…) when
# it can, and falls back to downloading what it can't display (Office docs).
_FORCE_DOWNLOAD_TYPES = {
    "text/html", "application/xhtml+xml", "image/svg+xml",
    "application/xml", "text/xml",
}


def _note_file_response(item, token):
    """Serve an inline note attachment. Opens in the browser when it can render
    the type; only script-capable types (HTML/SVG/XML) are forced to download."""
    f = next((x for x in (item.note_files or []) if x.token == token), None)
    if not f:
        abort(404)
    from urllib.parse import quote
    ctype = (f.content_type or "application/octet-stream").lower()
    disposition = "attachment" if ctype in _FORCE_DOWNLOAD_TYPES else "inline"
    fallback = f.filename.encode("ascii", "ignore").decode() or "attachment"
    content_disposition = (f"{disposition}; filename=\"{fallback}\"; "
                           f"filename*=UTF-8''{quote(f.filename)}")
    return Response(
        bytes(f.data),
        mimetype=f.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": content_disposition,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=86400",
        },
    )


@tasks_bp.route("/<task_id>/note-file", methods=["POST"])
@login_required
def note_file_upload(task_id):
    """Store an arbitrary file dropped/pasted onto a note inline on the item
    and return a Markdown download link. Warns / refuses as the item grows."""
    task = _editable_task_or_404(task_id)
    result = _store_note_file(task, request.files.get("file"))
    if isinstance(result, tuple):
        return jsonify({"error": result[0]}), result[1]
    label = result["filename"].replace("[", "").replace("]", "")
    url = url_for("tasks.note_file", task_id=str(task.pk), token=result["token"])
    markdown = f"[{label}]({url})"
    if request.form.get("append_to_note") == "1":
        _append_markdown_to_note(task, markdown)
    payload = {"markdown": markdown, "storage": result["storage"]}
    if result["size"] > ITEM_WARN_BYTES:
        payload["warning"] = (f"This item is getting large "
                              f"({result['size'] / 1024 / 1024:.1f} MB of 16 MB max) "
                              f"— consider removing some attachments.")
    return jsonify(payload)


@tasks_bp.route("/<task_id>/note-file/<token>")
@login_required
def note_file(task_id, token):
    """Serve an inline note attachment stored on the item, as a download."""
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    return _note_file_response(task, token)


@tasks_bp.route("/<task_id>/related-notes")
@login_required
def related_notes(task_id):
    """Notes linked to this task (via the symmetric ``related`` field). Used by
    the day/plan view's per-task note button to list existing notes."""
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    notes = []
    for r in task.related_items():
        if r.kind == "note" and r.can_be_viewed_by(user):
            notes.append({
                "id": str(r.pk),
                "title": r.title or "Untitled note",
                "url": url_for("notes.detail", note_id=str(r.pk)),
            })
    return jsonify({"notes": notes})


@tasks_bp.route("/<task_id>/new-note", methods=["POST"])
@login_required
def new_related_note(task_id):
    """Create a note linked to this task, straight from the day/plan view, and
    return its URL so the client can open it."""
    user = current_user._get_current_object()
    task = Item.objects(pk=task_id).first()
    if not task or not task.can_be_viewed_by(user):
        abort(404)
    title = (request.form.get("title") or "").strip() or (task.title or "Untitled note")
    note = Item(owner=user, kind="note", title=title, note="")
    note.save()
    note.relate_to(task)  # symmetric: surfaces on the task's related list too
    return jsonify({
        "ok": True,
        "id": str(note.pk),
        "url": url_for("notes.detail", note_id=str(note.pk)),
    })


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


def _related_response(task):
    """Re-render the related-items partial for AJAX callers, else redirect
    back to the detail page (no-JS fallback). Mirrors _checklist_response."""
    if request.headers.get("X-Partial") == "1":
        return render_template("tasks/_related.html", task=task)
    return redirect(url_for("tasks.detail", task_id=str(task.pk)) + "#related")


@tasks_bp.route("/<task_id>/related", methods=["POST"])
@login_required
def related_add(task_id):
    """Link another item as 'related' (symmetric). The link is purely a
    reference shown in the inspector — it never groups anything in views."""
    task = _editable_task_or_404(task_id)
    user = current_user._get_current_object()
    rel_id = (request.form.get("related_id") or "").strip()
    # Only link to a visible item the user may edit (the link writes the
    # other side too), and never to self.
    other = Item.objects(pk=rel_id).first() if rel_id else None
    if other and other.pk != task.pk and other.can_be_edited_by(user):
        task.relate_to(other)
    return _related_response(task)


@tasks_bp.route("/<task_id>/related/<rel_id>/remove", methods=["POST"])
@login_required
def related_remove(task_id, rel_id):
    """Drop a related link from both sides. ``all_objects`` so a link to a
    trashed item can still be removed; a purged target (other=None) just
    clears the dangling ref on this side."""
    task = _editable_task_or_404(task_id)
    other = Item.all_objects(pk=rel_id).first()
    task.unrelate_from(other)
    return _related_response(task)


@tasks_bp.route("/<task_id>/related/<rel_id>/to-subtask", methods=["POST"])
@login_required
def related_to_subtask(task_id, rel_id):
    """Promote a related task into a sub-task of this task: reparent it
    under the current task and drop the now-redundant related link. The
    parent stays a task (no project conversion — a task may hold child
    tasks). Plain POST reloads to the subtasks section."""
    task = _editable_task_or_404(task_id)
    user = current_user._get_current_object()
    other = Item.objects(pk=rel_id, kind="task").first()
    # Owner-only on the task being absorbed. ``can_be_edited_by`` is an alias
    # of ``can_be_viewed_by``, so it would also accept a task merely delegated
    # to this user, or one they see through a shared project — reparenting it
    # under a task of their own moves it out of its owner's tree and, because
    # owning an ancestor grants the whole subtree, hands over its descendants
    # too. Nothing here required the two to be genuinely related, so this was
    # reachable for any editable task.
    if other and other.pk != task.pk and other.owner == user:
        # Reject cycles: the related task must not be an ancestor of this one.
        cur, depth = task.parent, 0
        is_ancestor = False
        while cur is not None and depth < 20:
            if cur.pk == other.pk:
                is_ancestor = True
                break
            cur, depth = cur.parent, depth + 1
        if not is_ancestor:
            other.parent = task
            other.save()
            task.unrelate_from(other)
    if request.headers.get("X-Partial") != "1":
        return redirect(url_for("tasks.detail", task_id=str(task.pk)) + "#subtasks")
    return _related_response(task)


def _dependencies_response(task):
    """Re-render the dependencies partial for AJAX callers, else redirect back
    to the detail page (no-JS fallback). Mirrors _related_response."""
    if request.headers.get("X-Partial") == "1":
        return render_template("tasks/_dependencies.html", task=task)
    return redirect(url_for("tasks.detail", task_id=str(task.pk)) + "#dependencies")


@tasks_bp.route("/<task_id>/dependencies", methods=["POST"])
@login_required
def dependency_add(task_id):
    """Make this task depend on another task (directed, blocking). The edge is
    stored only on this task, so viewing the other task is enough — you can
    wait on a task you can see but don't own. Rejects self-edges and cycles
    (add_dependency is a no-op for those)."""
    task = _editable_task_or_404(task_id)
    user = current_user._get_current_object()
    dep_id = (request.form.get("depends_on_id") or "").strip()
    other = Item.objects(pk=dep_id, kind="task").first() if dep_id else None
    if other and other.pk != task.pk and other.can_be_viewed_by(user):
        task.add_dependency(other)
    return _dependencies_response(task)


@tasks_bp.route("/<task_id>/dependencies/<dep_id>/remove", methods=["POST"])
@login_required
def dependency_remove(task_id, dep_id):
    """Drop a dependency edge (stored on this side only)."""
    task = _editable_task_or_404(task_id)
    task.remove_dependency(dep_id)
    return _dependencies_response(task)


def _graph_subtree_task_pks(root):
    """Every descendant pk under a project/folder ``root`` (BFS over ``parent``,
    same pattern as focus_pks). Used to scope the dependency graph to one
    project/folder — the dependency-declaring tasks are then intersected with
    this set."""
    out, frontier = [], [root.pk]
    while frontier:
        kids = list(Item.objects(parent__in=frontier).only("pk"))
        if not kids:
            break
        frontier = [k.pk for k in kids]
        out.extend(frontier)
    return out


@tasks_bp.route("/graph")
@login_required
def dependency_graph():
    """Workspace-wide dependency graph: every owned task that declares a
    dependency, plus the tasks they wait on, as a directed acyclic graph.
    Nodes + edges are serialised to JSON and laid out client-side by
    dep-graph.js (self-contained SVG, layered left→right).

    ``?scope=<project|folder pk>`` narrows the graph to the dependency-declaring
    tasks inside that container's subtree (their prerequisites are still shown
    even if they live outside, so a cross-project edge stays meaningful)."""
    user = current_user._get_current_object()
    from bson import ObjectId
    from application.services.containers import container_choices

    # Scope picker: owned projects/folders, breadcrumb-labelled.
    scope_choices = container_choices(user, selectable_kinds=("project", "folder"))
    scope_id = (request.args.get("scope") or "").strip()
    scope_item = None
    # A hand-typed / stale ``scope`` may not be a valid ObjectId — treat any
    # bad or unknown id as "no scope" rather than 500ing.
    if scope_id and ObjectId.is_valid(scope_id):
        scope_item = Item.objects(pk=scope_id, owner=user,
                                  kind__in=["project", "folder"]).first()
    # Base set: tasks the user owns that declare at least one dependency.
    q = Item.objects(owner=user, kind="task", depends_on__0__exists=True)
    if scope_item is not None:
        subtree = _graph_subtree_task_pks(scope_item)
        q = q.filter(pk__in=subtree) if subtree else Item.objects(pk__in=[])
    with_deps = list(q)

    node_ids, raw_edges = set(), []
    for t in with_deps:
        sid = str(t.pk)
        node_ids.add(sid)
        for d in (t._data.get("depends_on") or []):
            did = getattr(d, "id", None) or getattr(d, "pk", None)
            if did:
                raw_edges.append((sid, str(did)))
                node_ids.add(str(did))
    # Fetch every node in one query — dependency targets may have no deps of
    # their own, so they aren't in ``with_deps``. Purged/trashed targets simply
    # don't come back and their edges get dropped below.
    docs = {str(d.pk): d for d in Item.objects(pk__in=list(node_ids), kind="task")}
    nodes = [{
        "id": nid,
        "title": d.title,
        "status": d.status,
        "blocked": d.is_blocked(),
        "url": url_for("tasks.detail", task_id=nid),
    } for nid, d in docs.items()]
    edges = [{"from": s, "to": t} for (s, t) in raw_edges
             if s in docs and t in docs]
    return render_template("tasks/graph.html", nodes=nodes, edges=edges,
                           scope_choices=scope_choices, scope_id=scope_id,
                           scope_item=scope_item)


@tasks_bp.route("/<task_id>/subtasks", methods=["GET", "POST"])
@login_required
def subtasks(task_id):
    """List + add sub-tasks of a task. The parent stays a task (no
    ensure_container / project conversion) — a task can hold child tasks
    directly. GET/POST both return the ``_subtasks`` partial for AJAX
    callers; a plain POST redirects back to the detail page."""
    task = _editable_task_or_404(task_id)
    if request.method == "POST":
        raw = (request.form.get("title") or "").strip()[:500]
        if raw:
            from application.services.quick_parse import parse_task_text
            user = current_user._get_current_object()
            # Magic words work here just like Todoist-style quick-add:
            # ``today``/``heute`` & friends → due date, ``@name`` → assignee,
            # ``+tag`` / ``p1``-``p4`` / ``!``. We deliberately IGNORE any
            # ``#project`` token (parsed.project) — a subtask must stay
            # parented to THIS task, not get yanked into another project.
            parsed = parse_task_text(raw, user)
            sub = Item(
                owner=user, kind="task", parent=task, status="next",
                title=(parsed.title or raw)[:500],
                tags=list(parsed.tags),
                priority=parsed.priority,
                due_date=parsed.due_date,
                flagged=parsed.flagged,
            )
            if parsed.assignee:
                sub.assign_to(parsed.assignee, by_user=user)
            sub.save()
        if request.headers.get("X-Partial") != "1":
            return redirect(url_for("tasks.detail", task_id=str(task.pk)) + "#subtasks")
    return render_template("tasks/_subtasks.html", task=task)
