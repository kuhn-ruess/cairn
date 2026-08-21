"""Capture — the shared "turn this marked text into something" endpoints.

Two pages feed these: the braindump pad (the marked part of the scratch text,
or all of it) and a note's rendered body (what the reader marked there). The
conversion itself lives in ``application/services/capture.py``; this module is
only the permission check around it.

POST /capture/tasks  — {text, project_id} → tasks, in that project (blank =
                       Inbox). Answers with the counts and where they landed.
POST /capture/note   — {text, title, folder_id} → one Markdown note.
"""
from flask import Blueprint, jsonify, request, url_for
from flask_login import current_user, login_required

from application.models.item import Item
from application.services import capture

capture_bp = Blueprint("capture", __name__, url_prefix="/capture")


def _text_or_none(data) -> str:
    return (data.get("text") or "").strip()


@capture_bp.route("/tasks", methods=["POST"])
@login_required
def to_tasks():
    user = current_user._get_current_object()
    data = request.get_json(silent=True) or request.form
    text = _text_or_none(data)
    if not text:
        return jsonify({"error": "Nothing to convert."}), 400

    raw_project = (data.get("project_id") or "").strip()
    project = None
    if raw_project:
        project = Item.objects(pk=raw_project, kind__in=["project", "section"]).first()
        if not project or not project.can_be_edited_by(user):
            return jsonify({"error": "Unknown project."}), 400

    stats = capture.create_tasks(text, user, parent=project)
    return jsonify({
        "ok": True,
        "stats": stats,
        "target": {
            "label": project.name if project else "Inbox",
            "url": (url_for("projects.detail", project_id=str(project.pk))
                    if project else url_for("tasks.inbox")),
        },
    })


@capture_bp.route("/note", methods=["POST"])
@login_required
def to_note():
    user = current_user._get_current_object()
    data = request.get_json(silent=True) or request.form
    text = _text_or_none(data)
    if not text:
        return jsonify({"error": "Nothing to convert."}), 400

    raw_folder = (data.get("folder_id") or "").strip()
    folder = None
    if raw_folder:
        folder = Item.objects(
            pk=raw_folder, kind="folder", folder_domain="notes",
        ).first()
        if not folder or not folder.can_be_edited_by(user):
            return jsonify({"error": "Unknown folder."}), 400

    note = capture.create_note(
        text, user, title=(data.get("title") or "").strip(), folder=folder,
    )
    return jsonify({
        "ok": True,
        "id": str(note.pk),
        "title": note.title,
        "url": url_for("notes.detail", note_id=str(note.pk)),
    })
