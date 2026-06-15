"""Subscription / billing blueprint.

* ``/billing``                — plan overview, current status, upgrade/manage
* ``/billing/checkout/<plan>``— start a Stripe Checkout session (POST)
* ``/billing/portal``         — open the Stripe Billing Portal (POST)
* ``/billing/success``        — Checkout return landing (GET)
* ``/billing/webhook``        — Stripe webhook sink (POST, CSRF-exempt)

The webhook is the authoritative path for level changes; the GET landing only
reconciles eagerly so the user sees their new tier immediately instead of
waiting for the async event.
"""
from flask import (
    Blueprint, current_app, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from application.services import billing

billing_bp = Blueprint("billing", __name__, url_prefix="/billing")


@billing_bp.route("/")
@login_required
def index():
    from application.models.team import Team
    user = current_user._get_current_object()
    owned = Team.objects(admin=user).first()
    return render_template(
        "billing/index.html",
        user=user,
        billing_enabled=billing.billing_enabled(),
        premium_label=current_app.config.get("PRICE_PREMIUM_LABEL", ""),
        team_label=current_app.config.get("PRICE_TEAM_LABEL", ""),
        team_family_label=current_app.config.get("PRICE_TEAM_FAMILY_LABEL", ""),
        # which team variant the user currently owns (for "current plan" badges)
        current_team_family=bool(owned and owned.family),
    )


@billing_bp.route("/checkout/<plan>", methods=["POST"])
@login_required
def checkout(plan):
    user = current_user._get_current_object()
    # Avoid creating a second, parallel subscription — a user already paying
    # switches plans through the Stripe portal, not a fresh checkout.
    if user.has_active_subscription:
        flash("You already have an active plan — use Manage to switch or cancel.",
              "info")
        return redirect(url_for("billing.index"))
    try:
        url = billing.create_checkout_session(user, plan)
    except billing.BillingError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("billing.index"))
    return redirect(url, code=303)


@billing_bp.route("/portal", methods=["POST"])
@login_required
def portal():
    user = current_user._get_current_object()
    try:
        url = billing.create_portal_session(user)
    except billing.BillingError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("billing.index"))
    return redirect(url, code=303)


@billing_bp.route("/success")
@login_required
def success():
    """Checkout return URL. The subscription webhook is authoritative, but it
    may land a moment later — pull the session's subscription now so the user
    sees the upgrade immediately."""
    user = current_user._get_current_object()
    session_id = request.args.get("session_id")
    if session_id and billing.billing_enabled():
        try:
            sess = billing._stripe().checkout.Session.retrieve(session_id)
            billing._apply_checkout_completed(sess)
            user.reload()
        except Exception as exc:  # pragma: no cover - best-effort eager sync
            current_app.logger.warning("billing success sync failed: %s", exc)
    flash("Thanks — your subscription is active.", "success")
    return redirect(url_for("billing.index"))


@billing_bp.route("/webhook", methods=["POST"])
def webhook():
    """Stripe webhook sink. CSRF-exempt (registered in the app factory) and
    auth-free — authenticity is proven by the Stripe signature header."""
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = billing.verify_webhook(payload, sig)
    except billing.BillingError as exc:
        current_app.logger.warning("billing webhook rejected: %s", exc)
        return "", 400
    try:
        billing.handle_event(event)
    except Exception:  # never make Stripe retry on our bug — log + 200
        current_app.logger.exception("billing webhook handler failed")
    return "", 200
