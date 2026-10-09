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

What *does* follow Zammad is the **assignment**: whoever owns the ticket over
there is assigned the task here, so it reaches their Inbox and their search
even though it is filed on somebody else's account. That question is asked of
every imported ticket on every run (:func:`_reconcile_assignments`), not only
of the ones an incremental window happened to carry — otherwise a hand-over the
roster could not resolve at the time is lost for good, and the ticket's owner
cannot even find it.

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


# "Cairn already says the right thing" — the answer for almost every task on
# almost every run, and the one that must cost nothing.
KEEP = object()


def _assignment_for(owner_pk, assignee_pk, wanted, roster_pks: set):
    """What a task's assignment should become, given who owns its ticket now.

    ``wanted`` is the Cairn account the roster names for the ticket's current
    Zammad owner, or ``None`` when it can name nobody. Returns that account, or
    ``None`` to clear the assignment, or :data:`KEEP` to leave it alone.

    The rules, which are the whole of the policy:

    * a Zammad owner with no Cairn account leaves the assignment alone — there
      is nobody to name, and clearing it would lose a real one,
    * the ticket coming back to the task's own owner clears the assignment, but
      only when the current assignee is somebody this sync could have set;
      an assignment to anyone else was somebody's decision here,
    * otherwise the ticket's owner is the assignee, and stays it until Zammad
      or a person here says otherwise.
    """
    if wanted is None:
        return KEEP
    if str(wanted.pk) == str(owner_pk):
        if assignee_pk is None or str(assignee_pk) not in roster_pks:
            return KEEP
        return None                        # handed back → undo the delegation
    if assignee_pk is not None and str(assignee_pk) == str(wanted.pk):
        return KEEP                        # already says so
    return wanted


def _reconcile_assignments(members, owners: dict, roster_pks: set,
                           owner_by_ext: dict, *, dry_run: bool) -> int:
    """Mirror every imported ticket's Zammad owner into Cairn's *assignment*.

    Reassigning a ticket is the same statement as assigning a task here — it
    is still filed where it was, somebody else is now doing it — so it is
    written the same way: :meth:`Item.assign_to`, the method the UI calls. The
    assignee gets the same bell entry, the task turns up in their Inbox and
    their search, their own filing of it survives a round trip, and the owner
    keeps it in sight.

    The owner deliberately does not move. An imported task may sit in a project
    this team cannot even see, put there by a person; taking it out from under
    them is a decision a sync doesn't get to make.

    This runs over **every** imported ticket, not only the ones this run
    fetched, and that is the point. The old pass only looked at tickets the
    incremental window happened to carry, so a hand-over whose mirror could not
    be made at the time — the roster could not name the new owner yet — was
    lost for good: nothing ever asked about that ticket again, and the task sat
    on a colleague's account where its owner could not even search for it.
    Here the question is asked again on every run, for nothing: the ticket's
    owner is stored on the task (``Item.external_owner``), ``owner_by_ext``
    overrides it for the tickets this run did fetch, and the scan is one
    projected query with no dereference. Repair therefore follows the roster —
    match an agent today and their back catalogue arrives on the next run.

    Returns how many assignments changed, so a run can report it."""
    if not owners:
        return 0
    rows = (Item.objects(owner__in=members, kind="task", source="zammad",
                         external_id__startswith="zammad:")
            .only("owner", "assigned_to", "external_id", "external_owner")
            .as_pymongo())
    changed = 0
    for row in rows:
        zammad_owner = (owner_by_ext.get(row.get("external_id") or "")
                        or row.get("external_owner") or "")
        target = _assignment_for(row.get("owner"), row.get("assigned_to"),
                                 owners.get(str(zammad_owner)), roster_pks)
        if target is KEEP:
            continue
        changed += 1
        if dry_run:
            continue
        # Load the whole document only for the few that actually change: a
        # projected row is cheap to read and unsafe to save.
        item = Item.objects(pk=row["_id"]).first()
        if item is None:
            continue
        item.assign_to(target, by_user=item.owner)
        item.save()
    return changed


def _existing_task(team_members, external_id):
    """The task for this ticket, whoever in the team happens to own it.

    The trash counts: a trashed task is still this ticket's task and is
    updated where it lies (see ``zammad_import._upsert_ticket``). Through
    ``Item.objects`` it was invisible, and the ticket got a second task."""
    return Item.all_objects(owner__in=team_members, external_id=external_id,
                            template__ne=True).first()


def sync_team(team, *, full: bool = False, dry_run: bool = False,
              debug: bool = False, trigger: str = "manual",
              on_progress=None) -> zi.ZammadStats:
    """Pull the team's Zammad once: tickets to their owners, time to the team.

    Incremental like the per-user run (``Team.last_zammad_sync_at``), and with
    the same rule for closed tickets — kept out unless hours are booked on them,
    because those hours are real and somebody will invoice them.

    Every run leaves a note behind (:mod:`application.services.sync_log`), which
    is how a manager finds out what happened at four in the morning.

    ``on_progress(stage, done, total, stats)`` is called while it works — once
    for the ticket list, then once per ticket. A full pass takes minutes, and
    the page that started it needs something to show
    (:mod:`application.services.sync_progress`)."""

    def _tick(stage, done, total):
        if on_progress is None:
            return
        try:
            on_progress(stage, done, total, stats)
        except Exception:  # progress must never take the sync down with it
            pass
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

    # A connection goes live the day it first runs. Without a cutoff the run
    # would book the instance's whole time history — hours that are already
    # here from mite, under ids nothing can match against Zammad's, so they'd
    # arrive a second time. Stamped once and then left alone, so this is a
    # go-live date and not a rolling "today" that drops yesterday's bookings.
    if not (team.zammad_time_from or "").strip():
        team.zammad_time_from = zi.default_time_from()
        if not dry_run:
            team.save()

    opts = zi.TimeOptions.for_team(team)
    members = team_members(team)
    owners = _owner_map(team)
    # Who this sync is allowed to have assigned: an assignment to anyone else
    # came from a person here and is not ours to clear.
    roster_pks = {str(u.pk) for u in owners.values()}
    fallback = team.admin
    targets: dict = {}

    _tick("fetching", 0, 0)
    tickets, owner_by_ext = _fetch_tickets(base, token, stats, since)

    stats.tickets_fetched = len(tickets)
    _tick("running", 0, len(tickets))

    seen_ids = {f"zammad:{t.get('id')}" for t in tickets if t.get("id")}
    for done, raw in enumerate(tickets, start=1):
        _tick("running", done, len(tickets))
        if not team.zammad_include_closed:
            state = (raw.get("state") or raw.get("state_name") or "").lower()
            if state in zi._DONE_STATES:
                known = _existing_task(members, f"zammad:{raw.get('id')}")
                # A ticket that opened, got booked and closed between two runs
                # is only ever seen here, in this branch — so "does it carry
                # time" must not be answered from a field the install may not
                # maintain (see zi.closed_ticket_has_time).
                if known is None and not (opts.enabled and zi.closed_ticket_has_time(
                        base, token, raw, stats, incremental=since is not None)):
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
        zammad_owner = owners.get(str(raw.get("owner_id") or ""))
        if existing is not None:
            owner = existing.owner
            target = existing.parent
            if zammad_owner is None:
                # Nobody to hand it to. Counted, because this is exactly how a
                # hand-over in Zammad fails to arrive here, and it used to
                # happen in complete silence.
                stats.tickets_owner_unmapped += 1
            elif str(zammad_owner.pk) != str(owner.pk):
                stats.tickets_foreign_owner += 1
        else:
            owner = zammad_owner
            if owner is None:
                owner = fallback
                stats.tickets_unowned += 1
            target = _cached_target(targets, owner, stats)

        try:
            zi._upsert_ticket(owner, raw, base, token, target, stats,
                              dry_run, me_id=None, debug=debug, opts=opts)
        except zi.ZammadError as exc:
            stats.errors.append(f"ticket {raw.get('id')}: {exc}")
            stats.tickets_skipped += 1

    # A ticket that changed hands in Zammad: the task stays where it is and the
    # new agent is assigned it, exactly as a hand-off here would read. Over the
    # whole imported set, not just this run's slice — see the function.
    stats.tickets_assigned = _reconcile_assignments(
        members, owners, roster_pks, owner_by_ext, dry_run=dry_run)

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

    _tick("finishing", len(tickets), len(tickets))
    if not dry_run:
        team.last_zammad_sync_at = _utcnow()
        team.save()
    stats.api_calls = next(zi._CALLS)
    if not dry_run:
        sync_log.record("zammad-team", team=team, trigger=trigger, stats=stats,
                        seconds=_time.monotonic() - began)
    return stats


def _cached_target(targets: dict, user, stats=None):
    key = str(user.pk)
    if key not in targets:
        targets[key] = _target_project(user)
        # They picked a project and it is not there any more — trashed,
        # deleted, handed over. Every ticket of theirs lands in the inbox from
        # here on, which is the one outcome a sync must not reach quietly.
        # Once per run, into the log the team page shows.
        if (stats is not None and targets[key] is None
                and (getattr(user, "zammad_sync_project", "") or "")):
            stats.errors.append(
                f"{user.display_name or user.username}: the project picked for "
                "Zammad tickets is gone — filing into their inbox instead")
    return targets[key]


def _owner_ids(rows) -> dict:
    """``zammad:<id>`` → the ticket's Zammad owner id, for every row seen.

    Read off the list before it is narrowed to what changed: knowing who owns a
    ticket costs nothing here, and it is what lets
    :func:`_reconcile_assignments` put right a ticket this run had no other
    reason to touch."""
    return {f"zammad:{r.get('id')}": str(r.get("owner_id") or "")
            for r in rows if r.get("id")}


def _fetch_tickets(base: str, token: str, stats: zi.ZammadStats,
                   since) -> tuple[list[dict], dict]:
    """Every ticket the service token can see, narrowed to what changed.

    Answers with that narrowed list AND the Zammad owner of every ticket it
    saw on the way — the wider set, whenever the fallback below had to read the
    whole list anyway.

    Search first (Elasticsearch carries the cutoff for us), the plain list as
    the fallback — the same two paths the per-user import uses, minus the
    owner filter, which is the entire point of a team run.

    An **empty** search result is not evidence that nothing changed:
    ``/tickets/search`` needs Elasticsearch, and an install without it answers
    a Lucene-style ``updated_at:>=…`` query with a cheerful empty list rather
    than an error. Trusting that made every incremental run a no-op — the sync
    ran, said it was fine, and fetched nothing. Only a page that actually
    carries rows proves the search can answer; otherwise we fall through to the
    list and filter here."""
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
                # End of the results — or a search that cannot answer. Both
                # look the same, so only a non-empty page counts.
                break
            tickets.extend(chunk)
            page += 1
        if tickets:
            stats.fetched_via = "team connection (search)"
            return tickets, _owner_ids(tickets)

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
    stats.fetched_via = "team connection (full list)"
    owners = _owner_ids(tickets)
    if since is not None:
        # No search index: drop the untouched ones before they cost a request
        # each for articles, tags and time. Their OWNERS stay — that much we
        # already know, and it is free.
        fresh = []
        for raw in tickets:
            stamp = zi._stamp(raw.get("updated_at"))
            if stamp is not None and stamp < since:
                stats.tickets_unchanged += 1
                continue
            fresh.append(raw)
        tickets = fresh
    return tickets, owners


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


def refile_team_owners(team, *, dry_run: bool = True) -> dict:
    """Hand the imported tickets to the Cairn account their Zammad owner maps
    to. The one-time correction for what the roster couldn't answer yet.

    A run files each *new* ticket under its Zammad owner, read from the roster
    as it stands **when the run starts**. Before anybody is matched — the first
    run on a new connection — that answer is "nobody", so everything lands with
    the team's admin, and the next run won't touch it again: an imported ticket
    is recognised, never moved, because it may well have been filed by hand in
    the meantime.

    That is right as a sync rule and wrong as a one-off: after matching the
    agents, a whole import can be sitting in the wrong person's Inbox. This
    reaches back over exactly those — and only while they are still where the
    import put them:

    * unfiled (the fallback's Inbox), or still in the current owner's own
      Zammad target project — anything moved into a project by a person stays
      put, and is reported as kept,
    * owned by someone other than the account their Zammad owner maps to.

    Needs the connection, because the ticket's owner is in Zammad, not here."""
    from application.services.crypto import decrypt_for
    from application.services.external_tags import ensure_user_has_tags
    from application.services.permissions import team_members

    out = {"checked": 0, "moved": 0, "kept": 0, "unmatched": 0,
           "tickets": 0, "examples": [], "dry_run": dry_run}

    url = (team.zammad_url or "").strip()
    token = decrypt_for(team, team.zammad_token)
    if not url or not token:
        raise zi.ZammadError(
            "This team has no Zammad connection — the ticket owners are in "
            "Zammad, so there is nothing to read them from.")
    base = url.rstrip("/")
    if not base.endswith("/api/v1"):
        base = base + "/api/v1"

    owners = _owner_map(team)
    stats = zi.ZammadStats()
    _tickets, ticket_owner = _fetch_tickets(base, token, stats, None)
    out["tickets"] = len(ticket_owner)

    members = team_members(team)
    targets: dict = {}
    for item in Item.objects(owner__in=members, kind="task", source="zammad",
                             external_id__startswith="zammad:"):
        out["checked"] += 1
        wanted = owners.get(ticket_owner.get(item.external_id or "", ""))
        if wanted is None:
            # Nobody in the roster owns this ticket in Zammad — leave it with
            # whoever has it rather than guess.
            out["unmatched"] += 1
            continue
        if str(wanted.pk) == str(item.owner.pk):
            continue
        # Still where the import put it, or filed by a person since?
        here = _cached_target(targets, item.owner)
        if item.parent is not None and (here is None
                                        or str(item.parent.pk) != str(here.pk)):
            out["kept"] += 1
            continue
        out["moved"] += 1
        if len(out["examples"]) < 10:
            out["examples"].append({
                "title": item.title, "from": item.owner.display_name
                or item.owner.username, "to": wanted.display_name
                or wanted.username})
        if dry_run:
            continue
        item.owner = wanted
        item.parent = _cached_target(targets, wanted)
        item.updated_at = _utcnow()
        item.save()
        # The tag names travel with the task, but they are the *owner's* tags —
        # without this the chips render as bare strings for their new owner.
        ensure_user_has_tags(wanted, item.external_tags or [], group="Zammad")
    return out


def purge_zammad_time_before(team, day: str, *, dry_run: bool = True,
                             include_billed: bool = False) -> dict:
    """Remove time imported from Zammad that was booked **before** ``day``.

    The narrow companion to :func:`purge_team_import`: it takes hours and
    nothing else, bounded by a date. What it is for is the overlap — an import
    that reached further back than it should have and booked hours a second
    time, next to the ones the previous system already holds under its own ids,
    which nothing can match against Zammad's.

    Bounded three ways, because a delete that takes one row too many is worse
    than one that takes one too few:

    * only entries stamped ``zammad:…`` — anything typed here, and everything
      imported from anywhere else, is out of scope by construction,
    * only days strictly before ``day``, read in the deployment's timezone, the
      same way the import decides which day a booking belongs to,
    * billed (locked) entries are left alone and counted, unless
      ``include_billed`` says otherwise: a lock means somebody invoiced this.

    ``day`` is ``"YYYY-MM-DD"``. Returns a count summary; with ``dry_run`` it
    counts exactly what it would remove and touches nothing."""
    from application.models.timetracking import TimeBucket
    from application.services.tz import user_zone

    zone = user_zone(None)
    out = {"day": day, "dry_run": dry_run, "include_billed": include_billed,
           "entries": 0, "minutes": 0, "buckets": 0, "billed_kept": 0,
           "examples": []}
    if not day:
        return out

    for bucket in TimeBucket.objects(
            team=team, entries__external_id__startswith="zammad:"):
        keep, dropped = [], []
        for e in bucket.entries:
            if not (e.external_id or "").startswith("zammad:"):
                keep.append(e)
                continue
            when = e.spent_on.astimezone(zone).date().isoformat() \
                if e.spent_on else ""
            if not when or when >= day:
                keep.append(e)
                continue
            if e.locked and not include_billed:
                out["billed_kept"] += 1
                keep.append(e)
                continue
            dropped.append(e)
        if not dropped:
            continue
        out["buckets"] += 1
        out["entries"] += len(dropped)
        out["minutes"] += sum(e.minutes or 0 for e in dropped)
        for e in dropped:
            if len(out["examples"]) < 10:
                out["examples"].append({
                    "day": e.spent_on.astimezone(zone).date().isoformat(),
                    "minutes": e.minutes, "who": e.user_name or "—",
                    "note": (e.note or "")[:60]})
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
    return out
