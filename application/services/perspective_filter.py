"""Evaluate a ``CustomPerspective`` filter tree against a queryset of tasks.

The user defines the filter in the UI; we walk the tree per-task in
Python because the conditions cross many fields and computed values
(``available``, ``blocked``) that can't easily be expressed as a single
MongoDB query.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Iterable


def _decode_value(raw):
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _task_field(task, field: str):
    """Return the raw attribute used for comparison."""
    if field == "available":
        return not task.is_blocked()
    if field == "blocked":
        return task.is_blocked()
    if field in ("has_due", "has_plan", "has_defer"):
        return bool(getattr(task, field.replace("has_", "") + "_date"))
    if field == "due":
        return task.due_date
    if field == "plan":
        return task.plan_date
    if field == "defer":
        return task.defer_date
    if field == "tag":
        # task.tags is a list of tag NAMES now.
        return list(task.tags or [])
    if field == "project":
        return str(task.project.pk) if task.project else None
    if field == "kind":
        return task.kind
    if field == "repeating":
        return bool(task.repeat_every_days)
    if field == "assigned":
        # True for tasks assigned to anyone (i.e. shared work). The
        # comparison happens by field with op is_true/is_false.
        return task.assigned_to is not None
    if field == "note":
        return task.note or ""
    if field == "created":
        return task.created_at
    if field == "updated":
        return task.updated_at
    return getattr(task, field, None)


def _date_check(task_date, op: str, value) -> bool:
    if task_date is None:
        return op in ()  # always false when no date
    if hasattr(task_date, "tzinfo") and task_date.tzinfo is None:
        task_date = task_date.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if op == "overdue":
        return task_date < now
    if op == "in_past":
        return task_date < now
    if op == "before":
        try:
            ref = datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return False
        return task_date < ref
    if op == "after":
        try:
            ref = datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return False
        return task_date > ref
    if op == "on":
        try:
            ref = datetime.fromisoformat(str(value)).date()
        except (ValueError, TypeError):
            return False
        return task_date.date() == ref
    if op == "within_days":
        try:
            n = int(value)
        except (TypeError, ValueError):
            return False
        return today <= task_date <= today + timedelta(days=n)
    return False


def _eval_leaf(task, clause) -> bool:
    field = clause.field
    op = clause.op
    val = _decode_value(clause.value)
    actual = _task_field(task, field)

    # Boolean field shortcuts
    if op == "is_true":
        return bool(actual)
    if op == "is_false":
        return not bool(actual)

    # Status
    if field == "status":
        if op == "is":
            return actual == val
        if op == "is_not":
            return actual != val
        if op == "in":
            return actual in (val or [])

    # Priority
    if field == "priority":
        try:
            target = int(val) if val is not None else None
        except (TypeError, ValueError):
            return False
        if op == "is":
            return actual == target
        if op == "lte":
            return actual is not None and target is not None and actual <= target
        if op == "gte":
            return actual is not None and target is not None and actual >= target

    # Tag
    if field == "tag":
        # value is the tag NAME (post-refactor); actual is task.tags (names).
        tag_name = str(val) if val is not None else ""
        has = tag_name in (actual or [])
        if op == "has":
            return has
        if op == "not_has":
            return not has

    # Project
    if field == "project":
        if op == "is":
            return actual == str(val)
        if op == "is_descendant_of":
            # Walk task.project's parent chain to see if `val` is an ancestor
            if not task.project:
                return False
            cur = task.project
            depth = 0
            while cur and depth < 12:
                if str(cur.pk) == str(val):
                    return True
                cur = cur.parent
                depth += 1
            return False

    # Item kind
    if field == "kind":
        if op == "is":
            return actual == val
        if op == "is_not":
            return actual != val

    # Date fields
    if field in ("due", "plan", "defer", "created", "updated"):
        return _date_check(actual, op, val)

    # Title / note free-text
    if field in ("title", "note"):
        if op == "contains":
            return actual and str(val).lower() in actual.lower()
        if op == "equals":
            return actual == val

    return False


def _eval(task, clause) -> bool:
    if not clause:
        return True
    if clause.kind == "group":
        op = clause.group_op or "and"
        if not clause.children:
            return True
        results = [_eval(task, c) for c in clause.children]
        return all(results) if op == "and" else any(results)
    return _eval_leaf(task, clause)


def apply_filter(tasks: Iterable, perspective) -> list:
    """Return the subset of ``tasks`` matching the perspective's filter."""
    if not perspective.clauses:
        return list(tasks)
    op = perspective.root_op or "and"
    out = []
    for t in tasks:
        results = [_eval(t, c) for c in perspective.clauses]
        if (all(results) if op == "and" else any(results)):
            out.append(t)
    return out
