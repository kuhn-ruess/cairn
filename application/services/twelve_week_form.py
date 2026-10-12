"""The plan form of the 12 Week Year: read it, check it, and give it back.

The form is rendered from a plain STATE (dicts of the raw values), built
either from the saved cycle (:func:`state_from_cycle`) or from what was just
submitted (:func:`read_form`). A submit with a mistake renders the state it
came with, so nothing typed is ever lost, and every error is keyed to the
field it belongs to.

Tactic rows are keyed by an index the page assigns (``row`` lists them in
order), not by position in parallel lists: rows are added and removed in
the browser, and a week grid is a set of checkboxes, which a parallel list
cannot carry.
"""
from __future__ import annotations

from datetime import date

from application.models.twelve_week import MAX_GOALS, WEEKS, TwyGoal, TwyTactic, _short_id
from application.services.twelve_week import goal_ids

NEW_HABIT = "__new__"   # the picker's "create it in Habitify" choice


def _num_str(v) -> str:
    if v is None:
        return ""
    return f"{v:g}"


def state_from_cycle(cycle, start: date) -> dict:
    goals = [{"gid": g.gid, "title": g.title, "lag_target": _num_str(g.lag_target),
              "lag_current": _num_str(g.lag_current), "lag_unit": g.lag_unit or ""}
             for g in cycle.goals]
    while len(goals) < MAX_GOALS:
        goals.append({"gid": _short_id(), "title": "", "lag_target": "",
                      "lag_current": "", "lag_unit": ""})
    tactics = []
    for i, t in enumerate(cycle.tactics):
        tactics.append({
            "idx": str(i), "tid": t.tid, "title": t.title, "gids": goal_ids(t),
            "weeks": list(range(1, WEEKS + 1)) if t.recurring else sorted(t.weeks or []),
            "habit": t.habitify_id or "", "habit_manual": "",
            "habit_name": t.habitify_name or "",
            "manage": bool(t.habitify_manage), "per_week": str(t.per_week or 1)})
    return {"title": cycle.title, "vision": cycle.vision or "",
            "start": start.isoformat(), "goals": goals, "tactics": tactics}


def blank_tactic(idx: str) -> dict:
    return {"idx": idx, "tid": "", "title": "", "gids": [], "weeks": [],
            "habit": "", "habit_manual": "", "habit_name": "", "manage": False,
            "per_week": "1"}


def _parse_num(raw: str):
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None, False
    try:
        return float(raw), False
    except ValueError:
        return None, True


def read_form(form, cycle, habit_names: dict, *, has_key: bool):
    """Returns ``(state, errors, plan)``. ``plan`` is ``None`` when there are
    errors, else ``{"title", "vision", "start", "goals", "tactics"}`` with
    model objects ready to be put on the cycle."""
    errors: dict[str, str] = {}
    state = {"title": (form.get("title") or "").strip(),
             "vision": (form.get("vision") or "").strip(),
             "start": (form.get("start") or "").strip(),
             "goals": [], "tactics": []}
    if not state["title"]:
        errors["title"] = "Give the cycle a name."
    try:
        start = date.fromisoformat(state["start"])
    except ValueError:
        start = None
        errors["start"] = "Pick the day week 1 starts on."

    # Goals: three fixed slots.
    gids, titles = form.getlist("goal_gid"), form.getlist("goal_title")
    targets, currents = form.getlist("goal_lag_target"), form.getlist("goal_lag_current")
    units = form.getlist("goal_lag_unit")

    def at(lst, i):
        return (lst[i] if i < len(lst) else "") or ""

    goals: list[TwyGoal] = []
    for i in range(MAX_GOALS):
        g = {"gid": at(gids, i).strip() or _short_id(), "title": at(titles, i).strip(),
             "lag_target": at(targets, i).strip(), "lag_current": at(currents, i).strip(),
             "lag_unit": at(units, i).strip()[:40]}
        state["goals"].append(g)
        target, bad_t = _parse_num(g["lag_target"])
        current, bad_c = _parse_num(g["lag_current"])
        if bad_t:
            errors[f"goal-{i}-lag_target"] = "A number, please."
        if bad_c:
            errors[f"goal-{i}-lag_current"] = "A number, please."
        if g["title"]:
            goals.append(TwyGoal(gid=g["gid"], title=g["title"][:200],
                                 lag_target=target, lag_current=current,
                                 lag_unit=g["lag_unit"]))
        elif g["lag_target"] or g["lag_current"]:
            errors[f"goal-{i}-title"] = "Name the goal these numbers belong to."
    named = {g.gid for g in goals}

    # Tactics: one row per index the page sent.
    tactics: list[TwyTactic] = []
    seen = set()
    for idx in form.getlist("row"):
        idx = (idx or "").strip()
        if not idx or idx in seen:
            continue
        seen.add(idx)
        p = f"t-{idx}-"
        weeks = sorted({int(w) for w in form.getlist(p + "week")
                        if str(w).isdigit() and 1 <= int(w) <= WEEKS})
        row = {"idx": idx, "tid": (form.get(p + "tid") or "").strip(),
               "title": (form.get(p + "title") or "").strip(),
               "gids": list(dict.fromkeys(g.strip() for g in form.getlist(p + "goal")
                                          if g and g.strip())), "weeks": weeks,
               "habit": (form.get(p + "habit") or "").strip(),
               "habit_manual": (form.get(p + "habit_manual") or "").strip(),
               "habit_name": (form.get(p + "habit_name") or "").strip(),
               "manage": bool(form.get(p + "manage")),
               "per_week": (form.get(p + "per_week") or "1").strip()}
        state["tactics"].append(row)
        habit = row["habit_manual"] or row["habit"]
        if not row["title"] and not weeks and not habit:
            continue                                  # an empty row
        if not row["title"]:
            errors[p + "title"] = "Name the tactic."
        if not weeks:
            errors[p + "weeks"] = "Click the week(s) it is planned for."
        if any(g not in named for g in row["gids"]):
            errors[p + "goal"] = "A picked goal has no name (any more); name it or unpick it."
        try:
            per_week = int(row["per_week"])
            if not 1 <= per_week <= 7:
                raise ValueError
        except ValueError:
            per_week = 1
            errors[p + "per_week"] = "1 to 7."
        manage = row["manage"]
        if habit == NEW_HABIT:
            if not has_key:
                errors[p + "habit"] = "Connect Habitify first (Settings > Habitify)."
            habit, manage = "", True
        old = cycle.tactic(row["tid"]) if row["tid"] else None
        if old is not None and old.habitify_id and old.habitify_id == habit:
            name = habit_names.get(habit) or old.habitify_name
            hstart, hend = old.habitify_start, old.habitify_end
            hfreq = old.habitify_per_week
        else:
            name, hstart, hend = habit_names.get(habit, ""), "", ""
            hfreq = 0
        if not habit and not manage:
            name = ""
        tactics.append(TwyTactic(
            tid=row["tid"] or _short_id(), gids=[g for g in row["gids"] if g in named],
            gid=old.gid if old else "", goals_listed=True,
            title=row["title"][:200], recurring=len(weeks) == WEEKS, weeks=weeks,
            per_week=per_week, log=list(old.log) if old else [],
            source=old.source if old else "",
            habitify_id=habit[:100], habitify_name=name[:200],
            habitify_manage=manage, habitify_start=hstart, habitify_end=hend,
            habitify_per_week=hfreq))

    if not goals and not tactics:
        errors["goals"] = "Set at least one goal or one habit."
    if errors:
        return state, errors, None
    return state, errors, {"title": state["title"][:200], "vision": state["vision"],
                           "start": start, "goals": goals, "tactics": tactics}
