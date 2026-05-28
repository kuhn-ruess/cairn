"""Inline image uploads used by note-field paste.

POST a single image (multipart ``file=…``), get back a JSON payload with
the public URL and a ready-made markdown snippet. Files live under
``UPLOAD_DIR`` and are served as static assets — filenames are random
so URLs aren't guessable from outside.
"""
import os
import secrets

from flask import Blueprint, current_app, jsonify, request, url_for
from flask_login import current_user, login_required

uploads_bp = Blueprint("uploads", __name__, url_prefix="/uploads")

ALLOWED_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
ALLOWED_MIME = {
    "image/png", "image/jpeg", "image/gif",
    "image/webp", "image/svg+xml",
}


def _ensure_dir(path: str) -> None:
    if not os.path.isabs(path):
        path = os.path.join(current_app.root_path, "..", path)
    path = os.path.abspath(path)
    os.makedirs(path, exist_ok=True)
    return path


@uploads_bp.route("/note-image", methods=["POST"])
@login_required
def note_image():
    fileobj = request.files.get("file")
    if not fileobj or not fileobj.filename:
        return jsonify({"error": "no file"}), 400
    if fileobj.mimetype not in ALLOWED_MIME:
        return jsonify({"error": f"unsupported type: {fileobj.mimetype}"}), 415
    ext = (fileobj.filename.rsplit(".", 1)[-1] or "").lower()
    if ext not in ALLOWED_EXTS:
        # Map mime back to a sensible extension when the upload name has
        # none / a weird one.
        ext = {
            "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
            "image/webp": "webp", "image/svg+xml": "svg",
        }[fileobj.mimetype]

    # Size guard — read once, check, then write.
    fileobj.stream.seek(0, os.SEEK_END)
    size = fileobj.stream.tell()
    fileobj.stream.seek(0)
    max_bytes = current_app.config.get("UPLOAD_MAX_BYTES", 8 * 1024 * 1024)
    if size > max_bytes:
        return jsonify({"error": f"too large (max {max_bytes // 1024 // 1024} MB)"}), 413

    user_pk = str(current_user._get_current_object().pk)
    target_root = _ensure_dir(current_app.config.get("UPLOAD_DIR") or "application/static/uploads")
    user_dir = os.path.join(target_root, user_pk)
    os.makedirs(user_dir, exist_ok=True)

    name = f"{secrets.token_urlsafe(16)}.{ext}"
    full_path = os.path.join(user_dir, name)
    fileobj.save(full_path)

    # Public URL via the static endpoint. Compute the path under
    # /static/ from UPLOAD_DIR so the response url stays valid even if
    # the deployer points UPLOAD_DIR somewhere else under static/.
    rel = os.path.relpath(full_path, os.path.join(current_app.root_path, "static"))
    public = url_for("static", filename=rel)
    return jsonify({
        "url": public,
        "markdown": f"![pasted image]({public})",
    })
