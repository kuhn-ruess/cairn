"""Sidebar favorites — toggle endpoint."""
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

favorites_bp = Blueprint("favorites", __name__, url_prefix="/favorites")


def _valid(token: str, user) -> bool:
    """Reject obviously bogus tokens so the favorites list can't be polluted."""
    typ, _, ref = token.partition(":")
    if not ref:
        return False
    if typ == "item":
        from application.models.item import Item
        it = Item.objects(pk=ref).first()
        return bool(it and it.can_be_viewed_by(user))
    if typ == "persp":
        from application.services import perspectives as p
        return ref in p._BY_KEY
    if typ == "cp":
        return any(c.pid == ref for c in (user.custom_perspectives or []))
    if typ == "org":
        from application.services.favorites import ORGANIZE_ITEMS
        return ref in ORGANIZE_ITEMS
    return False


@favorites_bp.route("/toggle", methods=["POST"])
@login_required
def toggle():
    user = current_user._get_current_object()
    token = (request.form.get("token") or "").strip()
    if not token or not _valid(token, user):
        return jsonify({"error": "invalid favorite"}), 400
    favorited = user.toggle_favorite(token)
    user.save()
    return jsonify({"ok": True, "favorited": favorited})
