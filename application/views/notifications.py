"""Navbar-bell notification routes.

The bell itself renders server-side from ``current_user.notifications`` in the
base layout (the user doc is already loaded, so it's free). This blueprint just
handles marking them read.
"""
from flask import Blueprint, jsonify, request
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
