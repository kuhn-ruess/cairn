"""Trash view: look at, restore or permanently delete soft-deleted items.

The list shows each deletion once (``trash.trashed_roots``), with where the
item lived, when and why it went, and a short excerpt, grouped by the moment
it was trashed so a bulk delete reads as one block. Every trashed item opens
read only (``/trash/<id>``): the normal pages cannot show it (``Item.objects``
hides the trash), and nothing on that page can change it except Restore.
"""
import re
from datetime import timedelta, timezone

from flask import (
    Blueprint, Response, abort, flash, redirect, render_template, request,
    url_for,
)
from flask_login import current_user, login_required

from application.models.item import Item
from application.models.user import User
from application.services import trash as trash_svc

trash_bp = Blueprint("trash", __name__, url_prefix="/trash")

# Items trashed within this gap of each other are shown as one group: a bulk
# delete or a sync round stamps its items a few milliseconds apart.
GROUP_GAP = timedelta(seconds=60)
EXCERPT_LEN = 160

_MD_NOISE = re.compile(r"!\[[^\]]*\]\([^)]*\)|[#>*_`~\[\]]|\([^)]*://[^)]*\)|"
                       r"^\s*-\s+|^\s*\w+::.*$", re.M)


def _raw_id(doc, field):
    """A reference field's id without dereferencing it (a dereference per row
    is the query this page must not make)."""
    raw = doc._data.get(field)
    return getattr(raw, "id", None) or getattr(raw, "pk", None) or raw


def _excerpt(text: str) -> str:
    if not text:
        return ""
    flat = " ".join(_MD_NOISE.sub(" ", text).split())
    return flat if len(flat) <= EXCERPT_LEN else flat[:EXCERPT_LEN - 1] + "…"


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def reply_since_trashed(item):
    """When the newest outside reply arrived after the item went to the trash.

    A Zammad ticket's task stays in the trash when its ticket moves on (the
    sync updates it where it lies), so the row has to say that something came
    in. ``None`` when nothing did. Public replies by someone other than the
    user only, the same ones that ring the bell."""
    trashed = _aware(item.trashed_at)
    if trashed is None:
        return None
    newest = None
    for c in item.comments or []:
        at = _aware(c.created_at)
        if (c.external_id and c.is_public and c.author is None
                and at is not None and at > trashed
                and (newest is None or at > newest)):
            newest = at
    return newest


def _place_labels(items) -> dict:
    """``{item pk: "Project › Section"}`` for where each item was filed, from
    at most two batched reads (parents, then the projects above sections)."""
    pids = {_raw_id(i, "parent") for i in items} - {None}
    if not pids:
        return {}
    parents = {p.pk: p for p in Item.all_objects(pk__in=list(pids))
               .only("id", "title", "kind", "parent")}
    up_ids = {_raw_id(p, "parent") for p in parents.values()
              if p.kind == "section"} - {None}
    ups = {u.pk: u for u in Item.all_objects(pk__in=list(up_ids))
           .only("id", "title")} if up_ids else {}
    out = {}
    for i in items:
        p = parents.get(_raw_id(i, "parent"))
        if p is None:
            continue
        label = p.title or ""
        if p.kind == "section":
            u = ups.get(_raw_id(p, "parent"))
            if u is not None:
                label = f"{u.title} › {label}"
        out[i.pk] = label
    return out


def _user_names(ids) -> dict:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.pk: (u.display_name or u.username)
            for u in User.objects(pk__in=list(ids))
            .only("id", "username", "display_name")}


def _trashed_with_counts(user, roots) -> dict:
    """How many items went into the trash along with each root, counted from
    one read of the user's trashed items (ids, parents and stamps only)."""
    rows = list(Item.all_objects(owner=user, trashed_at__ne=None)
                .only("id", "parent", "trashed_at"))
    stamp = {r.pk: r.trashed_at for r in rows}
    kids = {}
    for r in rows:
        pid = _raw_id(r, "parent")
        if pid in stamp and stamp[pid] == r.trashed_at:
            kids.setdefault(pid, []).append(r.pk)
    out = {}
    for root in roots:
        n, todo = 0, list(kids.get(root.pk, []))
        while todo:
            pk = todo.pop()
            n += 1
            todo.extend(kids.get(pk, []))
        out[root.pk] = n
    return out


def _groups(rows):
    """Split rows (newest first) into runs trashed close together."""
    groups, cur = [], None
    for row in rows:
        at = row["item"].trashed_at
        if cur is None or cur["last"] - at > GROUP_GAP:
            cur = {"at": at, "last": at, "rows": []}
            groups.append(cur)
        cur["rows"].append(row)
        cur["last"] = at
    for g in groups:
        g["sources"] = sorted({r["source"] for r in g["rows"]})
    return groups


@trash_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()
    roots = trash_svc.trashed_roots(user)
    places = _place_labels(roots)
    names = _user_names(_raw_id(i, "trashed_by") for i in roots)
    along = _trashed_with_counts(user, roots)

    counts = {}
    for it in roots:
        src = it.trashed_source or "unknown"
        counts[src] = counts.get(src, 0) + 1
    source = request.args.get("source", "")
    if source not in counts:
        source = ""

    rows = []
    for it in roots:
        src = it.trashed_source or "unknown"
        if source and src != source:
            continue
        rows.append({
            "item": it,
            "source": src,
            "place": places.get(it.pk, ""),
            "excerpt": _excerpt(it.note),
            "by": names.get(_raw_id(it, "trashed_by"), ""),
            "reason": trash_svc.reason_label(it),
            "along": along.get(it.pk, 0),
            "reply_since": reply_since_trashed(it),
        })
    return render_template(
        "trash/index.html",
        groups=_groups(rows),
        total=len(roots),
        counts=counts,
        source=source,
        source_labels={**trash_svc.SOURCES, "unknown": "Unknown"},
        retention_days=int(getattr(user, "trash_retention_days", 30) or 0),
    )


def _owned_trashed(user, item_id):
    item = Item.all_objects(pk=item_id).first()
    if not item or item.owner != user or item.trashed_at is None:
        abort(404)
    return item


def _viewable_trashed(user, item_id):
    """A trashed item the user may LOOK at: owner or assignee, the same two
    the item's own pages and the API scope answer to. Anything else, and
    anything not in the trash, is a 404."""
    item = Item.all_objects(pk=item_id).first()
    if (not item or item.trashed_at is None
            or (item.owner != user and item.assigned_to != user)):
        abort(404)
    return item


_FILE_URL = re.compile(r"/(?:tasks|notes)/([0-9a-f]{24})/note-(image|file)/")


def _local_file_urls(item, text: str) -> str:
    """Point the item's own pasted images and attachments at the trash's file
    routes: the normal ones look the item up through ``Item.objects`` and
    answer 404 for anything in the trash."""
    if not text:
        return text
    pk = str(item.pk)
    return _FILE_URL.sub(
        lambda m: (f"/trash/{pk}/note-{m.group(2)}/" if m.group(1) == pk
                   else m.group(0)), text)


@trash_bp.route("/<item_id>")
@login_required
def view(item_id):
    user = current_user._get_current_object()
    item = _viewable_trashed(user, item_id)
    children = list(
        Item.all_objects(parent=item, trashed_at=item.trashed_at)
        .only("id", "title", "kind", "status").order_by("kind", "title"))
    # Links name other items: only those this user may see themselves.
    related = [r for r in (item.related or [])
               if isinstance(r, Item) and r.can_be_viewed_by(user)]
    depends = [d for d in (item.depends_on or [])
               if isinstance(d, Item) and d.can_be_viewed_by(user)]
    names = _user_names([_raw_id(item, "trashed_by")])
    # The time booked on it, read through the billing module's own reader:
    # trashing never touches booked time, and this page must not suggest it.
    from application.services.timetracking import item_entries
    return render_template(
        "trash/view.html",
        item=item,
        time_rows=item_entries(user, item),
        note_html_src=_local_file_urls(item, item.note or ""),
        place=_place_labels([item]).get(item.pk, ""),
        children=children,
        related=related,
        depends=depends,
        by=names.get(_raw_id(item, "trashed_by"), ""),
        reason=trash_svc.reason_label(item),
        reply_since=reply_since_trashed(item),
        source_label=trash_svc.SOURCES.get(item.trashed_source or "", "Unknown"),
        is_owner=item.owner == user,
    )


@trash_bp.route("/<item_id>/note-image/<token>")
@login_required
def note_image(item_id, token):
    item = _viewable_trashed(current_user._get_current_object(), item_id)
    img = next((i for i in item.note_images or [] if i.token == token), None)
    if not img:
        abort(404)
    return Response(bytes(img.data), mimetype=img.content_type or "image/webp",
                    headers={"Cache-Control": "private, max-age=3600",
                             "X-Content-Type-Options": "nosniff"})


@trash_bp.route("/<item_id>/note-file/<token>")
@login_required
def note_file(item_id, token):
    from application.views.tasks import _note_file_response
    item = _viewable_trashed(current_user._get_current_object(), item_id)
    return _note_file_response(item, token)


@trash_bp.route("/<item_id>/restore", methods=["POST"])
@login_required
def restore(item_id):
    user = current_user._get_current_object()
    item = _owned_trashed(user, item_id)
    n = trash_svc.restore_item(item)
    flash(f"Restored: {n} item(s).", "success")
    return redirect(url_for("trash.index"))


@trash_bp.route("/restore-selected", methods=["POST"])
@login_required
def restore_selected():
    user = current_user._get_current_object()
    ids = request.form.getlist("item_ids")
    items = list(Item.all_objects(pk__in=ids, owner=user, trashed_at__ne=None))
    # Parents before their children, whatever order the page or the database
    # gives: a child restored first finds its parent still in the trash and is
    # re-filed at the top level (restore_item), out of the tree it came from.
    parent_of = {i.pk: _raw_id(i, "parent") for i in items}

    def depth(pk):
        d, seen = 0, set()
        while parent_of.get(pk) in parent_of and pk not in seen:
            seen.add(pk)
            pk = parent_of[pk]
            d += 1
        return d
    items.sort(key=lambda i: depth(i.pk))
    n = 0
    for i in items:
        fresh = Item.all_objects(pk=i.pk, trashed_at__ne=None).first()
        if fresh is not None:   # already back with an ancestor restored above
            n += trash_svc.restore_item(fresh)
    flash(f"Restored: {n} item(s).", "success" if n else "warning")
    return redirect(url_for("trash.index", source=request.form.get("source") or None))


@trash_bp.route("/<item_id>/delete", methods=["POST"])
@login_required
def delete_permanent(item_id):
    user = current_user._get_current_object()
    item = _owned_trashed(user, item_id)
    n = trash_svc.purge_item(item)
    flash(f"Permanently deleted: {n} item(s).", "info")
    return redirect(url_for("trash.index"))


@trash_bp.route("/empty", methods=["POST"])
@login_required
def empty():
    user = current_user._get_current_object()
    n = trash_svc.empty_trash(user)
    flash(f"Trash emptied: {n} item(s) permanently deleted.", "info")
    return redirect(url_for("trash.index"))
