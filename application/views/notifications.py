"""Navbar-bell notification routes.

The bell itself renders server-side from ``current_user.notifications`` in the
base layout (the user doc is already loaded, so it's free). This blueprint
handles marking them read, opening one, and serves the bell again for its poll.
"""
from flask import (Blueprint, jsonify, make_response, redirect, render_template,
                   request, url_for)
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


@notifications_bp.route("/<ref>/open")
@login_required
def open_notification(ref):
    """Open a bell row: mark it read, then go where it points.

    The row's link is this address, not the task's, so the read happens on
    the way in, however the row was opened: a click, a middle click, Ctrl or
    Cmd click, "Open in new tab" from the context menu. A click handler on the
    bell only ever saw the first of those. The row stands for every entry
    about its task (``User.notification_feed``), so they all go read with it.
    The target is the stored link, never a parameter: nothing here redirects
    off site. An entry already pruned lands on the dashboard.
    """
    user = current_user._get_current_object()
    entry = next((n for n in user.notifications or []
                  if str(n.pk_ref) == ref), None)
    if entry is None:
        return redirect(url_for("dashboard.home"))
    if entry.item_id:
        ids = [str(n.pk_ref) for n in user.notifications
               if n.item_id == entry.item_id]
    else:
        ids = [ref]
    if user.mark_notifications_read(ids):
        user.save()
    link = entry.link or ""
    if not link.startswith("/") or link.startswith("//") or "\\" in link:
        link = url_for("dashboard.home")
    resp = redirect(link)
    resp.headers["Cache-Control"] = "no-store"
    return resp


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

