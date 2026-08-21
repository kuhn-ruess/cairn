"""Braindump — a Markdown scratch pad you empty by converting it.

GET  /braindump       — the editor page
POST /braindump/save  — persist the scratch text (autosave + Clear)

Converting is not here: marked text becomes tasks or a note through the shared
``/capture/*`` endpoints (application/views/capture.py), which the notes page
uses too.
"""
from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

braindump_bp = Blueprint("braindump", __name__, url_prefix="/braindump")


@braindump_bp.route("/")
@login_required
def index():
    from application.services.containers import container_choices
    from application.views.notes import _folder_choices

    user = current_user._get_current_object()
    return render_template(
        "braindump/index.html",
        draft=user.braindump or "",
        capture_projects=container_choices(
            user, selectable_kinds=("project", "section"),
            include_shared=True, active_projects_only=True,
        ),
        capture_folders=_folder_choices(user),
    )


@braindump_bp.route("/save", methods=["POST"])
@login_required
def save():
    """Persist the scratch-pad text (debounced autosave + the Clear button)."""
    user = current_user._get_current_object()
    data = request.get_json(silent=True) or request.form
    user.braindump = data.get("text") or ""
    user.save()
    return jsonify({"ok": True})
