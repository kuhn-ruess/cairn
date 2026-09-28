"""Navbar-bell notification routes.

The bell itself renders server-side from ``current_user.notifications`` in the
base layout (the user doc is already loaded, so it's free). This blueprint
handles marking them read, and serves the bell again for its poll.
"""
from flask import Blueprint, jsonify, make_response, render_template, request
from flask_login import current_user, login_required

notifications_bp = Blueprint("notifications", __name__, url_prefix="/notifications")


@notifications_bp.route("/read", methods=["POST"])
@login_required
def mark_read():
    """Mark notifications read. With no ``ids`` form field, marks all read
    ("Mark all read"); the bell otherwise sends the single id of the row that
    was opened (plus the older entries that row hides — see
    ``User.notification_feed``). Returns the new unread count in rows, the way
    the badge counts."""
    user = current_user._get_current_object()
    ids = request.form.getlist("ids") or None
    if user.mark_notifications_read(ids):
        user.save()
    return jsonify({"ok": True, "unread": user.unread_notification_row_count})


@notifications_bp.route("/bell")
@login_required
def bell():
    """The bell again, for the page's poll (``static/js/notifications.js``).

    The page sends back the fingerprint it was rendered with
    (``If-None-Match``); nothing new is a bodiless 304, so a tab left open
    costs one user load every half minute and renders nothing. Otherwise the
    badge count and the dropdown body, rendered from the same partial as the
    page. The per-entry REST list (``GET /api/v1/notifications``) is separate
    and unchanged.
    """
    user = current_user._get_current_object()
    etag = user.notification_etag
    if etag in request.if_none_match:
        resp = make_response("", 304)
    else:
        feed = user.notification_feed
        resp = jsonify({
            "etag": etag,
            "unread": sum(1 for r in feed if r.unread),
            "html": render_template("layout/_notif_menu.html", _feed=feed),
        })
    resp.set_etag(etag)
    resp.headers["Cache-Control"] = "no-cache, private"
    return resp

