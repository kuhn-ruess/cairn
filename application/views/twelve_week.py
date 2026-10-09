"""The 12 Week Year (premium). It is also where the habits live now: the
rolling habits of the old Habits area moved in as habit tactics, see
``services/habit_migration.py``, which runs once per user on the first visit.

One cycle is one document (``models/twelve_week.py``), so every page here is a
single read. Ticks, "+1", the week note, the lag measures and the Habitify
read all answer with the cycle body partial, which ``twelve-week.js`` swaps in
place: Back stays clean and the page keeps its scroll position.

Forms never lose what was typed: a submit with a mistake is rendered again
from what it carried, with the error beside the field
(``services/twelve_week_form.py``).
"""
from __future__ import annotations

from datetime import date, datetime

from flask import (
    Blueprint, abort, flash, g, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from application.models.twelve_week import (
    MAX_GOALS, WEEKS, TwelveWeekYear, TwyTactic,
)
from application.services import habitify
from application.services import twelve_week as tw
from application.services import twelve_week_form as twf
from application.services.permissions import premium_required
from application.services.tz import user_zone

twelve_week_bp = Blueprint("twelve_week", __name__,
                           url_prefix="/habits/12-week-year")

BLANK_TACTIC_ROWS = 2


def _today() -> date:
    return datetime.now(user_zone(current_user)).date()


def _owned(cycle_id: str) -> TwelveWeekYear:
    cycle = TwelveWeekYear.objects(
        id=cycle_id, owner=current_user._get_current_object()).first()
    if cycle is None:
        abort(404)
    # The single goal link of the first version becomes the goal list.
    if tw.backfill_goal_ids(cycle):
        cycle.save()
    return cycle


def _shown_week(cycle, today: date) -> int:
    """The week to show: ``?week=`` when valid, else the current one, clamped
    into 1..13 so a cycle not yet started opens on week 1."""
    raw = request.values.get("week", "")
    if raw.isdigit() and 1 <= int(raw) <= tw.REVIEW_WEEK:
        return int(raw)
    return min(max(tw.week_of(cycle, today), 1), tw.REVIEW_WEEK)


def _row(cycle, t, week):
    return {"t": t, "done": tw.is_done(cycle, t, week),
            "manual": tw.key(t.tid, week) in (cycle.done or []),
            "count": tw.count(cycle, t, week),
            "counter": tw.counted(t) and not t.habitify_id}


def _body_context(cycle, week: int, today: date, lag_errors=None) -> dict:
    board = tw.scoreboard(cycle, today)
    rows = []
    for goal in cycle.goals:
        # A habit serving several goals is listed under each of them and
        # counts in each goal's score; the week's score counts it once.
        rows.append({"g": goal, "progress": tw.goal_progress(goal),
                     "score": tw.goal_week_score(cycle, goal.gid, week) if week <= WEEKS else None,
                     "average": tw.goal_average(cycle, goal.gid, today),
                     "tactics": [_row(cycle, t, week) for t in cycle.tactics
                                 if tw.serves(t, goal.gid) and tw.planned(t, week)]})
    named = {goal.gid for goal in cycle.goals}
    habits = [_row(cycle, t, week) for t in cycle.tactics
              if not named.intersection(tw.goal_ids(t)) and tw.planned(t, week)]
    goal_titles = {goal.gid: goal.title for goal in cycle.goals}
    first, last = tw.week_range(cycle, week)
    return {
        "c": cycle, "week": week, "board": board, "rows": rows, "habits": habits,
        "this_week": tw.week_score(cycle, week) if week <= WEEKS else None,
        "first": first, "last": last, "end": tw.end_date(cycle),
        "review_week": tw.REVIEW_WEEK, "weeks": WEEKS,
        "has_habitify": bool(current_user.habitify_api_key),
        "mapped": any(t.habitify_id for t in cycle.tactics),
        "lag_errors": lag_errors or {}, "goal_titles": goal_titles,
        "pending": len(habitify.pending_changes(cycle)) if current_user.habitify_api_key else 0,
    }


def _body(cycle, week: int, **kw):
    return render_template("twelve_week/_body.html",
                           **_body_context(cycle, week, _today(), **kw))


@twelve_week_bp.route("/")
@login_required
@premium_required
def index():
    from application.services.habit_migration import ensure_migrated
    stats = ensure_migrated(current_user._get_current_object())
    if stats and stats["tactics"]:
        flash(f"Your {stats['tactics']} habit(s) moved in here, with "
              f"{stats['checkins']} check-in(s), as habits of the cycle below.",
              "info")
    today = _today()
    cycles = list(TwelveWeekYear.objects(
        owner=current_user._get_current_object()).only(
            "title", "start", "goals", "tactics", "done", "habitify_counts"))
    cards = [(c, tw.week_of(c, today), tw.scoreboard(c, today)) for c in cycles]
    return render_template("twelve_week/index.html", cards=cards,
                           review_week=tw.REVIEW_WEEK)


@twelve_week_bp.route("/new", methods=["GET", "POST"])
@login_required
@premium_required
def create():
    today = _today()
    owner = current_user._get_current_object()
    latest = TwelveWeekYear.objects(owner=owner).order_by("-start").first()
    carry = [t for t in (latest.tactics if latest else [])
             if not any(latest.goal(gid) for gid in tw.goal_ids(t))]
    values = {"title": "", "vision": "", "start": tw.monday_of(today).isoformat(),
              "carry": True}
    errors = {}
    if request.method == "POST":
        values = {"title": (request.form.get("title") or "").strip(),
                  "vision": (request.form.get("vision") or "").strip(),
                  "start": (request.form.get("start") or "").strip(),
                  "carry": bool(request.form.get("carry"))}
        if not values["title"]:
            errors["title"] = "Give the cycle a name."
        try:
            start = date.fromisoformat(values["start"])
        except ValueError:
            errors["start"] = "Pick the day week 1 starts on."
        if not errors:
            cycle = TwelveWeekYear(owner=owner, title=values["title"][:200],
                                   vision=values["vision"], start=tw.to_start(start))
            if values["carry"]:
                # The habits go on: same habit, same Habitify link, fresh weeks.
                cycle.tactics = [TwyTactic(
                    gids=[], goals_listed=True, title=t.title, recurring=True,
                    weeks=list(range(1, WEEKS + 1)), per_week=t.per_week,
                    habitify_id=t.habitify_id, habitify_name=t.habitify_name,
                    habitify_manage=t.habitify_manage, source=t.source)
                    for t in carry]
            cycle.save()
            flash("Cycle created. Now set its goals and tactics.", "success")
            return redirect(url_for("twelve_week.plan", cycle_id=cycle.pk))
    return render_template("twelve_week/new.html", values=values, errors=errors,
                           carry=carry, latest=latest)


@twelve_week_bp.route("/<cycle_id>")
@login_required
@premium_required
def detail(cycle_id):
    cycle = _owned(cycle_id)
    today = _today()
    ctx = _body_context(cycle, _shown_week(cycle, today), today)
    return render_template("twelve_week/detail.html", **ctx)


def _habit_names() -> dict:
    """The user's Habitify habits for the picker, read from Habitify once per
    request. Empty when no key is set or Habitify does not answer; the
    picker then offers the manual id field and says why."""
    if "habitify_habits" not in g:
        g.habitify_habits, g.habitify_error = {}, ""
        key = _api_key()
        if key:
            try:
                g.habitify_habits = {h["id"]: h["name"]
                                     for h in habitify.list_habits(key)}
            except habitify.HabitifyError as exc:
                g.habitify_error = str(exc)
    return g.habitify_habits


def _api_key() -> str:
    from application.services.crypto import decrypt_for_user
    return decrypt_for_user(current_user, current_user.habitify_api_key)


def _render_plan(cycle, state, errors, status=200):
    habits = _habit_names()
    named_goals = [x for x in state["goals"] if x["title"]]
    next_idx = max([int(t["idx"]) for t in state["tactics"] if t["idx"].isdigit()] + [-1]) + 1
    tactics = list(state["tactics"])
    if not errors:
        for i in range(BLANK_TACTIC_ROWS):
            tactics.append(twf.blank_tactic(str(next_idx + i)))
        next_idx += BLANK_TACTIC_ROWS
    return render_template(
        "twelve_week/plan.html", c=cycle, s=state, tactics=tactics,
        errors=errors, named_goals=named_goals, next_idx=next_idx,
        habits=habits, habitify_error=g.get("habitify_error", ""),
        has_habitify=bool(current_user.habitify_api_key),
        weeks=WEEKS, new_habit=twf.NEW_HABIT, max_goals=MAX_GOALS), status


@twelve_week_bp.route("/<cycle_id>/plan", methods=["GET", "POST"])
@login_required
@premium_required
def plan(cycle_id):
    cycle = _owned(cycle_id)
    if request.method == "GET":
        return _render_plan(cycle, twf.state_from_cycle(cycle, tw.as_date(cycle.start)), {})

    state, errors, parsed = twf.read_form(
        request.form, cycle, _habit_names(),
        has_key=bool(current_user.habitify_api_key))
    if errors:
        return _render_plan(cycle, state, errors, status=422)
    cycle.title = parsed["title"]
    cycle.vision = parsed["vision"]
    cycle.start = tw.to_start(parsed["start"])
    cycle.goals = parsed["goals"]
    cycle.tactics = parsed["tactics"]
    # Ticks and counts of tactics that no longer exist go with them.
    live = {t.tid for t in cycle.tactics}
    cycle.done = [k for k in (cycle.done or []) if k.split(":")[0] in live]
    cycle.habitify_counts = {k: v for k, v in (cycle.habitify_counts or {}).items()
                             if k.split(":")[0] in live}
    cycle.save()

    if habitify.pending_changes(cycle):
        flash("Plan saved. Check what goes to Habitify.", "success")
        return redirect(url_for("twelve_week.habitify_preview", cycle_id=cycle.pk))
    flash("Plan saved.", "success")
    return redirect(url_for("twelve_week.detail", cycle_id=cycle.pk))


@twelve_week_bp.route("/<cycle_id>/habitify", methods=["GET", "POST"])
@login_required
@premium_required
def habitify_preview(cycle_id):
    """What the saved plan would change in Habitify, and the button that does
    it. Nothing is written to Habitify without this page's POST."""
    cycle = _owned(cycle_id)
    changes = habitify.pending_changes(cycle)
    if request.method == "POST":
        done, errs = habitify.apply_changes(
            cycle, current_user._get_current_object(), changes)
        for e in errs:
            flash(e, "danger")
        if done:
            flash(f"Habitify updated: {done} habit(s).", "success")
        if errs:
            return redirect(url_for("twelve_week.habitify_preview", cycle_id=cycle.pk))
        return redirect(url_for("twelve_week.detail", cycle_id=cycle.pk))
    if not changes:
        return redirect(url_for("twelve_week.detail", cycle_id=cycle.pk))
    return render_template("twelve_week/habitify.html", c=cycle, changes=changes,
                           has_key=bool(current_user.habitify_api_key))


def _week_arg() -> int:
    try:
        return int(request.form.get("week", ""))
    except ValueError:
        abort(400)


@twelve_week_bp.route("/<cycle_id>/toggle", methods=["POST"])
@login_required
@premium_required
def toggle(cycle_id):
    cycle = _owned(cycle_id)
    week = _week_arg()
    if tw.toggle_done(cycle, request.form.get("tid", ""), week) is None:
        abort(400)
    cycle.save()
    return _body(cycle, week)


@twelve_week_bp.route("/<cycle_id>/log", methods=["POST"])
@login_required
@premium_required
def log(cycle_id):
    """"+1" or "undo" on a counted tactic (a habit done several times a week)."""
    cycle = _owned(cycle_id)
    week = _week_arg()
    tactic = cycle.tactic(request.form.get("tid", ""))
    if tactic is None or not tw.planned(tactic, week):
        abort(400)
    if request.form.get("undo"):
        tw.undo_log(cycle, tactic, week)
    else:
        tw.add_log(cycle, tactic, week, _today())
    cycle.save()
    return _body(cycle, week)


@twelve_week_bp.route("/<cycle_id>/note", methods=["POST"])
@login_required
@premium_required
def note(cycle_id):
    cycle = _owned(cycle_id)
    week = _week_arg()
    if not 1 <= week <= tw.REVIEW_WEEK:
        abort(400)
    notes = dict(cycle.week_notes or {})
    text = (request.form.get("note") or "").strip()
    if text:
        notes[str(week)] = text[:5000]
    else:
        notes.pop(str(week), None)
    cycle.week_notes = notes
    cycle.save()
    return _body(cycle, week)


@twelve_week_bp.route("/<cycle_id>/lag", methods=["POST"])
@login_required
@premium_required
def lag(cycle_id):
    """Update where a goal's lag measure stands now. Something that is not a
    number is said beside the field and stays in it."""
    cycle = _owned(cycle_id)
    goal = cycle.goal(request.form.get("gid", ""))
    if goal is None:
        abort(400)
    raw = (request.form.get("lag_current") or "").strip()
    value, bad = twf._parse_num(raw)
    week = _shown_week(cycle, _today())
    if bad:
        return _body(cycle, week, lag_errors={goal.gid: (raw, "A number, please.")}), 422
    goal.lag_current = value
    cycle.save()
    return _body(cycle, week)


@twelve_week_bp.route("/<cycle_id>/sync", methods=["POST"])
@login_required
@premium_required
def sync(cycle_id):
    """Pull completions from Habitify now: the whole cycle so far."""
    cycle = _owned(cycle_id)
    habitify.sync_cycle(cycle, current_user._get_current_object(),
                        today=_today(), full=True)
    return _body(cycle, _shown_week(cycle, _today()))


@twelve_week_bp.route("/<cycle_id>/delete", methods=["POST"])
@login_required
@premium_required
def delete(cycle_id):
    _owned(cycle_id).delete()
    flash("Cycle deleted.", "success")
    return redirect(url_for("twelve_week.index"))
