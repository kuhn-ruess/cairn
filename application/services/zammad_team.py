"""One Zammad sync for a whole team, instead of one per member.

Why this exists
---------------
The per-user import was built around "my tickets": it authenticates as one
person, asks for the tickets they own, and takes the time booked on them. Two
things follow from that, and both are wrong for billing:

* every member walks the same instance to find their own slice — N syncs where
  one would do, each paying for the same ticket list;
* the time it finds is bounded by *whose tickets* they are. Hours booked on a
  colleague's ticket are outside the set, and so were tickets that opened and
  closed between two syncs. We patched both; the shape kept producing more.

A team run turns it around. One service token reads the instance once, and each
ticket is filed **under the person who owns it in Zammad** — resolved through
the team's agent roster (``Team.zammad_agents``, the same one that decides whose
hours are whose), into that person's own target project. So the tickets still
land in each member's own area, without each member having to fetch them.

Whoever owns a ticket that nobody can be matched to keeps it: the run files it
under the team's admin rather than dropping it, and says how often that happened.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from application.models.item import Item
from application.models.user import User
from application.services import zammad_import as zi


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _owner_map(team) -> dict:
    """Zammad user id → the Cairn account that owns their tickets here.

    Read straight off the roster, which is what the People page maintains — so
    "who is this agent" has exactly one answer in the whole module."""
    out = {}
    for agent in (team.zammad_agents or []):
        if agent.user and (agent.source or "zammad") == "zammad":
            out[str(agent.zammad_id)] = agent.user
    return out


def _target_project(user):
    """Where this member's tickets land: the project they picked, if it is
    still theirs — otherwise their inbox, which is never wrong, only busy."""
    pid = getattr(user, "zammad_sync_project", "") or ""
    if not pid:
        return None
    return Item.objects(pk=pid, owner=user, kind="project").first()


def _existing_task(team_members, external_id):
    """The task for this ticket, whoever in the team happens to own it."""
    return Item.objects(owner__in=team_members, external_id=external_id).first()


def sync_team(team, *, full: bool = False, dry_run: bool = False,
              debug: bool = False) -> zi.ZammadStats:
    """Pull the team's Zammad once: tickets to their owners, time to the team.

    Incremental like the per-user run (``Team.last_zammad_sync_at``), and with
    the same rule for closed tickets — kept out unless hours are booked on them,
    because those hours are real and somebody will invoice them."""
    from application.services.crypto import decrypt_for
    from application.services.permissions import team_members

    stats = zi.ZammadStats()
    url = (team.zammad_url or "").strip()
    token = decrypt_for(team, team.zammad_token)
    if not url or not token:
        raise zi.ZammadError(
            "This team has no Zammad connection — set a URL and a service token.")

    base = url.rstrip("/")
    if not base.endswith("/api/v1"):
        base = base + "/api/v1"

    since = None
    if not full and team.last_zammad_sync_at:
        since = team.last_zammad_sync_at - timedelta(
            minutes=zi._SINCE_OVERLAP_MIN)
        stats.since = since

    opts = zi.TimeOptions.for_team(team)
    members = team_members(team)
    owners = _owner_map(team)
    fallback = team.admin
    targets: dict = {}

    tickets = _fetch_tickets(base, token, stats, since)
    stats.tickets_fetched = len(tickets)
    stats.fetched_via = "team connection"

    seen_ids = {f"zammad:{t.get('id')}" for t in tickets if t.get("id")}
    for raw in tickets:
        if not team.zammad_include_closed:
            state = (raw.get("state") or raw.get("state_name") or "").lower()
            if state in zi._DONE_STATES:
                known = _existing_task(members, f"zammad:{raw.get('id')}")
                if known is None and not (opts.enabled
                                          and zi._ticket_books_time(raw)):
                    stats.filtered_closed += 1
                    continue
                if known is None:
                    stats.closed_with_time += 1

        owner = owners.get(str(raw.get("owner_id") or ""))
        if owner is None:
            owner = fallback
            stats.tickets_unowned += 1
        # A ticket that changed hands in Zammad follows: re-owning beats leaving
        # a copy behind in someone's tree and creating a second one next door.
        existing = _existing_task(members, f"zammad:{raw.get('id')}")
        if existing is not None and str(existing.owner.id) != str(owner.id):
            if not dry_run:
                existing.owner = owner
                existing.parent = _cached_target(targets, owner)
                existing.save()
            stats.tickets_reowned += 1

        try:
            zi._upsert_ticket(owner, raw, base, token,
                              _cached_target(targets, owner), stats, dry_run,
                              me_id=None, debug=debug, opts=opts)
        except zi.ZammadError as exc:
            stats.errors.append(f"ticket {raw.get('id')}: {exc}")
            stats.tickets_skipped += 1

    # Orphan reconcile is a full-pass decision, exactly as in the per-user run:
    # an incremental one asked for a slice, so "didn't come back" means "wasn't
    # touched lately".
    if not dry_run and since is None:
        for it in Item.objects(owner__in=members, kind="task", source="zammad",
                               external_id__startswith="zammad:"):
            if it.external_id in seen_ids or it.status == "done":
                continue
            it.mark_done(via="zammad")
            stats.tickets_dropped += 1

    if not dry_run:
        team.last_zammad_sync_at = _utcnow()
        team.save()
    stats.api_calls = next(zi._CALLS)
    return stats


def _cached_target(targets: dict, user):
    key = str(user.pk)
    if key not in targets:
        targets[key] = _target_project(user)
    return targets[key]


def _fetch_tickets(base: str, token: str, stats: zi.ZammadStats,
                   since) -> list[dict]:
    """Every ticket the service token can see, narrowed to what changed.

    Search first (Elasticsearch carries the cutoff for us), the plain list as
    the fallback — the same two paths the per-user import uses, minus the
    owner filter, which is the entire point of a team run."""
    import itertools

    zi._CALLS = itertools.count()
    tickets: list[dict] = []
    if since is not None:
        page = 1
        while page <= 50:
            query = f'updated_at:>="{since:%Y-%m-%dT%H:%M:%SZ}"'
            try:
                chunk = zi._request(base + "/tickets/search", token, params={
                    "query": query, "expand": "true",
                    "per_page": 200, "page": page})
            except zi.ZammadError as exc:
                stats.errors.append(f"search page {page}: {exc}")
                chunk = None
            chunk = _rows(chunk)
            if chunk is None:
                tickets = []
                break
            if not chunk:
                return tickets
            tickets.extend(chunk)
            page += 1
        if tickets:
            return tickets

    page = 1
    while page <= 50:
        try:
            chunk = zi._request(base + "/tickets", token, params={
                "expand": "true", "per_page": 200, "page": page})
        except zi.ZammadError as exc:
            stats.errors.append(f"page {page}: {exc}")
            break
        rows = _rows(chunk) or []
        if not rows:
            break
        tickets.extend(rows)
        page += 1
    if since is not None:
        # No search index: drop the untouched ones before they cost a request
        # each for articles, tags and time.
        fresh = []
        for raw in tickets:
            stamp = zi._stamp(raw.get("updated_at"))
            if stamp is not None and stamp < since:
                stats.tickets_unchanged += 1
                continue
            fresh.append(raw)
        tickets = fresh
    return tickets


def _rows(chunk):
    """Zammad hands search results back as a list, or wrapped in assets."""
    if isinstance(chunk, list):
        return chunk
    if isinstance(chunk, dict) and "tickets" in chunk:
        assets = (chunk.get("assets") or {}).get("Ticket") or {}
        return [assets[str(i)] for i in (chunk.get("tickets") or [])
                if str(i) in assets]
    return None


def due_team_ids(interval_min: int, *, force: bool = False) -> list[str]:
    """Teams whose Zammad is due for a background sync."""
    from application.models.team import Team

    cutoff = _utcnow() - timedelta(minutes=max(interval_min, 0))
    out = []
    for t in Team.objects(zammad_url__nin=[None, ""],
                          zammad_token__nin=[None, ""],
                          zammad_auto_sync=True).only(
                              "id", "last_zammad_sync_at"):
        if not force and t.last_zammad_sync_at and t.last_zammad_sync_at > cutoff:
            continue
        out.append(str(t.pk))
    return out
