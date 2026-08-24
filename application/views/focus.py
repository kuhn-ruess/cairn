"""Set / clear the OmniFocus-style "Focus".

Thin endpoints over ``User.focused_item_pk`` — the actual filtering lives in
``application/services/focus.py`` and the ``_scope`` helpers. Focus changes the
global chrome (sidebar tree, badge counts, navbar chip), all outside ``<main>``,
so the front-end does a full reload rather than the usual partial main-swap.

Both routes answer JSON for the ``fetch`` caller and fall back to a redirect for
a plain form submit.
"""
from mongoengine import Q

from flask import (
    Blueprint, jsonify, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from application.models.item import Item
from application.services import focus as focus_service

focus_bp = Blueprint("focus", __name__, url_prefix="/focus")

_FOCUSABLE = ("folder", "project", "section")


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _back():
    # Honour a local ?next= (used by the post-login folder picker so the
    # choice lands on the dashboard, not back on the picker). Only a
    # site-relative single-slash path — reject "//host" / "/\host" which the
    # browser treats as protocol-relative (open-redirect primitive).
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith(("//", "/\\")):
        return redirect(nxt)
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


@focus_bp.route("/choose")
@login_required
def choose():
    """Post-login folder picker (see ``User.focus_on_login``).

    Lists the user's folders so they can set the session focus before seeing
    anything else. Picking a folder POSTs to ``set_focus`` with a ``next`` of
    the dashboard; "show everything" clears focus the same way.
    """
    user = current_user._get_current_object()
    from application.services.permissions import container_visibility_q
    folders = list(
        Item.objects(container_visibility_q(user), kind="folder",
                     folder_domain__ne="notes")
        .only("pk", "title", "parent")
        .order_by("+title")
    )
    return render_template("focus/choose.html", folders=folders)
