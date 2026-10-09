"""The 12 Week Year, inside the Habits area (premium, like the habits).

One cycle is one document (``models/twelve_week.py``), so every page here is a
single read. Ticks, the week note, the lag measures and the Habitify sync all
answer with the cycle body partial, which ``twelve-week.js`` swaps in place:
Back stays clean and the page keeps its scroll position.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from application.models.twelve_week import (
    MAX_GOALS, WEEKS, TwelveWeekYear, TwyGoal, TwyTactic, _short_id,
)
from application.services import twelve_week as tw
from application.services.permissions import premium_required
from application.services.tz import user_zone

twelve_week_bp = Blueprint("twelve_week", __name__,
                           url_prefix="/habits/12-week-year")

BLANK_TACTIC_ROWS = 4


def _today() -> date:
    return datetime.now(user_zone(current_user)).date()


def _owned(cycle_id: str) -> TwelveWeekYear:
    cycle = TwelveWeekYear.objects(
        id=cycle_id, owner=current_user._get_current_object()).first()
    if cycle is None:
        abort(404)
    return cycle


def _shown_week(cycle, today: date) -> int:
    """The week to show: ``?week=`` when valid, else the current one, clamped
    into 1..13 so a cycle not yet started opens on week 1."""
    raw = request.values.get("week", "")
    if raw.isdigit() and 1 <= int(raw) <= tw.REVIEW_WEEK:
        return int(raw)
    return min(max(tw.week_of(cycle, today), 1), tw.REVIEW_WEEK)


def _body_context(cycle, week: int, today: date) -> dict:
    board = tw.scoreboard(cycle, today)
    rows = []
    for g in cycle.goals:
        tactics = [
            {"t": t, "done": tw.is_done(cycle, t, week),
             "manual": tw.key(t.tid, week) in (cycle.done or []),
             "count": int((cycle.habitify_counts or {}).get(tw.key(t.tid, week), 0))}
            for t in cycle.tactics if t.gid == g.gid and tw.planned(t, week)
        ]
        rows.append({"g": g, "progress": tw.goal_progress(g), "tactics": tactics})
    first, last = tw.week_range(cycle, week)
    return {
        "c": cycle, "week": week, "board": board, "rows": rows,
        "this_week": tw.week_score(cycle, week) if week <= WEEKS else None,
        "first": first, "last": last, "end": tw.end_date(cycle),
        "review_week": tw.REVIEW_WEEK, "weeks": WEEKS,
        "has_habitify": bool(current_user.habitify_api_key),
        "mapped": any(t.habitify_id for t in cycle.tactics),
    }


def _body(cycle, week: int):
    return render_template("twelve_week/_body.html",
                           **_body_context(cycle, week, _today()))


@twelve_week_bp.route("/")
@login_required
@premium_required
def index():
    today = _today()
    cycles = list(TwelveWeekYear.objects(
        owner=current_user._get_current_object()).only(
            "title", "start", "goals", "tactics", "done", "habitify_counts"))
    cards = [(c, tw.week_of(c, today), tw.scoreboard(c, today)) for c in cycles]
    return render_template("twelve_week/index.html", cards=cards,
                           review_week=tw.REVIEW_WEEK)


def _read_start(raw: str, fallback: date) -> date:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return fallback


@twelve_week_bp.route("/new", methods=["GET", "POST"])
@login_required
@premium_required
def create():
    today = _today()
    default_start = tw.monday_of(today)
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        if not title:
            flash("Give the cycle a name.", "warning")
        else:
            start = _read_start(request.form.get("start"), default_start)
            cycle = TwelveWeekYear(
                owner=current_user._get_current_object(), title=title[:200],
                vision=(request.form.get("vision") or "").strip(),
                start=tw.to_start(start)).save()
            flash("Cycle created. Now set its goals and tactics.", "success")
            return redirect(url_for("twelve_week.plan", cycle_id=cycle.pk))
    return render_template("twelve_week/new.html", default_start=default_start)


@twelve_week_bp.route("/<cycle_id>")
@login_required
@premium_required
def detail(cycle_id):
    cycle = _owned(cycle_id)
    today = _today()
    ctx = _body_context(cycle, _shown_week(cycle, today), today)
    return render_template("twelve_week/detail.html", **ctx)


def _read_plan(cycle) -> tuple[list[TwyGoal], list[TwyTactic], str | None]:
    """Goals and tactics from the plan form. Goal slots carry their gid (a
    fresh one for an empty slot), so a tactic row can name its goal before
    that goal was ever saved. An empty goal title drops the goal and the
    tactics filed under it."""
    goals: list[TwyGoal] = []
    titles = request.form.getlist("goal_title")
    gids = request.form.getlist("goal_gid")
    targets = request.form.getlist("goal_lag_target")
    currents = request.form.getlist("goal_lag_current")
    units = request.form.getlist("goal_lag_unit")
    for i, title in enumerate(titles[:MAX_GOALS]):
        title = (title or "").strip()
        if not title:
            continue
        goals.append(TwyGoal(
            gid=(gids[i] if i < len(gids) and gids[i] else _short_id()),
            title=title[:200],
            lag_target=_num(targets, i), lag_current=_num(currents, i),
            lag_unit=((units[i] if i < len(units) else "") or "").strip()[:40]))
    if not goals:
        return [], [], "Set at least one goal."
    known = {g.gid for g in goals}

    tactics: list[TwyTactic] = []
    f = request.form
    rows = zip(f.getlist("tactic_tid"), f.getlist("tactic_title"),
               f.getlist("tactic_goal"), f.getlist("tactic_mode"),
               f.getlist("tactic_weeks"), f.getlist("tactic_habitify"),
               f.getlist("tactic_per_week"))
    names = _habit_names()
    for tid, title, gid, mode, weeks, hab, per_week in rows:
        title = (title or "").strip()
        if not title or gid not in known:
            continue
        recurring = mode == "recurring"
        week_list = [] if recurring else tw.parse_weeks(weeks)
        if not recurring and not week_list:
            return goals, tactics, f"Tactic “{title}” needs its week(s), or make it weekly."
        hab = (hab or "").strip()[:100]
        old = cycle.tactic(tid) if tid else None
        try:
            pw = min(7, max(1, int(per_week)))
        except (TypeError, ValueError):
            pw = 1
        tactics.append(TwyTactic(
            tid=tid or _short_id(), gid=gid, title=title[:200],
            recurring=recurring, weeks=week_list, habitify_id=hab,
            habitify_name=names.get(hab) or (old.habitify_name if old and old.habitify_id == hab else ""),
            per_week=pw))
    return goals, tactics, None


def _num(values, i):
    try:
        raw = (values[i] or "").strip().replace(",", ".")
        return float(raw) if raw else None
    except (IndexError, ValueError):
        return None


def _habit_names() -> dict:
    """Names of the user's Habitify habits for the picker, cached on the
    request. Empty when no key is set or Habitify does not answer: the plan
    form then keeps the ids it already has and says so."""
    from flask import g
    if "habitify_habits" not in g:
        g.habitify_habits, g.habitify_error = {}, ""
        key = _api_key()
        if key:
            from application.services import habitify
            try:
                g.habitify_habits = {h["id"]: h["name"]
                                     for h in habitify.list_habits(key)}
            except habitify.HabitifyError as exc:
                g.habitify_error = str(exc)
    return g.habitify_habits


def _api_key() -> str:
    from application.services.crypto import decrypt_for_user
    return decrypt_for_user(current_user, current_user.habitify_api_key)


@twelve_week_bp.route("/<cycle_id>/plan", methods=["GET", "POST"])
@login_required
@premium_required
def plan(cycle_id):
    from flask import g
    cycle = _owned(cycle_id)
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        goals, tactics, error = _read_plan(cycle)
        if not title:
            error = "Give the cycle a name."
        if error:
            flash(error, "warning")
        else:
            cycle.title = title[:200]
            cycle.vision = (request.form.get("vision") or "").strip()
            cycle.start = tw.to_start(_read_start(request.form.get("start"),
                                                  tw.as_date(cycle.start)))
            cycle.goals = goals
            cycle.tactics = tactics
            # Ticks and counts of tactics that no longer exist go with them.
            live = {t.tid for t in tactics}
            cycle.done = [k for k in (cycle.done or []) if k.split(":")[0] in live]
            cycle.habitify_counts = {k: v for k, v in (cycle.habitify_counts or {}).items()
                                     if k.split(":")[0] in live}
            cycle.save()
            flash("Plan saved.", "success")
            if request.form.get("again"):
                return redirect(url_for("twelve_week.plan", cycle_id=cycle.pk))
            return redirect(url_for("twelve_week.detail", cycle_id=cycle.pk))
    habits = _habit_names()
    slots = list(cycle.goals) + [None] * (MAX_GOALS - len(cycle.goals))
    goal_slots = [{"g": g_, "gid": g_.gid if g_ else _short_id()} for g_ in slots]
    return render_template(
        "twelve_week/plan.html", c=cycle, goal_slots=goal_slots,
        tactic_rows=list(cycle.tactics) + [None] * BLANK_TACTIC_ROWS,
        habits=habits, habitify_error=g.get("habitify_error", ""),
        has_habitify=bool(current_user.habitify_api_key),
        format_weeks=tw.format_weeks, start=tw.as_date(cycle.start))


@twelve_week_bp.route("/<cycle_id>/toggle", methods=["POST"])
@login_required
@premium_required
def toggle(cycle_id):
    cycle = _owned(cycle_id)
    try:
        week = int(request.form.get("week", ""))
    except ValueError:
        abort(400)
    if tw.toggle_done(cycle, request.form.get("tid", ""), week) is None:
        abort(400)
    cycle.save()
    return _body(cycle, week)


@twelve_week_bp.route("/<cycle_id>/note", methods=["POST"])
@login_required
@premium_required
def note(cycle_id):
    cycle = _owned(cycle_id)
    try:
        week = int(request.form.get("week", ""))
    except ValueError:
        abort(400)
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
    """Update where a goal's lag measure stands now."""
    cycle = _owned(cycle_id)
    goal = cycle.goal(request.form.get("gid", ""))
    if goal is None:
        abort(400)
    goal.lag_current = _num([request.form.get("lag_current", "")], 0)
    cycle.save()
    return _body(cycle, _shown_week(cycle, _today()))


@twelve_week_bp.route("/<cycle_id>/sync", methods=["POST"])
@login_required
@premium_required
def sync(cycle_id):
    """Pull completions from Habitify now: the whole cycle so far."""
    from application.services import habitify
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
