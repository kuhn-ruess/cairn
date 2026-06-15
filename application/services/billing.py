"""Stripe subscription billing — the layer that turns payment state into
``User.level``.

Design (see also the field docstrings on ``User``):

* ``User.level`` stays the single source of truth every gate in the app reads.
  Nothing here is consulted by views directly — instead webhooks and the
  nightly ``reconcile-subscriptions`` sweep call :func:`recompute_level`, which
  rewrites ``level`` from the billing/team state.
* Entitlement precedence (high → low):
    1. ``global_admin``         — operator, never billed, never downgraded
    2. active **team** plan     — ``team_admin`` (owns a Team)
    3. member of a Team         — ``premium`` (free, bundled in the admin's plan)
    4. active **premium** plan  — ``premium``
    5. active trial             — ``premium``
    6. otherwise                — ``free``  (read-only over the project limit)
* No grace period: ``past_due`` / ``canceled`` don't entitle. A team whose
  admin loses the active team plan is dissolved (:func:`dissolve_team`) — a team
  must always have a paying admin. We trigger dissolution on the terminal
  ``customer.subscription.deleted`` event (and as a backstop in the nightly
  reconcile), NOT on a transient ``past_due``, so one failed card retry doesn't
  nuke a team's shares.

Billing is OFF when ``STRIPE_SECRET_KEY`` is empty — the public helpers then
raise :class:`BillingError` and the /billing page explains it isn't configured.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import current_app, url_for

log = logging.getLogger(__name__)


class BillingError(RuntimeError):
    """Raised when a billing operation can't proceed (Stripe not configured,
    or the Stripe API rejected the call)."""


# --------------------------------------------------------------------------
# config / stripe client
# --------------------------------------------------------------------------

def _cfg(key: str, default=""):
    return current_app.config.get(key, default)


def billing_enabled() -> bool:
    return bool(_cfg("STRIPE_SECRET_KEY"))


def _stripe():
    """Return the configured stripe module, or raise if billing is off."""
    if not billing_enabled():
        raise BillingError("Billing is not configured on this deployment.")
    import stripe
    stripe.api_key = _cfg("STRIPE_SECRET_KEY")
    return stripe


def _plan_for_price(price_id: str) -> str:
    """Map a Stripe price id back to our plan key (``premium`` / ``team``)."""
    if price_id and price_id == _cfg("STRIPE_PRICE_TEAM"):
        return "team"
    if price_id and price_id == _cfg("STRIPE_PRICE_PREMIUM"):
        return "premium"
    return ""


def _price_for_plan(plan: str) -> str:
    return _cfg("STRIPE_PRICE_TEAM") if plan == "team" else _cfg("STRIPE_PRICE_PREMIUM")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_dt(epoch) -> datetime | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


# --------------------------------------------------------------------------
# level recomputation — the heart of the system
# --------------------------------------------------------------------------

def _entitlement(user) -> tuple[bool, str]:
    """What the user is entitled to right now, as ``(active, plan)``. An
    operator comp plan counts exactly like a paid subscription."""
    if user.comp_plan:
        return True, user.comp_plan
    return user.has_active_subscription, (user.subscription_plan or "")


def recompute_level(user, *, save: bool = True) -> str:
    """Rewrite ``user.level`` from billing + team state. Returns the new level.

    Idempotent and side-effect-light: it only sets ``level`` (and, when an
    active team plan needs one, creates the owned Team). It does NOT dissolve
    teams — callers that detect a lost team plan must call
    :func:`dissolve_team` explicitly.
    """
    if user.level == "global_admin":
        return user.level

    active, plan = _entitlement(user)

    if active and plan == "team":
        new = "team_admin"
    elif user.team is not None:
        new = "premium"            # team membership → premium, free
    elif active and plan == "premium":
        new = "premium"
    elif user.is_trial_active:
        new = "premium"
    else:
        new = "free"

    if new == "team_admin":
        _ensure_owned_team(user)

    if user.level != new:
        log.info("billing: %s level %s → %s", user.username, user.level, new)
        user.level = new
        if save:
            user.save()
    return new


def _ensure_owned_team(user):
    """A user on the active team plan must own exactly one Team."""
    from application.models.team import Team
    owned = Team.objects(admin=user).first()
    if owned is None:
        owned = Team(name=f"{user.display_name or user.username}'s team",
                     admin=user).save()
        log.info("billing: created team %s for %s", owned.pk, user.username)
    if user.team is None:
        user.team = owned


# --------------------------------------------------------------------------
# team dissolution
# --------------------------------------------------------------------------

def dissolve_team(team) -> int:
    """Tear a team down: un-share every project shared within it, detach all
    members (admin included), delete the Team, and recompute each member's
    level (premium-by-membership falls back to free unless they pay their own
    way). Returns the number of members affected.

    Lives here (not in a teams service) so it can share ``recompute_level``
    without an import cycle — billing is what drives team lifecycle.
    """
    from application.models.item import Item
    from application.services.permissions import team_members

    members = team_members(team)            # includes the admin
    if not members:
        team.delete()
        return 0

    # Un-share: sharing only ever happens within a team, so pulling every team
    # member out of every item's ``members`` list cleanly removes team shares
    # without touching unrelated access.
    member_set = {str(m.pk) for m in members}
    for it in Item.all_objects(members__in=list(members)):
        kept = [u for u in (it.members or []) if str(u.pk) not in member_set]
        if len(kept) != len(it.members or []):
            it.members = kept
            it.save()

    for m in members:
        m.team = None

    log.info("billing: dissolving team %s (%s members)", team.pk, len(members))
    team.delete()

    for m in members:
        # team gone → level no longer propped up by membership; recompute.
        recompute_level(m, save=False)
        m.save()
    return len(members)


def _dissolve_owned_teams_if_unpaid(user) -> None:
    """If ``user`` owns teams but no longer holds an active team plan, dissolve
    them. Called after a subscription goes terminal."""
    from application.models.team import Team
    active, plan = _entitlement(user)
    if active and plan == "team":
        return
    for team in Team.objects(admin=user):
        dissolve_team(team)


# --------------------------------------------------------------------------
# checkout / portal
# --------------------------------------------------------------------------

def _ensure_customer(user) -> str:
    stripe = _stripe()
    if user.stripe_customer_id:
        return user.stripe_customer_id
    cust = stripe.Customer.create(
        email=user.email,
        name=user.display_name or user.username,
        metadata={"user_id": str(user.pk)},
    )
    user.stripe_customer_id = cust["id"]
    user.save()
    return cust["id"]


def create_checkout_session(user, plan: str) -> str:
    """Create a Stripe Checkout session for ``plan`` and return its URL."""
    if plan not in ("premium", "team"):
        raise BillingError(f"unknown plan {plan!r}")
    stripe = _stripe()
    price = _price_for_plan(plan)
    if not price:
        raise BillingError(f"no Stripe price configured for the {plan} plan.")
    customer = _ensure_customer(user)
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer,
        client_reference_id=str(user.pk),
        line_items=[{"price": price, "quantity": 1}],
        success_url=url_for("billing.success", _external=True)
        + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=url_for("billing.index", _external=True),
        metadata={"user_id": str(user.pk), "plan": plan},
        subscription_data={"metadata": {"user_id": str(user.pk), "plan": plan}},
    )
    return session["url"]


def create_portal_session(user) -> str:
    """Create a Stripe Billing Portal session (manage / cancel) URL."""
    stripe = _stripe()
    if not user.stripe_customer_id:
        raise BillingError("No billing account yet — subscribe first.")
    session = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=url_for("billing.index", _external=True),
    )
    return session["url"]


# --------------------------------------------------------------------------
# webhook handling
# --------------------------------------------------------------------------

def verify_webhook(payload: bytes, sig_header: str):
    """Verify a Stripe webhook signature and return the parsed event."""
    stripe = _stripe()
    secret = _cfg("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise BillingError("STRIPE_WEBHOOK_SECRET not configured.")
    try:
        return stripe.Webhook.construct_event(payload, sig_header, secret)
    except Exception as exc:  # SignatureVerificationError / ValueError
        raise BillingError(f"invalid webhook: {exc}") from exc


def handle_event(event) -> None:
    """Dispatch a verified Stripe event to keep the user's billing state in
    sync. Only the subscription-lifecycle events matter to us."""
    etype = event["type"]
    obj = event["data"]["object"]

    if etype in ("customer.subscription.created",
                 "customer.subscription.updated",
                 "customer.subscription.deleted"):
        _apply_subscription(obj, deleted=etype.endswith("deleted"))
    elif etype == "checkout.session.completed":
        _apply_checkout_completed(obj)
    else:
        log.debug("billing: ignoring event %s", etype)


def _user_for_subscription(sub) -> "object | None":
    from application.models.user import User
    uid = (sub.get("metadata") or {}).get("user_id")
    user = User.objects(pk=uid).first() if uid else None
    if user is None:
        cust = sub.get("customer")
        if cust:
            user = User.objects(stripe_customer_id=cust).first()
    return user


def _apply_subscription(sub, *, deleted: bool) -> None:
    user = _user_for_subscription(sub)
    if user is None:
        log.warning("billing: subscription %s has no matching user", sub.get("id"))
        return

    items = (sub.get("items") or {}).get("data") or []
    price_id = items[0]["price"]["id"] if items else ""
    plan = _plan_for_price(price_id) or user.subscription_plan

    user.subscription_id = sub.get("id") or ""
    user.subscription_plan = plan
    user.subscription_status = "canceled" if deleted else (sub.get("status") or "")
    user.current_period_end = _to_dt(sub.get("current_period_end"))
    user.cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    user.save()

    # If a team plan just went non-active, the team can't stand without a
    # paying admin. dissolve_team() recomputes the (ex-)members' levels.
    if not (user.has_active_subscription and user.subscription_plan == "team"):
        _dissolve_owned_teams_if_unpaid(user)

    recompute_level(user)


def _apply_checkout_completed(session) -> None:
    """Backstop for the brief window before the subscription.* events arrive —
    fetch the freshly created subscription and apply it."""
    from application.models.user import User
    uid = session.get("client_reference_id") or \
        (session.get("metadata") or {}).get("user_id")
    user = User.objects(pk=uid).first() if uid else None
    sub_id = session.get("subscription")
    if user and session.get("customer") and not user.stripe_customer_id:
        user.stripe_customer_id = session["customer"]
        user.save()
    if not sub_id:
        return
    try:
        sub = _stripe().Subscription.retrieve(sub_id)
    except Exception as exc:
        log.warning("billing: could not retrieve subscription %s: %s", sub_id, exc)
        return
    _apply_subscription(sub, deleted=False)


# --------------------------------------------------------------------------
# nightly reconcile (cron backstop)
# --------------------------------------------------------------------------

def reconcile_all() -> dict:
    """Sweep every user: expire trials, downgrade lapsed subs, dissolve teams
    whose admin no longer pays. Idempotent — safe to run nightly. Returns
    counters for the CLI to print.

    This is a backstop for missed/late webhooks; in steady state webhooks keep
    everything in sync and this changes nothing.
    """
    from application.models.team import Team
    from application.models.user import User

    counters = {"downgraded": 0, "teams_dissolved": 0, "users": 0}

    # First, dissolve any team whose admin can no longer support it. Iterate a
    # snapshot — dissolve_team() deletes as it goes.
    for team in list(Team.objects()):
        admin = team.admin
        if admin is None or admin.level == "global_admin":
            if admin is None:
                dissolve_team(team)
                counters["teams_dissolved"] += 1
            continue
        active, plan = _entitlement(admin)
        if not (active and plan == "team"):
            dissolve_team(team)
            counters["teams_dissolved"] += 1

    # Then recompute everyone (trials that lapsed overnight, etc).
    for user in User.objects(level__ne="global_admin"):
        counters["users"] += 1
        before = user.level
        if recompute_level(user) != before:
            counters["downgraded"] += 1
    return counters
