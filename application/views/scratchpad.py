"""Scratchpad — a scratch pad you empty by converting it.

GET  /scratchpad       — the pad
GET  /scratchpad/frame — the same pad inside the floating window (an iframe)
POST /scratchpad/save  — persist it (autosave + Clear)

The pad is an OUTLINE, the same one a note is: every line a block, Tab and
Shift-Tab moving it in and out, the text itself still plain Markdown in
``User.scratchpad`` (services/outline). That is not decoration — the whole point
of the pad is that what you jot down becomes tasks, and a task tree IS an
outline. Indentation used to be four spaces you typed yourself; now it is
structure you can move.

Converting is not here: marked blocks become tasks or a note through the shared
``/capture/*`` endpoints (application/views/capture.py), which the notes page
uses too.
"""
import re

from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

scratchpad_bp = Blueprint("scratchpad", __name__, url_prefix="/scratchpad")


@scratchpad_bp.route("/")
@login_required
def index():
    return render_template("scratchpad/index.html",
                           **_pad_context(current_user._get_current_object()))


@scratchpad_bp.route("/frame")
@login_required
def frame():
    """The pad as its own little document, for the floating window's iframe."""
    return render_template("scratchpad/frame.html", compact=True,
                           **_pad_context(current_user._get_current_object()))


def _pad_context(user) -> dict:
    """Everything scratchpad/_pad.html needs — the page and the window alike."""
    from application.services import outline
    from application.services.containers import container_choices
    from application.views.notes import _blocks_json, _folder_choices, _wiki_index
    from application.views.notes import notes_tree_for_sidebar

    # parse_lines, not parse: in the pad a LINE was always an item, and the
    # note parser would glue a page of them into one paragraph.
    blocks = outline.parse_lines(user.scratchpad or "")
    tree = notes_tree_for_sidebar(user)
    return dict(
        blocks=blocks,
        blocks_json=_blocks_json(blocks),
        # "[[" still links a note from the pad; a block reference does not
        # belong here, because the pad is not an Item and nothing can point
        # back at a line that is about to be converted away.
        wiki_index=_wiki_index(tree),
        block_index={},
        embed_of=lambda _content: "",
        capture_projects=container_choices(
            user, selectable_kinds=("project", "section"),
            include_shared=True, active_projects_only=True,
        ),
        capture_folders=_folder_choices(user),
    )


@scratchpad_bp.route("/save", methods=["POST"])
@login_required
def save():
    """Persist the pad (debounced autosave, Clear, and every block edit).

    Speaks the same little protocol as a note's edit route, because the very
    same editor posts here: the whole body in ``note``, optionally a block
    position in ``render`` to have that one block rendered back.
    """
    from application.services import outline

    user = current_user._get_current_object()
    data = request.get_json(silent=True) or request.form
    # ``note`` is what the outliner posts; ``text`` is what anything older did.
    text = data.get("note")
    if text is None:
        text = data.get("text") or ""
    # multipart/form-data normalises every newline to CRLF, and an outline is
    # read line by line — a stray "\r" would ride along inside every block.
    user.scratchpad = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    user.save()
    return jsonify({
        "ok": True,
        "note": user.scratchpad,
        "stamp": "",              # nothing else writes the pad; no conflict to have
        "words": len(re.findall(r"[\w'-]+", outline.plain(user.scratchpad) if user.scratchpad else "")),
        "rendered": _render_blocks(user, data.get("render")),
    })


def _render_blocks(user, spec) -> dict:
    """``{idx: html}`` for the blocks the editor asked to have rendered back."""
    from application.services import outline
    from application.views.notes import _wiki_index, notes_tree_for_sidebar

    want = {int(p) for p in (spec or "").split(",") if p.strip().isdigit()}
    if not want:
        return {}
    blocks = outline.parse_lines(user.scratchpad or "")
    # The note lookup is a query, so only pay for it when a link is in play.
    index = _wiki_index(notes_tree_for_sidebar(user)) if "[[" in user.scratchpad else {}
    render = current_app.jinja_env.filters["note_md"]
    return {str(b.idx): str(render(b.body, index))
            for b in outline.walk(blocks) if b.idx in want}
