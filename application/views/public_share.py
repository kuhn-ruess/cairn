"""Read-only public project dashboard, addressed by an opaque share token.

URL: ``/p/<token>`` — no login required. Shows the project's non-internal
tasks grouped by status, plus their non-internal comments. The token is
revealed only to the project owner via the project detail page.
"""
from collections import OrderedDict
from datetime import datetime

from flask import Blueprint, abort, render_template, request
from application.models.item import Item

public_bp = Blueprint("public", __name__, url_prefix="/p")

# Outward-facing status labels — GTD-specific terms ("inbox", "next",
# "someday") mean nothing to external viewers, so we surface neutral
# project-management words instead.
STATUS_ORDER = [
    ("next",      "In progress",  "bi-play-circle"),
    ("open",      "Open",         "bi-circle"),
    ("waiting",   "Waiting",      "bi-hourglass-split"),
    ("review",    "In review",    "bi-eye"),
    ("scheduled", "Scheduled",    "bi-calendar-event"),
    ("someday",   "Later",        "bi-cloud"),
    ("done",      "Done",         "bi-check2-all"),
    ("dropped",   "Cancelled",    "bi-x-circle"),
]

PUBLIC_STATUS_LABEL = {key: label for key, label, _ in STATUS_ORDER}

PROJECT_STATUS_LABEL = {
    "active":    "In progress",
    "on_hold":   "On hold",
    "completed": "Completed",
    "dropped":   "Cancelled",
}

def _project_or_404(token):
    proj = Item.objects(share_token=token, share_enabled=True).first()
    if not proj:
        abort(404)
    return proj

def _allowed_project_ids(proj):
    """Walk the project's subtree, skipping any container marked
    ``is_internal=True`` (and its descendants). The is_internal flag
    is enforced at every level — without this, an internal sub-project
    would still surface its name (and any non-internal tasks beneath)
    via the public dashboard.
    """
    if proj.is_internal:
        return []
    ids = [proj.pk]
    pending = [proj.pk]
    while pending:
        # Batch one BFS layer per query so the lookup is O(depth) Mongo
        # round-trips instead of O(items).
        children = list(Item.objects(parent__in=pending,
                                       kind__in=["project", "section", "folder"],
                                       is_internal__ne=True).only("id"))
        pending = [c.pk for c in children]
        ids.extend(pending)
    return ids

@public_bp.route("/<token>")
def project(token):
    proj = _project_or_404(token)

    # All non-internal tasks anywhere under this project (incl. sections).
    project_ids = _allowed_project_ids(proj)
    tasks = list(Item.objects(kind="task", parent__in=project_ids,
                                is_internal__ne=True)
                 .order_by("+status", "+sort_order"))

    groups = OrderedDict()
    for status, label, icon in STATUS_ORDER:
        groups[status] = {"label": label, "icon": icon, "tasks": []}
    for t in tasks:
        bucket = groups.get(t.status)
        if bucket is None:
            groups[t.status] = {"label": t.status, "icon": "bi-dash", "tasks": []}
            bucket = groups[t.status]
        bucket["tasks"].append(t)

    # Strip empty buckets so the dashboard isn't cluttered.
    groups = {k: v for k, v in groups.items() if v["tasks"]}

    return render_template(
        "public/project.html",
        project=proj,
        groups=groups,
        total=len(tasks),
        token=token,
        project_status_label=PROJECT_STATUS_LABEL.get(proj.status, proj.status),
    )

@public_bp.route("/time/<token>")
def project_time(token):
    """Customer-facing read-only time sheet for one billing project. Hours and
    notes only — never rates or amounts. Addressed by an opaque, revocable
    token on the TimeTrackingProject."""
    from application.models.timetracking import TimeTrackingProject
    from application.services.timetracking import public_project_report
    proj = TimeTrackingProject.objects(
        share_token=token, share_enabled=True).first()
    if not proj:
        abort(404)
    # The running month is what a customer opens this for; "all" is a click
    # away and stays bookmarkable. No argument at all therefore means *now*,
    # not "everything ever", which used to open on years of history.
    from application.services.tz import user_zone

    raw = (request.args.get("month") or "").strip()
    if raw == "all":
        month = None
    elif len(raw) == 7:
        month = raw
    else:
        month = datetime.now(user_zone(None)).strftime("%Y-%m")
    data = public_project_report(
        proj, month, group_by=(request.args.get("by") or "user").strip(),
        who=(request.args.get("who") or "").strip())
    return render_template("public/project_time.html", project=proj,
                           token=token, **data)


@public_bp.route("/time/<token>.csv")
def project_time_export(token):
    """The same time sheet as a spreadsheet download.

    UTF-8 with a BOM and semicolon-separated, which is what Excel opens
    correctly on a double-click in a German locale — no add-on, no import
    wizard. Honours the month *and* the person filter, so "export what I'm
    looking at" holds for both."""
    import csv
    import io
    from flask import Response
    from application.models.timetracking import TimeTrackingProject
    from application.services.timetracking import (
        public_project_report, sheet_cell)
    from application.services.tz import user_zone

    proj = TimeTrackingProject.objects(
        share_token=token, share_enabled=True).first()
    if not proj:
        abort(404)

    raw = (request.args.get("month") or "").strip()
    month = raw if len(raw) == 7 else (None if raw == "all" else
                                       datetime.now(user_zone(None)).strftime("%Y-%m"))
    who = (request.args.get("who") or "").strip()
    data = public_project_report(proj, month, who=who)
    cols = data["sheet_columns"]

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    # The same columns the page shows, in the same order the project configured
    # — and, as everywhere on this link, hours only. There is no money column to
    # leave out, which is the point (see services.timetracking.SHEET_BASE_COLUMNS).
    w.writerow([c["csv"] for c in cols])
    for r in data["rows"]:
        w.writerow([sheet_cell(r, c["key"], for_csv=True) for c in cols])
    w.writerow([])
    total = []
    for i, c in enumerate(cols):
        if c["key"] == "hours":
            total.append(f"{data['total_minutes'] / 60:.2f}".replace(".", ","))
        elif i == 0:
            total.append("Summe")
        else:
            total.append("")
    w.writerow(total)

    stamp = month or "gesamt"
    person = f"-{_slug(who)}" if who else ""
    return Response(
        "\ufeff" + buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename=zeiten-{_slug(proj.name)}{person}-{stamp}.csv"},
    )


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", (name or "projekt").lower()).strip("-") or "projekt"


@public_bp.route("/<token>/t/<task_id>")
def task(token, task_id):
    proj = _project_or_404(token)
    task = Item.objects(pk=task_id).first()
    if not task or task.is_internal:
        abort(404)
    if not task.project or task.project.pk not in _allowed_project_ids(proj):
        abort(404)
    public_comments = [c for c in (task.comments or []) if c.is_public]
    return render_template(
        "public/task.html",
        project=proj, task=task,
        comments=public_comments, token=token,
        status_label=PUBLIC_STATUS_LABEL.get(task.status, task.status),
    )
