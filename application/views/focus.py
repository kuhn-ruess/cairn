"""Set / clear the OmniFocus-style "Focus".

Thin endpoints over ``User.focused_item_pk`` — the actual filtering lives in
``application/services/focus.py`` and the ``_scope`` helpers. Focus changes the
global chrome (sidebar tree, badge counts, navbar chip), all outside ``<main>``,
so the front-end does a full reload rather than the usual partial main-swap.

Both routes answer JSON for the ``fetch`` caller and fall back to a redirect for
a plain form submit.
"""
from flask import Blueprint, jsonify, redirect, request, url_for
from flask_login import current_user, login_required

from application.models.item import Item
from application.services import focus as focus_service

focus_bp = Blueprint("focus", __name__, url_prefix="/focus")

_FOCUSABLE = ("folder", "project", "section")


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _back():
    return redirect(request.referrer or url_for("dashboard.home"))


@focus_bp.route("/set/<item_id>", methods=["POST"])
@login_required
def set_focus(item_id):
    user = current_user._get_current_object()
    item = Item.objects(pk=item_id).first()
    if (item is None
            or item.kind not in _FOCUSABLE
            or not item.can_be_viewed_by(user)):
        if _wants_json():
            return jsonify({"ok": False, "error": "not_focusable"}), 400
        return _back()
    user.focused_item_pk = str(item.pk)
    user.save()
    if _wants_json():
        return jsonify({"ok": True, "title": item.title})
    return _back()


@focus_bp.route("/clear", methods=["POST"])
@login_required
def clear_focus():
    user = current_user._get_current_object()
    user.focused_item_pk = ""
    user.save()
    if _wants_json():
        return jsonify({"ok": True})
    return _back()
