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

# Only raster formats. SVG is intentionally excluded — it can carry
# inline JavaScript that runs against this origin (XSS) when fetched
# from /static/. The accepted extension is derived from magic bytes,
# not from the client-supplied filename or mimetype.
SNIFF_BYTES = 12


def _sniff_image_ext(head: bytes) -> str | None:
    """Return the canonical file extension for ``head`` if it looks like
    a supported raster image, ``None`` otherwise."""
    if len(head) >= 8 and head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(head) >= 3 and head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if len(head) >= 6 and head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


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

    # Sniff the leading bytes — the browser-supplied mimetype is
    # advisory and an attacker can claim ``image/png`` for an SVG.
    head = fileobj.stream.read(SNIFF_BYTES)
    fileobj.stream.seek(0)
    ext = _sniff_image_ext(head)
    if not ext:
        return jsonify({"error": "unsupported image format"}), 415

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
