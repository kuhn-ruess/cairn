"""Where Zammad's time lands in Cairn's billing: which customer, and whose.

Two questions the ticket sync has to answer before it may book a minute, both
of which used to be answered implicitly — and wrongly:

**Which customer?** Normally the Cairn subtree the ticket lives in is linked to
a billing project (``Item.time_project``) and that link *is* the opt-in. Teams
that run their whole client work out of Zammad don't want to hand-link every
project first, so ``User.zammad_time_auto_customer`` turns the ticket's Zammad
*organisation* into a Cairn customer + billing project on first sight
(``external_id = "zammad:org:<id>"``, so a re-sync reuses it instead of piling
up duplicates). Tickets from a customer without an organisation fall back to the
requesting person, under ``zammad:user:<id>`` — a solo client is still a client.

**Whose time?** Zammad books time per agent, and an invoice is wrong if it says
the wrong name. Agents are matched to Cairn accounts by email, then login, then
display name, and the answer is remembered on the *team*
(``Team.zammad_agents``) so every colleague's sync attributes the same booking
the same way. What cannot be matched is kept as an unmatched roster entry rather
than quietly booked under a bare name: the UI can then say "these three agents
have no Cairn account" and offer the assignment, which re-attributes their
existing entries too (:func:`assign_agent`).
"""
from __future__ import annotations

from datetime import datetime, timezone

from application.models.team import ZammadAgent
from application.models.timetracking import Customer, TimeBucket, TimeTrackingProject
from application.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def agent_key(zammad_id) -> str:
    """The identity stamped on every entry imported for a Zammad agent."""
    return f"zammad:user:{zammad_id}"


# ---------------------------------------------------------------------------
# Customers straight from Zammad
# ---------------------------------------------------------------------------

def ensure_billing_target(team, *, external_id: str, name: str
                          ) -> tuple[TimeTrackingProject | None, bool]:
    """The billing project for a Zammad organisation, created if new.

    Returns ``(project, created)``. Customer *and* project carry the same
    ``external_id`` so the pair is found again on the next sync — no name
    matching, which would break the moment someone renames the customer in
    Cairn (they may; we never rename back). The project is deliberately
    rate-less: hours are recorded, and whoever manages the team sets the rate
    once, on a real customer they recognise."""
    if not team or not external_id:
        return None, False
    name = (name or "").strip()[:200] or external_id

    from mongoengine import Q

    from application.services.timetracking import intake_project

    created = False
    customer = Customer.objects(
        Q(team=team) & (Q(external_id=external_id)
                        | Q(external_ids=external_id))).first()
    if customer is None:
        # Before inventing one: the same client may already be here under
        # another key (imported from mite, or typed in by hand). Matching by
        # name is what a person would do, and it is why "Flughafen München"
        # must not become two customers. The Zammad key is remembered on the
        # customer we found, so the next sync goes straight to it and the
        # original key stays untouched.
        customer = Customer.objects(team=team, name__iexact=name).first()
        if customer is not None:
            customer.external_ids = list(customer.external_ids or []) + [external_id]
            customer.save()
    if customer is None:
        customer = Customer(team=team, external_id=external_id, name=name,
                            note="Created by the Zammad time import.").save()
        created = True

    # A project marked as the customer's intake wins — that flag exists exactly
    # so the time can be redirected into a project you already had, after which
    # the auto-created one can be emptied and deleted.
    project = intake_project(team, customer)
    if project is None:
        project = TimeTrackingProject.objects(
            team=team, customer=customer, external_id=external_id).first()
    if project is None:
        # A customer that already had exactly one project doesn't need a second
        # one just because its time now arrives from Zammad.
        actives = list(TimeTrackingProject.objects(
            team=team, customer=customer, archived=False).limit(2))
        if len(actives) == 1:
            project = actives[0]
            project.intake = True
            project.save()
    if project is None:
        project = TimeTrackingProject(
            team=team, customer=customer, external_id=external_id, name=name,
            intake=True,
            note="Created by the Zammad time import.").save()
        created = True
    elif project.archived:
        # Archived on purpose → treat it as "don't book here", same as an
        # archived project resolved through a link.
        return None, False
    return project, created


# ---------------------------------------------------------------------------
# Agents → Cairn users
# ---------------------------------------------------------------------------

def _match_user(team, *, email: str, login: str, name: str):
    """Best automatic guess for the Cairn account behind a Zammad agent.

    Email first — the one identifier both systems agree on and the only one
    that is unique by construction. Login and display name are only consulted
    *within the team*, where a collision is something a human would notice."""
    email = (email or "").strip().lower()
    if email:
        hit = User.objects(email__iexact=email).first()
        if hit is not None:
            return hit, "email"
    from application.services.permissions import team_members
    members = team_members(team) if team else []
    login = (login or "").strip().lower()
    if login:
        for m in members:
            if (m.username or "").lower() == login:
                return m, "login"
    name = (name or "").strip().lower()
    if name:
        for m in members:
            if (m.display_name or "").lower() == name:
                return m, "name"
    return None, ""


def record_agent(team, *, zammad_id, email: str = "", login: str = "",
                 name: str = "", source: str = "zammad") -> ZammadAgent | None:
    """Upsert one Zammad agent into the team's roster and return it.

    Called once per agent per sync (the caller caches), and only writes when
    something actually changed — a roster of ten agents costs no writes on a
    steady sync. A **manual** assignment is never re-matched: the person who
    made it knows better than the matcher."""
    if not team or not zammad_id:
        return None
    zid = str(zammad_id)
    agent = team.zammad_agent(zid, source)
    dirty = False
    if agent is None:
        agent = ZammadAgent(zammad_id=zid, source=source)
        team.zammad_agents.append(agent)
        dirty = True
    for field, value in (("email", (email or "").strip()),
                         ("login", (login or "").strip()),
                         ("name", (name or "").strip())):
        if value and getattr(agent, field) != value:
            setattr(agent, field, value)
            dirty = True
    if agent.user is None and agent.matched_by != "manual":
        hit, how = _match_user(team, email=agent.email, login=agent.login,
                               name=agent.name)
        if hit is not None:
            agent.user = hit
            agent.matched_by = how
            dirty = True
    # Cheap heartbeat, but not worth a write of its own: only stamped when we
    # are saving anyway, plus once a day so "last seen" stays meaningful.
    last = agent.last_seen_at
    if dirty or last is None or (_utcnow() - last).total_seconds() > 86400:
        agent.last_seen_at = _utcnow()
        team.save()
    return agent


def actor_for(agent, fallback_user=None) -> dict:
    """The attribution snapshot for a booking made by ``agent``.

    A matched agent books under their Cairn account. An unmatched one keeps
    their Zammad name and *no* Cairn id — deliberately: filing a colleague's
    hours under whoever happened to run the sync is the failure mode this whole
    module exists to prevent. ``external_user`` rides along either way so a
    later assignment can find these entries."""
    if agent is None:
        who = fallback_user
        return {
            "user_id": str(who.id) if who else None,
            "user_name": (getattr(who, "display_name", None)
                          or getattr(who, "username", "") or "") if who else "",
            "external_user": "",
        }
    if agent.user:
        return {"user_id": str(agent.user.id),
                "user_name": (agent.user.display_name
                              or agent.user.username or agent.label),
                "external_user": agent.key}
    return {"user_id": None, "user_name": agent.label,
            "external_user": agent.key}


def assign_agent(team, zammad_id, cairn_user, source: str = "zammad"
                 ) -> tuple[ZammadAgent | None, int]:
    """Point a Zammad agent at a Cairn account (or clear it), and fix history.

    Returns ``(agent, entries_reattributed)``. The point of the mapping is the
    time already booked under the bare name from the source system, so the
    assignment reaches back over everything they have booked."""
    agent = team.zammad_agent(zammad_id, source) if team else None
    if agent is None:
        return None, 0
    agent.user = cairn_user or None
    agent.matched_by = "manual" if cairn_user else ""
    team.save()
    return agent, reattribute(team, agent)


def reattribute(team, agent) -> int:
    """Re-stamp the team's entries booked by ``agent`` with their Cairn name.

    Indexed on ``entries.external_user``, so this touches only buckets that
    actually hold this person's time — plus, as a second pass, the entries
    imported before that field existed, which can only be recognised by the
    name they were booked under. That pass is deliberately narrow: an import
    from *this* person's source system, still unattributed, and an exact name
    match.

    **Billed entries are re-attributed too**, which is the one thing the freeze
    deliberately does not cover. A lock protects what the work cost and what it
    says about itself; whose hours they are is neither. Imported history arrives
    already billed — everything mite ever settled comes across ``locked`` — so
    refusing here would have meant the mapping silently reached almost nothing,
    which is exactly how this was found."""
    who = actor_for(agent)
    src = f"{agent.source or 'zammad'}:"
    fixed = 0
    buckets = list(TimeBucket.objects(team=team, entries__external_user=agent.key))
    seen_ids = {b.pk for b in buckets}
    if agent.label:
        for b in TimeBucket.objects(team=team, entries__user_id=None):
            if b.pk not in seen_ids:
                buckets.append(b)
                seen_ids.add(b.pk)
    for bucket in buckets:
        for entry in bucket.entries:
            owned = entry.external_user == agent.key or (
                not entry.external_user
                and not entry.user_id
                and entry.user_name == agent.label
                and (entry.external_id or "").startswith(src)
            )
            if not owned:
                continue
            if (entry.user_id or None) == who["user_id"] and \
                    entry.user_name == who["user_name"]:
                continue
            res = TimeBucket.objects(
                pk=bucket.pk, entries__match={"entry_id": entry.entry_id},
            ).update_one(
                set__entries__S__user_id=who["user_id"],
                set__entries__S__user_name=who["user_name"],
                set__entries__S__external_user=agent.key,
                set__updated_at=_utcnow(),
            )
            if res:
                fixed += 1
    return fixed


def attribute_name(team, name: str, cairn_user) -> int:
    """Re-file every unattributed entry booked under ``name``.

    The escape hatch for time whose author was never identified at all — an
    import that ran before anyone was matched, or a booking made under a bare
    name. Matched by exactly that name and only among entries with no Cairn
    account, so it can never take someone else's hours; billed entries stay
    frozen."""
    from application.services.timetracking import _utcnow

    if not name or cairn_user is None:
        return 0
    who = {"user_id": str(cairn_user.id),
           "user_name": (cairn_user.display_name or cairn_user.username or name)}
    fixed = 0
    for bucket in TimeBucket.objects(team=team, entries__user_name=name):
        for entry in bucket.entries:
            if entry.user_id or entry.user_name != name:
                continue
            res = TimeBucket.objects(
                pk=bucket.pk, entries__match={"entry_id": entry.entry_id},
            ).update_one(
                set__entries__S__user_id=who["user_id"],
                set__entries__S__user_name=who["user_name"],
                set__updated_at=_utcnow(),
            )
            if res:
                fixed += 1
    return fixed


def agent_rows(team) -> list[dict]:
    """The roster as the settings page shows it — matched first, newest last.

    The mapped accounts are resolved in one query rather than by dereferencing
    each agent's ``user``, so a roster of thirty agents costs one lookup."""
    from application.models.team import Team

    if team is None:
        return []
    # Raw read: touching ``agent.user`` on the mongoengine document would
    # dereference one account per agent. The ids come back as ids, and the
    # accounts behind them in a single query.
    doc = Team.objects(pk=team.pk).only("zammad_agents").as_pymongo().first() or {}
    agents = doc.get("zammad_agents") or []
    ids = {a["user"] for a in agents if a.get("user")}
    names = {}
    if ids:
        for u in User.objects(id__in=list(ids)).only(
                "id", "username", "display_name"):
            names[str(u.id)] = u.display_name or u.username
    rows = []
    for a in agents:
        uid = str(a["user"]) if a.get("user") else ""
        rows.append({
            "zammad_id": a.get("zammad_id", ""),
            "source": a.get("source") or "zammad",
            "label": (a.get("name") or a.get("login") or a.get("email")
                      or f"#{a.get('zammad_id', '')}"),
            "email": a.get("email") or "",
            "login": a.get("login") or "",
            "user_id": uid,
            "user_name": names.get(uid, ""),
            "matched_by": a.get("matched_by") or "",
            "last_seen_at": a.get("last_seen_at"),
        })
    rows.sort(key=lambda r: (bool(r["user_id"]), r["label"].lower()))
    return rows
