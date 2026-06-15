"""Stripe subscription billing — seat-based team plans + the layer that turns
payment state into ``User.level``.

Model
-----
* **Premium (solo)** — €2/mo, one person.
* **Family team** — flat base (``FAMILY_BASE``) covering ``FAMILY_INCLUDED_SEATS``
  member seats; extra members billed at the cheaper ``SEAT_FAMILY`` price.
* **Organisation team** — every member seat billed at ``SEAT_ORG``.
* In every team the **paying admin's own seat is free** — billable seats are the
  members minus the payer.

A team may have several admins (``Team.admins``); exactly one — ``Team.admin`` —
is the **payer**, whose Stripe subscription carries the seats. The payer's
subscription quantity tracks the billable member count and is re-synced whenever
membership changes (:func:`sync_team_seats`).

``User.level`` stays the single gate the rest of the app reads. Webhooks and the
nightly ``reconcile-subscriptions`` sweep call :func:`recompute_level`, which
derives it (precedence): global_admin > active team payer (team_admin) > member
covered by a team's active billing (premium) > own premium sub (premium) >
trial (premium) > free. ``global_admin`` is never touched automatically.

Billing is OFF when ``STRIPE_SECRET_KEY`` is empty.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import current_app, url_for

log = logging.getLogger(__name__)


class BillingError(RuntimeError):
    """Billing can't proceed (Stripe not configured, or the API rejected it)."""


# --------------------------------------------------------------------------
# config / stripe client
# --------------------------------------------------------------------------

def _cfg(key: str, default=""):
    return current_app.config.get(key, default)


def billing_enabled() -> bool:
    return bool(_cfg("STRIPE_SECRET_KEY"))


def _stripe():
    if not billing_enabled():
        raise BillingError("Billing is not configured on this deployment.")
    import stripe
    stripe.api_key = _cfg("STRIPE_SECRET_KEY")
    return stripe


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_dt(epoch):
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc) if epoch else None


# Checkout keys → internal subscription_plan.
_CHECKOUT_PLANS = ("premium", "team", "team_family")


def _team_price_ids() -> set:
    return {pid for pid in (_cfg("STRIPE_PRICE_SEAT_ORG"),
                            _cfg("STRIPE_PRICE_FAMILY_BASE"),
                            _cfg("STRIPE_PRICE_SEAT_FAMILY")) if pid}


def _plan_for_prices(price_ids) -> str:
    """Map the set of a subscription's price ids to our plan key."""
    ids = {p for p in price_ids if p}
    if ids & _team_price_ids():
        return "team"
    if _cfg("STRIPE_PRICE_PREMIUM") and _cfg("STRIPE_PRICE_PREMIUM") in ids:
        return "premium"
    return ""


def _is_family_prices(price_ids) -> bool:
    base = _cfg("STRIPE_PRICE_FAMILY_BASE")
    return bool(base) and base in {p for p in price_ids if p}


def _checkout_line_items(plan: str):
    """The line items a fresh Checkout starts with. Seat items begin at the
    Stripe minimum (qty 1) and are corrected to the real billable count by
    :func:`sync_team_seats` as soon as the subscription exists."""
    if plan == "premium":
        return [{"price": _cfg("STRIPE_PRICE_PREMIUM"), "quantity": 1}]
    if plan == "team_family":
        return [{"price": _cfg("STRIPE_PRICE_FAMILY_BASE"), "quantity": 1}]
    if plan == "team":
        return [{"price": _cfg("STRIPE_PRICE_SEAT_ORG"), "quantity": 1}]
    raise BillingError(f"unknown plan {plan!r}")


# --------------------------------------------------------------------------
# seat accounting
# --------------------------------------------------------------------------

def billable_member_count(team) -> int:
    """Members occupying a paid seat — everyone in the team except the payer
    (whose own seat is free)."""
    from application.models.user import User
    payer_pk = team.admin.pk if team.admin else None
    return sum(1 for pk in User.objects(team=team).values_list("pk")
               if pk != payer_pk)


def desired_seat_items(team) -> dict:
    """Desired ``{price_id: quantity}`` for the team's subscription."""
    billable = billable_member_count(team)
    if team.family:
        items = {_cfg("STRIPE_PRICE_FAMILY_BASE"): 1}
        included = int(_cfg("FAMILY_INCLUDED_SEATS", 5) or 0)
        extra = max(0, billable - included)
        seat = _cfg("STRIPE_PRICE_SEAT_FAMILY")
        if seat:
            items[seat] = extra
        return items
    return {_cfg("STRIPE_PRICE_SEAT_ORG"): billable}


def team_cost_label(team) -> str:
    """Human-readable monthly cost for the team page (best-effort, display)."""
    billable = billable_member_count(team)
    if team.family:
        included = int(_cfg("FAMILY_INCLUDED_SEATS", 5) or 0)
        extra = max(0, billable - included)
        base = f"€5 base ({min(billable, included)}/{included} seats)"
        return base + (f" + {extra}×€2" if extra else "")
    return f"{billable}×€5" if billable else "€0 (no member seats yet)"


def sync_team_seats(team) -> bool:
    """Push the team's desired seat quantities to the payer's Stripe
    subscription. No-op when billing is off, the payer has no active sub, or
    nothing changed. Returns True if it modified the subscription."""
    payer = team.admin
    if not billing_enabled() or not payer or not payer.subscription_id:
        return False
    if not payer.has_active_subscription or payer.comp_plan:
        return False  # comp / inactive payers aren't metered
    stripe = _stripe()
    try:
        sub = stripe.Subscription.retrieve(payer.subscription_id)
    except Exception as exc:
        log.warning("seat sync: can't retrieve %s: %s", payer.subscription_id, exc)
        return False
    existing = {it["price"]["id"]: it for it in sub["items"]["data"]}
    desired = desired_seat_items(team)
    payload, changed = [], False
    for price_id, qty in desired.items():
        if not price_id:
            continue
        if price_id in existing:
            if int(existing[price_id]["quantity"]) != int(qty):
                payload.append({"id": existing[price_id]["id"], "quantity": qty})
                changed = True
        elif qty > 0:
            payload.append({"price": price_id, "quantity": qty})
            changed = True
    for price_id, it in existing.items():
        if price_id not in desired:
            payload.append({"id": it["id"], "deleted": True})
            changed = True
    if not changed:
        return False
    stripe.Subscription.modify(payer.subscription_id, items=payload,
                               proration_behavior="create_prorations")
    log.info("seat sync: team %s → %s", team.pk, desired)
    return True


# --------------------------------------------------------------------------
# entitlement / level recomputation
# --------------------------------------------------------------------------

def _entitlement(user):
    """``(active, plan)`` the user holds in their own right (own sub or comp)."""
    if user.comp_plan in ("team", "premium"):
        return True, user.comp_plan
    return user.has_active_subscription, (user.subscription_plan or "")


def team_billing_active(team) -> bool:
    """Is the team's seat billing currently covered (payer entitled to team)?"""
    payer = team.admin if team else None
    if not payer:
        return False
    active, plan = _entitlement(payer)
    return active and plan == "team"


def recompute_level(user, *, save: bool = True) -> str:
    if user.level == "global_admin":
        return user.level

    active, plan = _entitlement(user)
    covered = user.team is not None and team_billing_active(user.team)

    if active and plan == "team":
        new = "team_admin"
    elif covered:
        new = "premium"            # seat paid by the team's payer
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
    """A team payer must own exactly one Team and be in its admins list."""
    from application.models.team import Team
    owned = Team.objects(admin=user).first()
    if owned is None:
        owned = Team(name=f"{user.display_name or user.username}'s team",
                     admin=user, admins=[user]).save()
        log.info("billing: created team %s for %s", owned.pk, user.username)
    if not owned.admins or user.pk not in [a.pk for a in owned.admins if a]:
        owned.admins = list(owned.admins or []) + [user]
        owned.save()
    if user.team is None:
        user.team = owned


# --------------------------------------------------------------------------
# team dissolution
# --------------------------------------------------------------------------

def dissolve_team(team) -> int:
    from application.models.item import Item
    from application.services.permissions import team_members

    members = team_members(team)
    if not members:
        team.delete()
        return 0

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
        recompute_level(m, save=False)
        m.save()
    return len(members)


def _dissolve_owned_teams_if_unpaid(user) -> None:
    from application.models.team import Team
    active, plan = _entitlement(user)
    if active and plan == "team":
        return
    for team in Team.objects(admin=user):
        dissolve_team(team)


# --------------------------------------------------------------------------
# payer designation
# --------------------------------------------------------------------------

def set_team_payer(team, new_payer) -> None:
    """Hand the paying-admin role to ``new_payer`` (must already be an admin).

    Stripe doesn't move a subscription between customers, so the old payer's
    team subscription is cancelled and the new payer must start a team
    subscription via Checkout. Until they do, the team's billing is inactive
    (members fall back to free on the next recompute) — surface that in the UI.
    """
    if new_payer.pk not in [a.pk for a in (team.admins or []) if a]:
        raise BillingError("The new payer must be a team admin first.")
    old = team.admin
    if old and old.pk == new_payer.pk:
        return
    # Cancel the old payer's team subscription.
    if old and old.subscription_id and billing_enabled() and not old.comp_plan:
        try:
            _stripe().Subscription.cancel(old.subscription_id)
        except Exception as exc:  # pragma: no cover - best effort
            log.warning("payer switch: cancel %s failed: %s", old.subscription_id, exc)
    # Reassign the payer FIRST and persist it, so the recompute below sees the
    # team's (now unpaid) new payer — otherwise a fresh deref of the team would
    # still report the old payer as covering it and keep them premium.
    team.admin = new_payer
    team.save()
    if old:
        old.subscription_id = ""
        old.subscription_status = ""
        old.subscription_plan = ""
        old.comp_plan = "" if old.comp_plan == "team" else old.comp_plan
        old.save()
        recompute_level(old)
    recompute_level(new_payer)


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
    if plan not in _CHECKOUT_PLANS:
        raise BillingError(f"unknown plan {plan!r}")
    stripe = _stripe()
    line_items = _checkout_line_items(plan)
    if not all(li.get("price") for li in line_items):
        raise BillingError(f"no Stripe price configured for the {plan} plan.")
    stored_plan = "team" if plan in ("team", "team_family") else plan
    family = "1" if plan == "team_family" else "0"
    customer = _ensure_customer(user)
    meta = {"user_id": str(user.pk), "plan": stored_plan, "family": family}
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer,
        client_reference_id=str(user.pk),
        line_items=line_items,
        success_url=url_for("billing.success", _external=True)
        + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=url_for("billing.index", _external=True),
        metadata=meta,
        subscription_data={"metadata": meta},
    )
    return session["url"]


def create_portal_session(user) -> str:
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
    stripe = _stripe()
    secret = _cfg("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise BillingError("STRIPE_WEBHOOK_SECRET not configured.")
    try:
        return stripe.Webhook.construct_event(payload, sig_header, secret)
    except Exception as exc:
        raise BillingError(f"invalid webhook: {exc}") from exc


def handle_event(event) -> None:
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


def _user_for_subscription(sub):
    from application.models.user import User
    uid = (sub.get("metadata") or {}).get("user_id")
    user = User.objects(pk=uid).first() if uid else None
    if user is None and sub.get("customer"):
        user = User.objects(stripe_customer_id=sub["customer"]).first()
    return user


def _apply_subscription(sub, *, deleted: bool) -> None:
    from application.models.team import Team
    user = _user_for_subscription(sub)
    if user is None:
        log.warning("billing: subscription %s has no matching user", sub.get("id"))
        return

    items = (sub.get("items") or {}).get("data") or []
    price_ids = [it["price"]["id"] for it in items if it.get("price")]
    plan = _plan_for_prices(price_ids) or user.subscription_plan

    user.subscription_id = sub.get("id") or ""
    user.subscription_plan = plan
    user.subscription_status = "canceled" if deleted else (sub.get("status") or "")
    user.current_period_end = _to_dt(sub.get("current_period_end"))
    user.cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    user.save()

    if not (user.has_active_subscription and user.subscription_plan == "team"):
        _dissolve_owned_teams_if_unpaid(user)

    recompute_level(user)

    if user.has_active_subscription and user.subscription_plan == "team":
        team = Team.objects(admin=user).first()
        if team:
            fam = _is_family_prices(price_ids)
            if team.family != fam:
                team.family = fam
                team.save()
            # Correct the seat quantity to the real billable count (checkout
            # starts seats at qty 1). No-op if already in sync.
            sync_team_seats(team)


def _apply_checkout_completed(session) -> None:
    from application.models.user import User
    uid = session.get("client_reference_id") or \
        (session.get("metadata") or {}).get("user_id")
    user = User.objects(pk=uid).first() if uid else None
    if user and session.get("customer") and not user.stripe_customer_id:
        user.stripe_customer_id = session["customer"]
        user.save()
    sub_id = session.get("subscription")
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
    from application.models.team import Team
    from application.models.user import User

    counters = {"downgraded": 0, "teams_dissolved": 0, "users": 0}

    for team in list(Team.objects()):
        if team.admin is None:
            dissolve_team(team)
            counters["teams_dissolved"] += 1
            continue
        if team.admin.level == "global_admin":
            continue
        if not team_billing_active(team):
            dissolve_team(team)
            counters["teams_dissolved"] += 1

    for user in User.objects(level__ne="global_admin"):
        counters["users"] += 1
        before = user.level
        if recompute_level(user) != before:
            counters["downgraded"] += 1
    return counters
