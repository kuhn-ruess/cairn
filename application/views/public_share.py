"""Read-only public project dashboard, addressed by an opaque share token.

URL: ``/p/<token>`` — no login required. Shows the project's non-internal
tasks in one of three views (list / board / timeline), searchable and
filterable by status. The token is revealed only to the project owner via the
project detail page.

**Everything a visitor may see is derived from the one shared project.** No
identifier is ever taken from the request: the container set comes from a walk
of the shared project's own subtree, and the task query is always bounded by
that set. ``q`` and ``status`` narrow what is shown *within* those tasks and
can never widen it — that is why ``status`` is checked against the label table
and ``q`` goes through mongoengine's ``icontains`` (which escapes the value, so
a regex cannot be smuggled in). The page is anonymous and therefore trivially
hammerable, so it costs one query per tree depth plus exactly one for the
tasks, whichever view is asked for, and it carries a per-IP rate limit.
"""
from collections import OrderedDict
from datetime import datetime

from flask import Blueprint, abort, current_app, render_template, request
from mongoengine import Q

from application import limiter
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

# The three ways a visitor may look at the same task set. Anything else in
# ``?view=`` falls back to the list — an unknown value must not reach a
# template name.
PUBLIC_VIEWS = ("list", "board", "timeline")

# A search term is a substring match over title and note. Capped because this
# page is unauthenticated: a megabyte of needle would be scanned against every
# task in the project.
MAX_QUERY_LEN = 80


def _share_tree(proj):
    """The containers a visitor may see, in tree order, each stamped with its
    depth.

    Walks the shared project's subtree and stops at any container marked
    ``is_internal=True`` — the flag is enforced at *every* level, because an
    internal sub-project would otherwise still surface its name (and the
    non-internal tasks beneath it).

    One query per depth level, not per node, and each row already carries the
    name and the parent reference: the board groups by container and the
    timeline nests by it, and asking the database again per row is the N+1 an
    anonymous page can least afford. ``no_dereference`` keeps ``parent`` a
    ``DBRef`` so reading it costs nothing.
    """
    if proj.is_internal:
        return []
    root = {"pk": proj.pk, "name": proj.name, "parent": None,
            "depth": 0, "item": proj}
    nodes = [root]
    children_of = {}
    frontier = [proj.pk]
    depth = 0
    while frontier and depth < 40:
        depth += 1
        kids = list(Item.objects(parent__in=frontier,
                                 kind__in=["project", "section", "folder"],
                                 is_internal__ne=True)
                    .order_by("+sort_order", "+title")
                    .no_dereference())
        if not kids:
            break
        frontier = []
        for k in kids:
            parent_pk = k.parent.id if k.parent else None
            node = {"pk": k.pk, "name": k.title, "parent": parent_pk,
                    "depth": depth, "item": k}
            nodes.append(node)
            children_of.setdefault(parent_pk, []).append(node)
            frontier.append(k.pk)

    # Re-order breadth-first into depth-first so the timeline reads as a tree.
    ordered = []

    def _walk(node):
        ordered.append(node)
        for c in children_of.get(node["pk"], []):
            _walk(c)

    _walk(root)
    return ordered


def _allowed_project_ids(proj):
    """Just the ids of :func:`_share_tree` — the bound every task query on this
    blueprint is filtered by."""
    return [n["pk"] for n in _share_tree(proj)]


def _read_filters():
    """The two narrowing arguments, validated. Neither can widen the query:
    ``status`` must be one this page has a label for, ``q`` is a capped
    substring."""
    q = (request.args.get("q") or "").strip()[:MAX_QUERY_LEN]
    status = (request.args.get("status") or "").strip()
    if status not in PUBLIC_STATUS_LABEL:
        status = ""
    view = (request.args.get("view") or "").strip()
    if view not in PUBLIC_VIEWS:
        view = "list"
    return q, status, view


@public_bp.route("/<token>")
@limiter.limit(lambda: current_app.config["RATELIMIT_PUBLIC_SHARE"],
               methods=["GET"])
def project(token):
    proj = _project_or_404(token)
    q, status, view = _read_filters()

    containers = _share_tree(proj)
    by_pk = {n["pk"]: n for n in containers}

    # The ONE task query. ``parent__in`` is the whole visibility rule and is
    # never built from anything the request sent; ``q`` only narrows it.
    qs = Item.objects(kind="task", parent__in=list(by_pk),
                      is_internal__ne=True)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(note__icontains=q))
    tasks = list(qs.order_by("+status", "+sort_order").no_dereference())

    # Resolve each task's container from what the tree walk already fetched,
    # so no row costs a dereference.
    for t in tasks:
        node = by_pk.get(t.parent.id) if t.parent else None
        t._container = node
        t._public_status = PUBLIC_STATUS_LABEL.get(t.status, t.status)

    # Status counts are over the SEARCH result, not the filtered one — the
    # chips have to say what picking another status would give you.
    counts = {}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1

    shown = [t for t in tasks if t.status == status] if status else tasks

    status_chips = [
        {"key": key, "label": label, "icon": icon, "count": counts.get(key, 0)}
        for key, label, icon in STATUS_ORDER if counts.get(key)
    ]

    groups = OrderedDict()
    for key, label, icon in STATUS_ORDER:
        groups[key] = {"label": label, "icon": icon, "tasks": []}
    for t in shown:
        bucket = groups.get(t.status)
        if bucket is None:
            groups[t.status] = {"label": t.status, "icon": "bi-dash",
                                "tasks": []}
            bucket = groups[t.status]
        bucket["tasks"].append(t)
    groups = OrderedDict((k, v) for k, v in groups.items() if v["tasks"])

    ctx = dict(
        project=proj, token=token, view=view, q=q, status=status,
        groups=groups, status_chips=status_chips,
        total=len(tasks), shown_count=len(shown),
        filtered=bool(q or status),
        project_status_label=PROJECT_STATUS_LABEL.get(proj.status,
                                                      proj.status),
        view_links={v: _view_link(token, v, q, status) for v in PUBLIC_VIEWS},
    )
    if view == "timeline":
        ctx["gantt_rows"] = _timeline_rows(containers, shown)
        # The timeline lists containers beside tasks, so it needs both label
        # tables — and neither may show the internal GTD word.
        ctx["gantt_status_labels"] = dict(PROJECT_STATUS_LABEL,
                                          **PUBLIC_STATUS_LABEL)
    return render_template("public/project.html", **ctx)


def _view_link(token, view, q, status):
    from flask import url_for
    args = {"token": token}
    if view != "list":
        args["view"] = view
    if q:
        args["q"] = q
    if status:
        args["status"] = status
    return url_for("public.project", **args)


def _timeline_rows(containers, tasks):
    """Rows for the shared Gantt template: every container that still holds a
    matching task (plus its ancestors, so the tree stays connected), each
    followed by its tasks. Pure — it works on what the two queries above
    already returned."""
    tasks_by_container = {}
    for t in tasks:
        tasks_by_container.setdefault(
            t.parent.id if t.parent else None, []).append(t)

    keep = set()
    by_pk = {n["pk"]: n for n in containers}
    for pk in tasks_by_container:
        node = by_pk.get(pk)
        while node is not None and node["pk"] not in keep:
            keep.add(node["pk"])
            node = by_pk.get(node["parent"])

    rows = []
    for node in containers:
        if node["pk"] not in keep:
            continue
        item = node["item"]
        item._gantt_indent = node["depth"]
        rows.append(item)
        for t in tasks_by_container.get(node["pk"], []):
            t._gantt_indent = node["depth"] + 1
            rows.append(t)
    return rows


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
