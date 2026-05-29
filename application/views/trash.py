"""Trash view — restore or permanently delete soft-deleted items."""
from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from application.models.item import Item
from application.services import trash as trash_svc

trash_bp = Blueprint("trash", __name__, url_prefix="/trash")


@trash_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()
    roots = trash_svc.trashed_roots(user)
    return render_template(
        "trash/index.html",
        items=roots,
        retention_days=int(getattr(user, "trash_retention_days", 30) or 0),
    )


def _owned_trashed(user, item_id):
    item = Item.all_objects(pk=item_id).first()
    if not item or item.owner != user or item.trashed_at is None:
        abort(404)
    return item


@trash_bp.route("/<item_id>/restore", methods=["POST"])
@login_required
def restore(item_id):
    user = current_user._get_current_object()
    item = _owned_trashed(user, item_id)
    n = trash_svc.restore_item(item)
    flash(f"Restored — {n} item(s).", "success")
    return redirect(url_for("trash.index"))


@trash_bp.route("/<item_id>/delete", methods=["POST"])
@login_required
def delete_permanent(item_id):
    user = current_user._get_current_object()
    item = _owned_trashed(user, item_id)
    n = trash_svc.purge_item(item)
    flash(f"Permanently deleted — {n} item(s).", "info")
    return redirect(url_for("trash.index"))


@trash_bp.route("/empty", methods=["POST"])
@login_required
def empty():
    user = current_user._get_current_object()
    n = trash_svc.empty_trash(user)
    flash(f"Trash emptied — {n} item(s) permanently deleted.", "info")
    return redirect(url_for("trash.index"))
