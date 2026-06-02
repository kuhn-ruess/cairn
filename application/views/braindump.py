"""Braindump — a Markdown scratch pad that converts into projects/tasks.

GET  /braindump          — the editor page
POST /braindump/convert  — parse the Markdown and create items, return stats
"""
from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from application.services.braindump import apply_braindump

braindump_bp = Blueprint("braindump", __name__, url_prefix="/braindump")


@braindump_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()
    return render_template("braindump/index.html", draft=user.braindump or "")


@braindump_bp.route("/save", methods=["POST"])
@login_required
def save():
    """Persist the scratch-pad text (debounced autosave + the Clear button)."""
    user = current_user._get_current_object()
    data = request.get_json(silent=True) or request.form
    user.braindump = data.get("text") or ""
    user.save()
    return jsonify({"ok": True})


@braindump_bp.route("/convert", methods=["POST"])
@login_required
def convert():
    user = current_user._get_current_object()
    data = request.get_json(silent=True) or request.form
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Nothing to convert."}), 400
    stats = apply_braindump(text, user)
    return jsonify({"ok": True, "stats": stats})
