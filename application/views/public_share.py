"""Read-only public project dashboard, addressed by an opaque share token.

URL: ``/p/<token>`` — no login required. Shows the project's non-internal
tasks grouped by status, plus their non-internal comments. The token is
revealed only to the project owner via the project detail page.
"""
from collections import OrderedDict

from flask import Blueprint, abort, render_template
from application.models.item import Item

public_bp = Blueprint("public", __name__, url_prefix="/p")

# Outward-facing status labels — GTD-specific terms ("inbox", "next",
# "someday") mean nothing to external viewers, so we surface neutral
# project-management words instead.
STATUS_ORDER = [
    ("next",      "In progress",  "bi-play-circle"),
    ("open",      "Open",         "bi-circle"),
    ("waiting",   "Waiting",      "bi-hourglass-split"),
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
    ids = [proj.pk]
    for child in Item.objects(parent=proj):
        ids.append(child.pk)
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
