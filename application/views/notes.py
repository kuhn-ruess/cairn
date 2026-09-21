"""Markdown notes — standalone ``kind="note"`` items organised in their own
folder tree (``folder_domain="notes"``), shared with the family like projects,
and cross-linked to tasks (via ``related``) and calendar events (via
``linked_events``).

The model work all lives on the unified ``Item`` (see models/item.py), so notes
inherit hierarchy (``parent``), sharing (``members`` + services/permissions),
soft-delete (``trashed_at`` + services/trash) and inline image paste
(``note_images``) for free — this module is just the views.
"""
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint, Response, abort, current_app, flash, jsonify, redirect,
    render_template, request, url_for,
)
from bson import ObjectId
from flask_login import current_user, login_required
from mongoengine import Q

from application.models.item import Item, LinkedEvent, NoteImage, NoteFile
from application.models.user import User

notes_bp = Blueprint("notes", __name__, url_prefix="/notes")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------- tree ----------

def notes_tree_for_sidebar(user):
    """Notes + notes-folders the user owns or is a member of, as a depth-first
    ``list[(depth, item)]`` for the notes navigator. Mirrors
    ``project_tree_for_sidebar`` but for the ``notes`` folder domain."""
    from application.services.permissions import container_visibility_q
    items = list(
        Item.objects(
            container_visibility_q(user)
            & (Q(kind="note") | Q(kind="folder", folder_domain="notes"))
        ).only("pk", "title", "kind", "parent", "color", "members", "owner",
              "shared_teams", "tags")
    )
    by_id = {str(i.pk): i for i in items}
    children: dict = {}
    for i in items:
        par = i.parent
        key = str(par.pk) if par is not None and str(par.pk) in by_id else None
        children.setdefault(key, []).append(i)

    ordered: list = []

    def walk(parent_key, depth):
        # Folders first, then notes; each group alphabetical — matches how the
        # project navigator reads.
        rows = sorted(
            children.get(parent_key, []),
            key=lambda x: (x.kind != "folder", (x.name or "").lower()),
        )
        for i in rows:
            ordered.append((depth, i))
            if i.kind == "folder":
                walk(str(i.pk), depth + 1)

    walk(None, 0)
    return ordered


# ---------- lookups ----------

@notes_bp.url_value_preprocessor
def _reject_ids_that_are_not_ids(_endpoint, values):
    """``/notes/<anything-that-is-not-an-id>`` is a 404, not a 500.

    mongoengine raises ValidationError when a pk is not a 24-character hex
    string, and Flask turns that into a server error — so a mistyped URL, an
    old bookmark, or a route that used to exist and does not any more came back
    as if the server were broken. Guarding here covers every route in the
    blueprint at once, rather than every lookup in it one at a time.
    """
    for key in ("note_id", "item_id"):
        if values and values.get(key) and not ObjectId.is_valid(values[key]):
            abort(404)


def _note_or_404(note_id, *, kinds=("note", "folder")):
    """A note/folder the current user may view. 404 if missing, 403 (friendly
    no-access page) if it exists but isn't shared with them."""
    user = current_user._get_current_object()
    item = Item.objects(pk=note_id, kind__in=list(kinds)).first()
    if not item:
        abort(404)
    if not item.can_be_viewed_by(user):
        abort(403)
    return item


def _editable_note_or_404(note_id, *, kinds=("note", "folder")):
    user = current_user._get_current_object()
    item = Item.objects(pk=note_id, kind__in=list(kinds)).first()
    if not item or not item.can_be_edited_by(user):
        abort(404)
    return item


def _filing_folder(user, raw_id):
    """The notes folder ``raw_id`` if the user may file into it, else ``None``.

    "May file into it" is "may edit it" — the same rule the rest of this module
    writes by — NOT "owns it". A notebook shared with someone is a notebook
    they work in: scoping this to the owner is what made dropping a note into
    a shared notebook come back as if the folder did not exist.
    """
    if not raw_id or not ObjectId.is_valid(raw_id):
        return None
    folder = Item.objects(pk=raw_id, kind="folder",
                          folder_domain="notes").first()
    if folder is None or not folder.can_be_edited_by(user):
        return None
    return folder


def _folder_choices(user, *, exclude_pk=None):
    """``[(pk, "Folder / Sub")]`` of the notes folders the user may file into —
    their own and the ones shared with them, the same set the navigator draws.
    Plain breadcrumb labels; cheap in-memory path walk."""
    from application.services.permissions import container_visibility_q
    folders = list(
        Item.objects(container_visibility_q(user)
                     & Q(kind="folder", folder_domain="notes"))
        .only("pk", "title", "parent")
    )
    by_id = {str(f.pk): f for f in folders}

    def label(f):
        parts, cur, seen = [], f, set()
        while cur is not None and str(cur.pk) not in seen:
            seen.add(str(cur.pk))
            parts.append(cur.name)
            par = cur.parent
            cur = by_id.get(str(par.pk)) if par else None
        return " / ".join(reversed(parts))

    out = [
        (str(f.pk), label(f))
        for f in folders if exclude_pk is None or f.pk != exclude_pk
    ]
    out.sort(key=lambda x: x[1].lower())
    return out


# ---------- note list (the middle column) ----------

# What a list row needs. Projected explicitly so the query never drags the
# inline image/file BLOBS (megabytes per note) into a 200-row list — only the
# image token, which is enough to build a lazy <img> URL.
_LIST_FIELDS = (
    "pk", "title", "note", "updated_at", "created_at", "tags", "parent",
    "owner", "members", "shared_teams", "note_images__token",
    "note_files__filename",
)

NOTE_SORTS = {
    "updated": "-updated_at",
    "created": "-created_at",
    "title": None,  # sorted in Python — Mongo's sort is case-sensitive
}


def _raw_parent_id(item) -> str:
    """The parent's id WITHOUT dereferencing it — accessing ``item.parent`` in
    a list loop fires one query per row (see CLAUDE.md: minimise DB load)."""
    ref = item._data.get("parent")
    if ref is None:
        return ""
    return str(getattr(ref, "id", getattr(ref, "pk", "")) or "")


def _folder_scope(tree, folder_id: str):
    """``[pk, …]`` for a folder plus every folder nested under it, read out of
    the already-built sidebar tree (depth-first, so a folder's descendants are
    exactly the following entries with a greater depth). ``None`` = no folder
    filter."""
    if not folder_id:
        return None
    out, collecting, base_depth = [], False, 0
    for depth, it in tree:
        if collecting and depth <= base_depth:
            break
        if collecting:
            if it.kind == "folder":
                out.append(it.pk)
            continue
        if str(it.pk) == folder_id and it.kind == "folder":
            collecting, base_depth = True, depth
            out.append(it.pk)
    return out or None


def _note_rows(user, tree, *, folder="", tag="", q="", sort="updated", limit=200):
    """The middle column: every note in scope, as plain dicts.

    One query, everything projected up front. Folder names and tag colours come
    from data already in memory (the sidebar tree / ``user.tags``), so
    rendering the list fires no further queries no matter how long it is.
    """
    from application.services.permissions import container_visibility_q

    qs = Item.objects(container_visibility_q(user) & Q(kind="note"))
    if folder == "unfiled":
        qs = qs.filter(parent=None)
    else:
        scope = _folder_scope(tree, folder)
        if scope is not None:
            qs = qs.filter(parent__in=scope)
    if tag:
        qs = qs.filter(tags=tag)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(note__icontains=q))

    order = NOTE_SORTS.get(sort, "-updated_at")
    qs = qs.only(*_LIST_FIELDS)
    notes = list(qs.order_by(order).limit(limit) if order else qs.limit(limit))
    if order is None:
        notes.sort(key=lambda n: (n.title or "").lower())

    folder_names = {str(it.pk): it.name for _d, it in tree if it.kind == "folder"}
    rows = []
    for n in notes:
        imgs = n.note_images or []
        rows.append({
            "id": str(n.pk),
            "title": n.title or "Untitled note",
            "snippet": (_search_snippet(n.note, q) if q
                        else _note_preview(n.note, 160)),
            "updated_at": n.updated_at,
            "created_at": n.created_at,
            "tags": n.tags or [],
            "folder": folder_names.get(_raw_parent_id(n), ""),
            "thumb": imgs[0].token if imgs else "",
            "files": len(n.note_files or []),
            "shared": bool(n._data.get("members") or n._data.get("shared_teams")),
        })
    return rows


def _list_context(user, tree, args):
    """The rows for the "All notes" page, and the filter that selected them.

    There used to be a permanent column rendering this beside every note. The
    tree is the navigation now, so this is one page: everything, or the hits
    for a search or a tag."""
    folder = (args.get("folder") or "").strip()
    tag = (args.get("tag") or "").strip()
    q = (args.get("q") or "").strip()
    sort = (args.get("sort") or "updated").strip()
    if sort not in NOTE_SORTS:
        sort = "updated"
    return {
        "rows": _note_rows(user, tree, folder=folder, tag=tag, q=q, sort=sort),
        "list_folder": folder,
        "list_tag": tag,
        "list_q": q,
        "list_sort": sort,
        "folder_name": next(
            (it.name for _d, it in tree if str(it.pk) == folder), ""),
    }


def _wiki_index(tree):
    """``[[Title]]`` → note lookup built from the tree that's already loaded,
    so wiki links cost no extra query (see services/note_links)."""
    from application.services import note_links
    return note_links.title_index(
        None, notes=[it for _d, it in tree if it.kind == "note"])


# ---------- views ----------

@notes_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()
    tree = notes_tree_for_sidebar(user)
    return render_template(
        "notes/list.html", tree=tree, note=None,
        tag_filter_choices=_tag_filter_choices(user, tree),
        **_list_context(user, tree, request.args),
    )


def _tag_filter_choices(user, tree):
    """Tags actually used by the user's notes, for the list filter. Counted
    from the tags already loaded on the tree rows — no extra query."""
    seen = {}
    for _d, it in tree:
        for name in (it.tags or []):
            seen[name] = seen.get(name, 0) + 1
    return sorted(seen.items(), key=lambda kv: kv[0].lower())


@notes_bp.route("/<note_id>")
@login_required
def detail(note_id):
    user = current_user._get_current_object()
    item = _note_or_404(note_id)
    tree = notes_tree_for_sidebar(user)
    # A folder has no body of its own — show an overview of its contents and
    # let the user manage sharing/rights for the whole subtree.
    if item.kind == "folder":
        children = list(Item.objects(
            parent=item, kind__in=["note", "folder"],
        ))
        children = [c for c in children if c.can_be_viewed_by(user)]
        children.sort(key=lambda x: (x.kind != "folder", (x.title or "").lower()))
        for c in children:
            if c.kind == "note":
                c._preview = _note_preview(c.note)
        # Reverse of Item.notes_folder — the project(s)/folder(s) that jump
        # here, so the link is walkable in both directions (one indexed query).
        linked_from = list(Item.objects(notes_folder=item)
                           .only("pk", "title", "kind", "parent", "owner",
                                 "shared_teams"))
        # …and the containers this folder could be attached to, so the link can
        # be made from here too. It reads as a property of the notes folder
        # ("these projects use me"), and looking for it only on the project side
        # is exactly how it gets missed.
        from application.services.containers import (
            container_choices, describe_containers,
        )
        linkable = container_choices(user, selectable_kinds=("project", "folder"))
        linked_paths = describe_containers(linked_from)
        return render_template(
            "notes/folder.html",
            note=item,
            folder=item,
            children=children,
            linked_from=linked_from,
            linked_paths=linked_paths,
            linkable=linkable,
            tree=tree,
            tag_filter_choices=_tag_filter_choices(user, tree),
            can_edit=item.can_be_edited_by(user),
            is_owner=(item.owner == user),
            can_delete=item.can_be_deleted_by(user),
            member_choices=_member_choices(user),
            selected_members=[str(m.pk) for m in (item.members or [])],
            team_choices=_team_choices(user),
            selected_teams=[str(t.pk) for t in (item.shared_teams or [])],
            access=item.effective_viewers(),
        )
    related = item.related_items()
    linked_tasks = [r for r in related if r.kind == "task"]
    linked_projects = [r for r in related if r.kind == "project"]
    from application.services import note_links
    from application.services.containers import container_choices
    # Reuse the task tag picker's tree builder (imported lazily — tasks.py
    # pulls in a lot and importing it at module load would risk a cycle).
    from application.views.tasks import _tag_choices_tree
    return render_template(
        "notes/detail.html",
        note=item,
        tree=tree,
        linked_tasks=linked_tasks,
        linked_projects=linked_projects,
        wiki_index=_wiki_index(tree),
        **_outline_context(item, user),
        tag_choices=_tag_choices_tree(user.tags or []),
        # Targets for "mark text in the note → tasks" (layout/_capture_modals).
        capture_projects=container_choices(
            user, selectable_kinds=("project", "section", "folder"),
            include_shared=True, active_projects_only=True,
        ),
        tag_filter_choices=_tag_filter_choices(user, tree),
        can_edit=item.can_be_edited_by(user),
        is_owner=(item.owner == user),
        can_delete=item.can_be_deleted_by(user),
        folder_choices=_folder_choices(user, exclude_pk=item.pk),
        member_choices=_member_choices(user),
        selected_members=[str(m.pk) for m in (item.members or [])],
        team_choices=_team_choices(user),
        selected_teams=[str(t.pk) for t in (item.shared_teams or [])],
        access=item.effective_viewers(),
    )


def _member_choices(user):
    from application.services.permissions import shareable_user_ids
    ids = [pk for pk in shareable_user_ids(user) if pk != str(user.pk)]
    users = User.objects(pk__in=ids) if ids else []
    return [(str(u.pk), str(u)) for u in users]


def _team_choices(user):
    from application.services.permissions import teams_of
    return [(str(t.pk), t.name) for t in teams_of(user)]


def _blocks_data(blocks) -> list:
    """The block tree as plain data for the editor (static/js/outliner.js)."""
    def node(b):
        return {"idx": b.idx, "content": b.content, "props": b.props,
                "children": [node(c) for c in b.children]}
    return [node(b) for b in blocks]


def _blocks_json(blocks) -> str:
    """The same, as JSON for a <script> tag."""
    import json
    blob = json.dumps(_blocks_data(blocks), ensure_ascii=False)
    # This goes inside a <script type="application/json"> block, where the
    # parser looks for "</script>" and nothing else — a note containing that
    # text would otherwise end the tag and spill its body into the page as
    # markup. "<\/" is the same string to a JSON reader.
    return blob.replace("</", "<\\/")


def _outline_context(item, user) -> dict:
    """Everything the outline needs, for any item that has a body.

    Notes and notes-FOLDERS both go through this: a folder is a container for
    filing and for access, and also a page you can write on — so there is no
    place where filing something and writing about it are different acts.
    """
    from application.services import block_refs, note_links, outline

    blocks = outline.parse(item.note or "")
    # Outline or plain Markdown document — the one question the page asks
    # before it decides which editor to put on the body. Everything else here
    # (backlinks, references, word count) is the same either way.
    note_format = outline.format_of(item.note or "", item.note_format or "")
    # The blocks pointing here are rendered on this page too, so whatever THEY
    # reference has to resolve as well — collected first so it all goes into
    # the one query below rather than one per referencing block.
    references = note_links.linked_references(item, user)
    wanted = list(block_refs.refs(item.note or ""))
    for group in references:
        for hit in group["blocks"]:
            wanted += block_refs.refs(hit["block"].content)
            for kid in outline.walk(hit["block"].children):
                wanted += block_refs.refs(kid.content)
    # Zoom: the page becomes one block and everything under it, with the way
    # back spelled out above it. An id that no longer resolves falls back to
    # the whole note rather than to an empty page.
    zoom = (request.args.get("zoom") or "").strip()
    trail = outline.path_to(blocks, zoom) if zoom else []
    shown = [trail[-1]] if trail else blocks
    return dict(
        note_format=note_format,
        blocks=shown,
        # The client's model comes from the SERVER's parser, as JSON — it only
        # has to serialize back. Two parsers that could disagree about where a
        # block starts is the one thing this feature cannot afford.
        blocks_json=_blocks_json(blocks),
        # Every ((reference)) and {{embed}} on this page in ONE query.
        block_index=block_refs.resolve(user, wanted),
        references=references,
        reference_count=sum(len(g["blocks"]) or 1 for g in references),
        embed_of=block_refs.embed_target,
        zoom=zoom if trail else "",
        # Plain labels, built here rather than in the template: a crumb should
        # read "Wochenplanung", not "## Wochenplanung".
        zoom_trail=[{"uuid": b.uuid, "label": _note_preview(b.body, 48) or "Block"}
                    for b in trail[:-1]],
        word_count=_word_count(item.note),
        base_stamp=_stamp(item.updated_at),
    )


def _derived_title(item) -> str:
    """A note's name taken from its own first line.

    Writing the first block names the note — you should not have to type the
    same words twice, once into a title field and once into the note. Leading
    empty blocks are skipped, so a note that starts with a blank line still
    finds its name.
    """
    from application.services import outline
    for block in outline.walk(outline.parse(item.note or "")):
        text = _note_preview(block.body, 80)
        if text:
            return text
    return ""


def _word_count(text) -> int:
    """Words in a note — its prose, not its bullets and ``id::`` lines."""
    from application.services import outline
    return len(re.findall(r"[\w'-]+", outline.plain(text or "")))


def _plain(text) -> str:
    """A note's markdown body as flat prose: outline syntax gone, images
    dropped, links unwrapped to their text, markdown punctuation stripped,
    whitespace collapsed."""
    from application.services import block_refs, outline
    # Bullets and block properties first — a preview must never read back an
    # "id:: 0193f2a1…" line to someone scanning a list of notes.
    s = outline.plain(text or "")
    # Nor a "{{embed ((0193f2a1…))}}". A pointer at another block has no words
    # of its own, and resolving it here would cost a query per row — so it
    # simply contributes nothing, and the note's own text shows instead. That
    # also keeps it out of a derived title (see _derived_title).
    s = block_refs.EMBED_ANY_RE.sub("", s)
    s = block_refs.BLOCKREF_RE.sub("", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)        # images out
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)     # links → their text
    s = re.sub(r"\[\[([^\[\]|]*)(?:\|([^\[\]]*))?\]\]", r"\2\1", s)  # wiki links
    s = re.sub(r"\[([ xX])\]",
               lambda m: "☑" if m.group(1).lower() == "x" else "☐", s)
    s = re.sub(r"[`*_#>~]|(?<!\w)-(?!\w)", " ", s)      # md punctuation
    return re.sub(r"\s+", " ", s).strip()


def _note_preview(text, limit=200):
    """A short plain-text snippet of a note's body, for the list rows."""
    s = _plain(text)
    return (s[:limit] + "…") if len(s) > limit else s


def _search_snippet(text, q, limit=180):
    """Preview centred on the search hit, with the match marked. Returns HTML
    (escaped here — the caller prints it raw), because a plain-text snippet
    can't say WHERE it matched, which is the whole point of a search result."""
    from markupsafe import Markup, escape
    s = _plain(text)
    if not q:
        return _note_preview(text, limit)
    hit = s.lower().find(q.lower())
    if hit < 0:
        return _note_preview(text, limit)   # matched the title, not the body
    start = max(0, hit - limit // 3)
    end = min(len(s), start + limit)
    body = ("…" if start else "") + s[start:end] + ("…" if end < len(s) else "")
    pos = hit - start + (1 if start else 0)
    return Markup("{}<mark>{}</mark>{}").format(
        body[:pos], body[pos:pos + len(q)], body[pos + len(q):])


def _stamp(dt) -> str:
    """A note version marker for the autosave's conflict check. Millisecond
    precision: Mongo stores milliseconds, so comparing the microsecond-precise
    in-memory value against a re-read one would report a phantom conflict."""
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


@notes_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    """Create a notes folder (and, with kind=note, a note). The navigator's
    'New note' button POSTs straight here with kind=note and opens the editor;
    folders go through the small form."""
    user = current_user._get_current_object()
    kind = (request.values.get("kind") or "note").strip()
    if kind not in ("note", "folder"):
        kind = "note"
    raw_parent = (request.values.get("parent") or "").strip()
    parent = _filing_folder(user, raw_parent)

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        if kind == "note":
            item = Item(
                owner=user, kind="note", parent=parent,
                title=title or "Untitled note", note="",
                # No name given, so the first line becomes it.
                title_auto=not title,
            )
            item.save()
            return redirect(url_for("notes.detail", note_id=str(item.pk)))
        # folder
        if not title:
            flash("A folder needs a name.", "danger")
        else:
            item = Item(
                owner=user, kind="folder", folder_domain="notes",
                parent=parent, title=title, status="active",
            )
            item.save()
            flash("Folder created.", "success")
            return redirect(url_for("notes.index"))

    return render_template(
        "notes/form.html", kind=kind,
        folder_choices=_folder_choices(user),
        parent_id=raw_parent,
        prefill_title=(request.args.get("title") or "").strip(),
    )


@notes_bp.route("/<note_id>/edit", methods=["POST"])
@login_required
def edit(note_id):
    """Save title and/or Markdown body from the editor (the autosave posts
    here). Returns the re-rendered body for AJAX callers (X-Partial), else
    redirects.

    ``base_stamp`` is the version the editor started from. If the stored note
    moved on since (a second tab, a share partner), we refuse rather than
    overwrite and hand back the current text so the client can show it — an
    autosave that silently clobbers someone else's paragraph is the one bug
    this feature must not have.
    """
    # Folders too: one is a container for filing and for access AND a page
    # with a body, so it is written through exactly this route.
    item = _editable_note_or_404(note_id, kinds=("note", "folder"))
    user = current_user._get_current_object()
    base = (request.form.get("base_stamp") or "").strip()
    if base and base != _stamp(item.updated_at):
        return jsonify({
            "ok": False, "conflict": True,
            "title": item.title, "note": item.note,
            "stamp": _stamp(item.updated_at),
        }), 409
    if "title" in request.form:
        title = (request.form.get("title") or "").strip()
        # The editor posts the title on every save, so "did the reader type in
        # that field?" is a comparison, not a flag on the request: only a value
        # that differs from what is stored can have come from them.
        if title != (item.title or ""):
            # Named it themselves — the name is theirs now. Emptied it — it
            # goes back to following the first block.
            item.title_auto = not title
            if title:
                item.title = title
        if not item.title:
            item.title = ("Untitled folder" if item.kind == "folder"
                          else "Untitled note")
    index = {}
    if "note" in request.form:
        # The body, the block-id inventory, the link index and the derived
        # name move together — see services/notes_write, which the API writes
        # notes through as well.
        from application.services.notes_write import apply_body
        index = apply_body(item, request.form.get("note", ""), user)
    item.save()
    if request.headers.get("X-Partial") == "1":
        return jsonify({"ok": True, "title": item.title, "note": item.note,
                        "rendered": _render_blocks(
                            item, request.form.get("render"), user, index),
                        "stamp": _stamp(item.updated_at),
                        "words": _word_count(item.note),
                        "updated": item.updated_at.isoformat() if item.updated_at else ""})
    return redirect(url_for("notes.detail", note_id=str(item.pk)))


def _render_blocks(item, spec, user, wiki_index) -> dict:
    """``{idx: html}`` for the blocks the editor asked to have re-rendered.

    The editor sends the whole body but only names the block it just finished
    writing, and only that one comes back rendered. Swapping the entire outline
    on every keystroke-pause would take the caret with it."""
    if not spec:
        return {}
    from application.services import block_refs, outline
    index = block_refs.resolve(user, block_refs.refs(item.note or ""))
    render = current_app.jinja_env.filters["note_md"]
    # "doc": the Markdown editor has no blocks to name — its body is one
    # document and comes back as one piece.
    if spec == "doc":
        return {"doc": str(render(item.note or "", wiki_index, index))}
    want = {int(p) for p in spec.split(",") if p.strip().isdigit()}
    if not want:
        return {}
    blocks = outline.parse(item.note or "")
    return {str(b.idx): str(render(b.body, wiki_index, index))
            for b in outline.walk(blocks) if b.idx in want}


@notes_bp.route("/<note_id>/format", methods=["POST"])
@login_required
def set_format(note_id):
    """Cross the bridge between the outline and a plain Markdown document.

    The BODY is rewritten, not merely read differently: an outline is written
    out as prose (``outline.to_markdown``), a document is written out as
    bullets (parse + serialize). Doing it to the text is the whole point —
    the mirrored ``.md`` file has to become the thing that was asked for, and a
    note that only *displayed* as Markdown would still sync as an outline.

    The same choice can be made from the outside, by putting ``format:`` in the
    mirrored file's front matter (services/notes_sync).
    """
    from application.services import outline
    from application.services.notes_write import apply_body

    item = _editable_note_or_404(note_id, kinds=("note", "folder"))
    user = current_user._get_current_object()
    want = (request.form.get("format") or "").strip()
    if want not in outline.FORMATS:
        abort(400)
    blocks = outline.parse(item.note or "")
    apply_body(item, outline.to_markdown(blocks) if want == "markdown"
               else outline.serialize(blocks), user)
    item.note_format = want
    item.save()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "format": want})
    return redirect(url_for("notes.detail", note_id=str(item.pk)))


@notes_bp.route("/parse", methods=["POST"])
@login_required
def parse_blocks():
    """Pasted text, as blocks.

    The editor deliberately has no parser — services/outline is the only one,
    so the two can never disagree about where a block starts — which means
    that when text arrives on the clipboard it has to ask here what it is.

    ``parse_lines`` rather than ``parse``: a paste is a list of items, one per
    line, which is both what Logseq does and what you get back when you cut an
    outline and put it down somewhere else. Text we wrote ourselves takes the
    strict path either way.
    """
    from application.services import outline
    text = (request.form.get("text") or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return jsonify({"blocks": _blocks_data(outline.parse_lines(text))})


@notes_bp.route("/search")
@login_required
def search():
    """Full-text hits for the navigator's search box, with the snippet that
    says WHERE each one matched.

    The tree beside it is navigation, and a tree of names cannot answer "where
    did I write about this" — so the box does not touch the tree at all. It
    asks this, and shows what comes back underneath itself.
    """
    user = current_user._get_current_object()
    q = (request.args.get("q") or "").strip()
    # One letter matches most of a collection; that is a list, not an answer.
    if len(q) < 2:
        return jsonify({"q": q, "total": 0, "results": []})
    tree = notes_tree_for_sidebar(user)
    rows = _note_rows(user, tree, q=q, limit=50)
    return jsonify({
        "q": q,
        "total": len(rows),
        # The snippet is HTML — escaped text with the hit marked (see
        # _search_snippet), which is the whole reason to show it.
        "results": [{"id": r["id"], "title": r["title"],
                     "snippet": str(r["snippet"]), "folder": r["folder"]}
                    for r in rows[:8]],
    })


@notes_bp.route("/titles")
@login_required
def titles():
    """Note titles for the editor's ``[[`` autocomplete."""
    user = current_user._get_current_object()
    q = (request.args.get("q") or "").strip().lower()
    from application.services.permissions import container_visibility_q
    notes = Item.objects(
        container_visibility_q(user) & Q(kind="note")
    ).only("pk", "title").order_by("-updated_at").limit(300)
    out = [{"id": str(n.pk), "title": n.title or "Untitled note"} for n in notes]
    if q:
        out = [o for o in out if q in o["title"].lower()]
    return jsonify({"results": out[:12]})


# NOTE: there is no /toggle-task here any more. services/md_tasks numbers the
# checkboxes it flips by their order within one rendered body, and an outline
# renders block by block — every block would start counting at zero again. The
# outliner flips the box in its own model and saves the body like any other
# edit. md_tasks still serves the note field on tasks and projects, which are
# single rendered bodies and where that numbering holds.


@notes_bp.route("/<note_id>/move", methods=["POST"])
@login_required
def move(note_id):
    """Drag-drop reparent within the notes tree. ``parent_id`` empty = top
    level. Only notes-folders are valid parents; cycles are rejected.

    Both ends go by "may edit", not by "owns": the navigator draws shared
    notes and shared notebooks as drag sources and drop targets, so anything
    stricter here answers a drop the page just offered with "not found".
    """
    user = current_user._get_current_object()
    item = Item.objects(pk=note_id, kind__in=["note", "folder"]).first()
    if (not item or not item.can_be_edited_by(user)
            or (item.kind == "folder" and item.folder_domain != "notes")):
        return jsonify({"error": "not found"}), 404
    raw_parent = (request.form.get("parent_id") or "").strip()
    if not raw_parent:
        item.parent = None
    else:
        new_parent = _filing_folder(user, raw_parent)
        if not new_parent:
            return jsonify({"error": "invalid parent"}), 400
        # Reject cycles (a folder can't move inside its own subtree).
        cur, depth = new_parent, 0
        while cur is not None and depth < 30:
            if cur.pk == item.pk:
                return jsonify({"error": "would create a cycle"}), 400
            cur, depth = cur.parent, depth + 1
        item.parent = new_parent
    item.save()
    return jsonify({"ok": True})


@notes_bp.route("/<note_id>/delete", methods=["POST"])
@login_required
def delete(note_id):
    """Soft-delete (trash) a note or notes-folder + its subtree."""
    user = current_user._get_current_object()
    item = Item.objects(pk=note_id, kind__in=["note", "folder"]).first()
    if not item or not item.can_be_deleted_by(user):
        abort(404)
    from application.services.trash import trash_item
    trash_item(item)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    flash("Moved to trash.", "info")
    return redirect(url_for("notes.index"))


@notes_bp.route("/<note_id>/share", methods=["POST"])
@login_required
def share(note_id):
    """Set the note's family/team members. Only the owner may share; each
    member id is validated against the owner's share scope."""
    user = current_user._get_current_object()
    item = Item.objects(pk=note_id, owner=user, kind__in=["note", "folder"]).first()
    if not item:
        abort(404)
    from application.services.permissions import resolve_assignable_user, teams_of
    members = []
    for mid in request.form.getlist("members"):
        m = resolve_assignable_user(user, mid)
        if m:
            members.append(m)
    item.members = members
    # Team shares, validated against the teams the user actually participates in.
    allowed_teams = {str(t.pk): t for t in teams_of(user)}
    item.shared_teams = [
        allowed_teams[tid] for tid in request.form.getlist("shared_teams")
        if tid in allowed_teams
    ]
    item.save()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "count": len(members) + len(item.shared_teams)})
    flash("Sharing updated.", "success")
    return redirect(url_for("notes.detail", note_id=str(item.pk)))


# ---------- item links: tasks + projects (symmetric, via Item.related) ----------

@notes_bp.route("/<note_id>/link-project", methods=["POST"])
@login_required
def link_project(note_id):
    """Attach a task project/folder to this notes folder (or detach one).

    The same link as the project inspector's "Notes folder" picker, from the
    other end: everything under the chosen container gets a Notes button
    pointing here. Stored once, on the container — the notes side is a reverse
    query, so the two can never disagree.
    """
    folder = _note_or_404(note_id, kinds=("folder",))
    user = current_user._get_current_object()
    from application.services.timetracking import to_oid
    target = Item.objects(pk=to_oid(request.form.get("item_id")) or ObjectId(),
                          owner=user, kind__in=["project", "folder"],
                          folder_domain__ne="notes").first()
    if not target:
        flash("Pick a project or folder to link.", "warning")
        return redirect(url_for("notes.detail", note_id=str(folder.pk)))
    if request.form.get("action") == "unlink":
        target.notes_folder = None
        flash(f"“{target.title}” no longer opens these notes.", "info")
    else:
        target.notes_folder = folder
        flash(f"“{target.title}” now opens these notes.", "success")
    target.save()
    return redirect(url_for("notes.detail", note_id=str(folder.pk)))


@notes_bp.route("/<note_id>/link-item", methods=["POST"])
@login_required
def link_item(note_id):
    """Link a task OR project to this note (symmetric — the other side
    back-links to the note, which is how it surfaces on the project page)."""
    item = _editable_note_or_404(note_id, kinds=("note",))
    user = current_user._get_current_object()
    other_id = (request.form.get("item_id") or "").strip()
    other = (Item.objects(pk=other_id, kind__in=["task", "project"]).first()
             if other_id else None)
    if other and other.can_be_viewed_by(user):
        item.relate_to(other)
    return jsonify({"ok": True})


@notes_bp.route("/<note_id>/unlink-item/<item_id>", methods=["POST"])
@login_required
def unlink_item(note_id, item_id):
    item = _editable_note_or_404(note_id, kinds=("note",))
    other = Item.all_objects(pk=item_id).first()
    item.unrelate_from(other)
    return jsonify({"ok": True})


# ---------- tags ----------

@notes_bp.route("/<note_id>/tags", methods=["POST"])
@login_required
def set_tags(note_id):
    """Replace the note's tags from the inspector multi-select. Enforces
    exclusive-children groups (one child per exclusive parent), mirroring the
    task save path. Label→status rules don't apply to notes."""
    item = _editable_note_or_404(note_id, kinds=("note",))
    user = current_user._get_current_object()
    submitted = request.form.getlist("tags")
    by_name = {t.name: t for t in (user.tags or [])}
    final, seen_excl = [], set()
    for name in submitted:
        tag = by_name.get(name)
        if tag and tag.parent:
            parent = by_name.get(tag.parent)
            if parent and parent.exclusive_children:
                if parent.name in seen_excl:
                    continue  # already picked a child of this exclusive group
                seen_excl.add(parent.name)
        final.append(name)
    item.tags = final
    item.save()
    # Hand back the new version marker: this save moved ``updated_at``, and the
    # editor's autosave would otherwise report a conflict against itself.
    return jsonify({"ok": True, "tags": final, "stamp": _stamp(item.updated_at)})


# ---------- calendar event links (snapshotted on the note) ----------

@notes_bp.route("/events")
@login_required
def events():
    """Upcoming calendar events across the user's connected calendars, for the
    event-link picker. Snapshots uid + calendar pk so the link can re-resolve."""
    user = current_user._get_current_object()
    from application.services.calendar_sync import fetch_events
    days = max(1, min(int(request.args.get("days", 30) or 30), 120))
    since = _utcnow() - timedelta(hours=6)
    until = since + timedelta(days=days)
    out = []
    for cal in (user.calendars or []):
        # Honour the same per-calendar visibility toggle as the Today/Plan
        # view — a calendar the user switched off shouldn't surface here.
        if not cal.show_in_today:
            continue
        try:
            evs = fetch_events(cal, since, until, user=user)
        except Exception:  # noqa: BLE001 — a flaky calendar shouldn't 500 the picker
            continue
        for e in evs:
            out.append({
                "uid": e.uid,
                "calendar_pk": str(getattr(cal, "pk_ref", "") or ""),
                "calendar_name": e.calendar_name or cal.name,
                "title": e.title,
                "start": e.start.isoformat() if e.start else "",
                "all_day": e.all_day,
                "url": e.url or "",
            })
    out.sort(key=lambda x: x["start"])
    return jsonify({"events": out[:200]})


@notes_bp.route("/<note_id>/link-event", methods=["POST"])
@login_required
def link_event(note_id):
    """Attach a calendar event (posted as a snapshot) to the note. De-dupes
    on uid so re-linking the same event is a no-op."""
    item = _editable_note_or_404(note_id, kinds=("note",))
    uid = (request.form.get("uid") or "").strip()
    if not uid:
        return jsonify({"error": "missing event id"}), 400
    if any((le.uid == uid) for le in (item.linked_events or [])):
        return jsonify({"ok": True})  # already linked
    url = (request.form.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = ""  # only emit safe hrefs
    start = None
    raw_start = (request.form.get("start") or "").strip()
    if raw_start:
        try:
            start = datetime.fromisoformat(raw_start)
        except ValueError:
            start = None
    item.linked_events.append(LinkedEvent(
        uid=uid,
        calendar_pk=(request.form.get("calendar_pk") or "").strip() or None,
        title=(request.form.get("title") or "").strip(),
        start=start,
        all_day=(request.form.get("all_day") in ("1", "true", "True", "on")),
        url=url or None,
    ))
    item.save()
    return jsonify({"ok": True})


@notes_bp.route("/<note_id>/unlink-event", methods=["POST"])
@login_required
def unlink_event(note_id):
    item = _editable_note_or_404(note_id, kinds=("note",))
    uid = (request.form.get("uid") or "").strip()
    item.linked_events = [le for le in (item.linked_events or []) if le.uid != uid]
    item.save()
    return jsonify({"ok": True})


def _linked_event_from_form() -> LinkedEvent:
    """Build a LinkedEvent snapshot from the posted event fields (shared by the
    day/plan-view 'note for this event' actions)."""
    start = None
    raw_start = (request.form.get("start") or "").strip()
    if raw_start:
        try:
            start = datetime.fromisoformat(raw_start)
        except ValueError:
            start = None
    url = (request.form.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = ""  # only keep safe hrefs
    return LinkedEvent(
        uid=(request.form.get("uid") or "").strip(),
        calendar_pk=(request.form.get("calendar_pk") or "").strip() or None,
        title=(request.form.get("title") or "").strip(),
        start=start,
        all_day=(request.form.get("all_day") in ("1", "true", "True", "on")),
        url=url or None,
    )


@notes_bp.route("/for-event")
@login_required
def notes_for_event():
    """Notes the current user has linked to a calendar event (by uid). Used by
    the day/plan view to list existing notes for an event."""
    user = current_user._get_current_object()
    uid = (request.args.get("uid") or "").strip()
    notes = []
    if uid:
        for n in Item.objects(owner=user, kind="note",
                              linked_events__uid=uid).only("title"):
            notes.append({
                "id": str(n.pk),
                "title": n.title or "Untitled note",
                "url": url_for("notes.detail", note_id=str(n.pk)),
            })
    return jsonify({"notes": notes})


@notes_bp.route("/new-for-event", methods=["POST"])
@login_required
def new_for_event():
    """Create a note already linked to a calendar event, straight from the
    day/plan view. Returns the new note's URL so the client can open it."""
    user = current_user._get_current_object()
    snap = _linked_event_from_form()
    if not snap.uid:
        return jsonify({"error": "missing event id"}), 400
    # Seed the briefing skeleton: whatever the user writes under this
    # heading shows up inline on the event in the Today view (see
    # application/services/briefing.py).
    note = Item(owner=user, kind="note",
                title=snap.title or "Untitled note",
                note="## Briefing\n\n")
    note.linked_events.append(snap)
    note.save()
    return jsonify({
        "ok": True,
        "id": str(note.pk),
        "url": url_for("notes.detail", note_id=str(note.pk)),
    })


# ---------- inline pasted images (mirrors tasks.note_image*) ----------

@notes_bp.route("/<note_id>/note-image", methods=["POST"])
@login_required
def note_image_upload(note_id):
    import secrets
    from application.views.tasks import (
        NOTE_IMAGE_MAX_BYTES, ITEM_MAX_BYTES, ITEM_WARN_BYTES,
        _sniff_image_type, _item_bson_size,
    )
    item = _editable_note_or_404(note_id, kinds=("note",))
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
    item.note_images.append(NoteImage(token=token, content_type=ctype, data=data))
    size = _item_bson_size(item)
    if size > ITEM_MAX_BYTES:
        item.note_images.pop()
        return jsonify({"error": "note is full — remove some images before adding more"}), 413
    item.save()
    payload = {
        "markdown": f"![pasted image]({url_for('notes.note_image', note_id=str(item.pk), token=token)})",
        "storage": item.storage_info(),
        # The upload saved the item, so the editor's autosave needs the new
        # version marker or its next post would look like a conflict.
        "stamp": _stamp(item.updated_at),
    }
    if size > ITEM_WARN_BYTES:
        payload["warning"] = f"This note is getting large ({size / 1024 / 1024:.1f} MB of 16 MB max) — consider removing some images."
    return jsonify(payload)


@notes_bp.route("/<note_id>/note-image/<token>")
@login_required
def note_image(note_id, token):
    user = current_user._get_current_object()
    item = Item.objects(pk=note_id).first()
    if not item or not item.can_be_viewed_by(user):
        abort(404)
    img = next((i for i in item.note_images if i.token == token), None)
    if not img:
        abort(404)
    return Response(bytes(img.data), mimetype=img.content_type,
                    headers={"Cache-Control": "private, max-age=86400"})


# ---------- inline file attachments (mirrors tasks.note_file*) ----------

@notes_bp.route("/<note_id>/note-file", methods=["POST"])
@login_required
def note_file_upload(note_id):
    from application.views.tasks import _store_note_file, ITEM_WARN_BYTES
    item = _editable_note_or_404(note_id, kinds=("note",))
    result = _store_note_file(item, request.files.get("file"))
    if isinstance(result, tuple):
        return jsonify({"error": result[0]}), result[1]
    label = result["filename"].replace("[", "").replace("]", "")
    url = url_for("notes.note_file", note_id=str(item.pk), token=result["token"])
    payload = {"markdown": f"[{label}]({url})", "storage": result["storage"],
               "stamp": _stamp(item.updated_at)}
    if result["size"] > ITEM_WARN_BYTES:
        payload["warning"] = (f"This note is getting large "
                              f"({result['size'] / 1024 / 1024:.1f} MB of 16 MB max) "
                              f"— consider removing some attachments.")
    return jsonify(payload)


@notes_bp.route("/<note_id>/note-file/<token>")
@login_required
def note_file(note_id, token):
    from application.views.tasks import _note_file_response
    user = current_user._get_current_object()
    item = Item.objects(pk=note_id).first()
    if not item or not item.can_be_viewed_by(user):
        abort(404)
    return _note_file_response(item, token)


# ---------- export (Markdown / TextBundle) ----------

_EXT_BY_CTYPE = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
}


def _note_slug(title: str) -> str:
    """Filesystem-safe stem for the download filename."""
    slug = re.sub(r"[^\w.-]+", "-", (title or "").strip()).strip("-")
    return slug or "note"


def _attachment_headers(filename: str) -> dict:
    return {"Content-Disposition": f'attachment; filename="{filename}"'}


@notes_bp.route("/<note_id>/export.md")
@login_required
def export_markdown(note_id):
    """Plain Markdown download. Embedded images stay as in-app URLs (use the
    TextBundle export to carry the attachments with the text)."""
    item = _note_or_404(note_id, kinds=("note",))
    body = item.note or ""
    return Response(
        body, mimetype="text/markdown",
        headers=_attachment_headers(f"{_note_slug(item.title)}.md"),
    )


@notes_bp.route("/<note_id>/export.textpack")
@login_required
def export_textbundle(note_id):
    """TextBundle export (as a zipped ``.textpack``) — the Markdown plus every
    embedded image rewritten to ``assets/<file>`` so attachments travel with
    the note (TextBundle spec: bundle dir with text.md + info.json + assets/)."""
    item = _note_or_404(note_id, kinds=("note",))
    body = item.note or ""

    bundle = f"{_note_slug(item.title)}.textbundle"
    assets = []  # (arcname, bytes)
    for idx, img in enumerate(item.note_images or []):
        ext = _EXT_BY_CTYPE.get(img.content_type, "img")
        fname = f"{img.token}.{ext}"
        # Point every reference to this image (the upload inserts an absolute
        # /notes/<id>/note-image/<token> URL) at the bundled copy.
        src = url_for("notes.note_image", note_id=str(item.pk), token=img.token)
        body = body.replace(src, f"assets/{fname}")
        assets.append((f"{bundle}/assets/{fname}", bytes(img.data)))

    for f in item.note_files or []:
        # Bundle each file attachment under its original name (deduped by token
        # prefix) and repoint the in-app /notes/<id>/note-file/<token> link at it.
        fname = f"{f.token[:6]}-{f.filename}"
        src = url_for("notes.note_file", note_id=str(item.pk), token=f.token)
        body = body.replace(src, f"assets/{fname}")
        assets.append((f"{bundle}/assets/{fname}", bytes(f.data)))

    info = {
        "version": 2,
        "type": "net.daringfireball.markdown",
        "transient": False,
        "creatorIdentifier": "de.cairn-app",
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{bundle}/text.md", body)
        zf.writestr(f"{bundle}/info.json", json.dumps(info, indent=2))
        for arcname, data in assets:
            zf.writestr(arcname, data)
    return Response(
        buf.getvalue(), mimetype="application/zip",
        headers=_attachment_headers(f"{_note_slug(item.title)}.textpack"),
    )
