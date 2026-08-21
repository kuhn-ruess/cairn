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
*new* ticket is filed **under the person who owns it in Zammad** — resolved
through the team's agent roster (``Team.zammad_agents``, the same one that
decides whose hours are whose), into that person's own target project. So the
tickets still land in each member's own area, without each member having to
fetch them.

A ticket that is **already here** is recognised, never moved: it may well sit in
a private project this team cannot see, put there by a person, and a sync has no
business overruling that — nor creating a second task beside it, which is what
the switch from personal to team syncing would otherwise produce on day one.

Whoever owns a ticket that nobody can be matched to keeps it: the run files it
under the team's admin rather than dropping it, and says how often that happened.
:func:`purge_team_import` is the way back out of all of it.
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
              debug: bool = False, trigger: str = "manual") -> zi.ZammadStats:
    """Pull the team's Zammad once: tickets to their owners, time to the team.

    Incremental like the per-user run (``Team.last_zammad_sync_at``), and with
    the same rule for closed tickets — kept out unless hours are booked on them,
    because those hours are real and somebody will invoice them.

    Every run leaves a note behind (:mod:`application.services.sync_log`), which
    is how a manager finds out what happened at four in the morning."""
    import time as _time

    from application.services.crypto import decrypt_for
    from application.services.permissions import team_members
    from application.services import sync_log

    began = _time.monotonic()

    stats = zi.ZammadStats()
    url = (team.zammad_url or "").strip()
    token = decrypt_for(team, team.zammad_token)
    if not url or not token:
        error = "This team has no Zammad connection — set a URL and a service token."
        sync_log.record("zammad-team", team=team, trigger=trigger, error=error)
        raise zi.ZammadError(error)

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

        # A ticket already imported here — by whoever's personal sync ran
        # before this team connection existed — is *recognised*, not moved. It
        # sits where a person put it, quite possibly in a private project this
        # team can't even see, and that is a decision the sync has no business
        # overruling; re-filing it would also mean two tasks for one ticket the
        # moment anything went wrong. Only tickets nobody has yet are filed
        # under their Zammad owner.
        existing = _existing_task(members, f"zammad:{raw.get('id')}")
        if existing is not None:
            owner = existing.owner
            target = existing.parent
            zammad_owner = owners.get(str(raw.get("owner_id") or ""))
            if zammad_owner is not None and str(zammad_owner.pk) != str(owner.pk):
                stats.tickets_foreign_owner += 1
        else:
            owner = owners.get(str(raw.get("owner_id") or ""))
            if owner is None:
                owner = fallback
                stats.tickets_unowned += 1
            target = _cached_target(targets, owner)

        try:
            zi._upsert_ticket(owner, raw, base, token, target, stats, dry_run,
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
    if not dry_run:
        sync_log.record("zammad-team", team=team, trigger=trigger, stats=stats,
                        seconds=_time.monotonic() - began)
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


def purge_team_import(team, *, dry_run: bool = True, tasks: bool = True,
                      time: bool = True) -> dict:
    """Take back what the Zammad import put here. Returns a count summary.

    Switching a live team over to a central sync is exactly the moment you want
    a way back: if the shape turns out wrong, the fix is to remove what was
    imported and import again, not to correct a thousand rows by hand. So this
    is deliberately blunt and deliberately *bounded* — it removes only what
    carries a Zammad identity:

    * tasks with ``source="zammad"`` owned by a member of this team (their
      comments go with them; a task someone has moved into a private project is
      still theirs and still goes),
    * time entries stamped ``zammad:…`` in this team's buckets,
    * customers and billing projects created *from* Zammad organisations, but
      only while nothing else is booked on them — a customer that has since
      collected hand-logged hours is a customer, not an import artefact,
    * the folder links pointing at those projects.

    Anything anyone typed here survives, which is the whole point: a purge you
    can't reason about isn't a way back, it's a second problem."""
    from application.models.timetracking import (
        Customer, TimeBucket, TimeTrackingProject)
    from application.services.permissions import team_members

    members = team_members(team)
    out = {"tasks": 0, "entries": 0, "projects": 0, "customers": 0,
           "unlinked": 0, "kept_projects": 0}

    if tasks:
        doomed = Item.all_objects(owner__in=members, source="zammad",
                                  external_id__startswith="zammad:")
        out["tasks"] = doomed.count()
        if not dry_run:
            doomed.delete()

    if not time:
        return out

    # Entries first: whether a project is an artefact or a real customer's
    # project depends on what is left once the imported hours are gone.
    for bucket in TimeBucket.objects(team=team,
                                     entries__external_id__startswith="zammad:"):
        keep = [e for e in bucket.entries
                if not (e.external_id or "").startswith("zammad:")]
        out["entries"] += len(bucket.entries) - len(keep)
        if dry_run:
            continue
        if keep:
            bucket.entries = keep
            bucket.entry_count = len(keep)
            bucket.total_minutes = sum(e.minutes or 0 for e in keep
                                       if e.status == "active")
            bucket.updated_at = _utcnow()
            bucket.save()
        else:
            bucket.delete()

    # Customers and projects the import invented, identified by the key it
    # stamped on them — never by their name.
    # "Is anything else booked here" has to be asked about the state *after*
    # the imported entries are gone — otherwise a dry run reports that a project
    # stays and the real run then deletes it, which is the one thing a preview
    # must never do.
    others: dict = {}
    for bucket in TimeBucket.objects(team=team).only("entries", "customer"):
        for e in bucket.entries:
            if (e.external_id or "").startswith("zammad:"):
                continue
            others[e.project_id] = others.get(e.project_id, 0) + 1

    survivors = set()
    for project in TimeTrackingProject.objects(team=team):
        imported = (project.external_id or "").startswith("zammad:")
        if not imported or others.get(str(project.id)):
            survivors.add(str(project.id))
            if imported:
                out["kept_projects"] += 1
            continue
        out["projects"] += 1
        if dry_run:
            continue
        out["unlinked"] += Item.all_objects(time_project=project).update(
            unset__time_project=True)
        project.delete()

    for customer in Customer.objects(team=team):
        keys = [customer.external_id or ""] + list(customer.external_ids or [])
        if not any(k.startswith("zammad:") for k in keys):
            continue
        # Only when nothing of theirs is left — a customer the import found by
        # name and stamped its key onto is somebody's real client.
        kept = [p for p in TimeTrackingProject.objects(team=team, customer=customer)
                if str(p.id) in survivors]
        if kept:
            continue
        out["customers"] += 1
        if not dry_run:
            customer.delete()
    return out


# Fields that make up "the connection" — everything the Zammad page writes.
# The token isn't here because it is encrypted against the team's id and has to
# be re-encrypted for the new one (services.crypto), not copied.
_CONNECTION_FIELDS = (
    "zammad_url", "zammad_auto_sync", "zammad_import_time",
    "zammad_time_auto_customer", "zammad_time_from", "zammad_include_closed",
    "last_zammad_sync_at",
)


def move_team_connection(src, dst) -> dict:
    """Move a Zammad connection (settings, token, agent roster) between teams.

    Configuring the connection under the wrong team is easy to do and annoying
    to undo by hand — the token is write-only in the UI and the agent roster is
    the work of several syncs. So this moves the whole thing in one step and
    leaves the old team without a connection, because two teams pointing at the
    same instance would both sync it.

    What was already *imported* stays where it is: those tasks, customers and
    hours belong to whoever they were booked for. Use ``purge_team_import`` on
    the old team and sync the new one if the data should move too."""
    from application.services.crypto import decrypt_for, encrypt_for

    for name in _CONNECTION_FIELDS:
        setattr(dst, name, getattr(src, name))
    dst.zammad_token = encrypt_for(dst, decrypt_for(src, src.zammad_token))
    dst.zammad_agents = list(src.zammad_agents or [])
    dst.save()

    src.zammad_url = ""
    src.zammad_token = ""
    src.zammad_auto_sync = False
    src.zammad_import_time = True
    src.zammad_time_auto_customer = False
    src.zammad_time_from = ""
    src.zammad_include_closed = False
    src.last_zammad_sync_at = None
    src.zammad_agents = []
    src.save()
    return {"agents": len(dst.zammad_agents or [])}
