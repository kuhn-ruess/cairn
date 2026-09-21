"""Refresh ONE imported Zammad ticket, on demand, from the inspector.

Why this exists
---------------
The sync is a *pass*: incremental, on an interval, over whatever the instance
says changed. Somebody looking at a ticket's task in the inspector is asking a
different question — "what does Zammad say about THIS one, now" — and until now
the only answer was to wait for the next run or to start a full one.

The rule this module is built around: **a click here and the background run
must not be able to disagree.** So it does not re-implement anything. It
resolves the same connection the sync would use, fetches the one ticket, and
hands it to :func:`zammad_import._upsert_ticket` — the same function the
per-user run and the team run both call. Whatever a sync updates (title, state,
tags, the first article as the note, later articles as comments, inline images,
booked time) is exactly what a refresh updates, because it is the same code.

Two things it deliberately does **not** do:

* it never stamps ``last_zammad_sync_at``. That field is the watermark an
  incremental run reads; moving it because one ticket was looked at would make
  the next run skip everything that changed in between.
* it never writes to Zammad. Read-only, like every other path in this app.
"""
from __future__ import annotations

from application.models.item import Item
from application.services import zammad_import as zi


class NotConfigured(zi.ZammadError):
    """No usable Zammad connection for this task's owner.

    Told apart from a ZammadError on purpose: this one is the user's own
    settings and is fixable here, so the view answers 400 rather than 502.
    """


def ticket_id(item) -> str:
    """Zammad's ticket id for ``item``, or ``""`` when it isn't one of ours."""
    if item is None:
        return ""
    return item.zammad_ticket_id()


def _api_base(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    if not base.endswith("/api/v1"):
        base = base + "/api/v1"
    return base


def connection_for(item):
    """``(base, token, opts, via)`` — the connection that speaks for this task.

    The **team** connection wins wherever there is one. That is the connection
    the ticket arrives through (:mod:`application.services.zammad_team`), and
    it is the one whose go-live date and billing target this ticket's hours are
    already booked against; refreshing through a personal token beside it would
    file the same hours a second time, into a different team. A user without a
    team connection falls back to their own, which is the older per-user sync.
    """
    from application.services.crypto import decrypt_for, decrypt_for_user
    from application.services.timetracking import default_team

    owner = item.owner
    if owner is None:
        raise NotConfigured("This task has no owner to ask Zammad as.")

    team = default_team(owner)
    if team is not None and team.zammad_url and team.zammad_token:
        token = decrypt_for(team, team.zammad_token)
        if not token:
            raise NotConfigured(
                "The team's saved Zammad token can't be read — re-enter it "
                "under Team → Zammad.")
        return (_api_base(team.zammad_url), token,
                zi.TimeOptions.for_team(team), "team")

    if owner.zammad_url and owner.zammad_token:
        if not owner.zammad_enabled:
            raise NotConfigured(
                "Zammad is switched off for this task's owner "
                "(Settings → Import from Zammad).")
        token = decrypt_for_user(owner, owner.zammad_token)
        if not token:
            raise NotConfigured(
                "The saved Zammad token can't be read — re-enter it under "
                "Settings → Import from Zammad.")
        return (_api_base(owner.zammad_url), token,
                zi.TimeOptions.for_user(owner), "user")

    raise NotConfigured(
        "No Zammad connection for this task's owner — set one up under "
        "Settings → Import from Zammad, or Team → Zammad.")


def explain(exc: Exception, zid: str) -> str:
    """Turn a raw transport failure into something the clicker can act on.

    ``_request`` reports the status line, which is the right thing in a sync
    log and the wrong thing in a panel beside a ticket number.
    """
    text = str(exc)
    if "HTTP 404" in text:
        return (f"Zammad doesn't know ticket {zid} any more — it was deleted, "
                "merged away, or the token can't see it.")
    if "HTTP 401" in text or "HTTP 403" in text:
        return ("Zammad refused the token. Check it under Settings → Import "
                "from Zammad (or Team → Zammad).")
    if "network error" in text or "refusing to" in text:
        return f"Couldn't reach Zammad: {text}"
    return f"Zammad said: {text}"


def refresh_item(item) -> zi.ZammadStats:
    """Ask Zammad for this one ticket and merge it in as a sync would.

    Raises :class:`NotConfigured` when there is nothing to ask with, and
    :class:`zammad_import.ZammadError` when the asking fails.
    """
    zid = ticket_id(item)
    if not zid:
        raise NotConfigured("This task didn't come from Zammad.")

    base, token, opts, via = connection_for(item)
    stats = zi.ZammadStats()
    stats.fetched_via = f"one ticket ({via} connection)"

    try:
        raw = zi._request(f"{base}/tickets/{zid}", token,
                          params={"expand": "true"})
    except zi.ZammadError as exc:
        raise zi.ZammadError(explain(exc, zid)) from exc
    if not isinstance(raw, dict) or not raw.get("id"):
        raise zi.ZammadError(
            f"Zammad didn't return ticket {zid} — nothing was changed here.")
    stats.tickets_fetched = 1

    # The owner, not the clicker: a ticket is filed under the account the sync
    # put it on, and _upsert_ticket finds the existing task by (owner,
    # external_id). Refreshing as somebody else would create a second task.
    # The parent stays put for the same reason the sync leaves it alone.
    zi._upsert_ticket(item.owner, raw, base, token, item.parent, stats,
                      False, me_id=None, opts=opts)

    # _upsert_ticket wrote through its OWN copy of the document, so the one in
    # hand is stale from here on.
    fresh = Item.objects(pk=item.pk).first() or item
    if _mirror_assignment(fresh, raw, opts.team):
        stats.tickets_assigned = 1
    return stats


def _mirror_assignment(item, raw, team) -> bool:
    """Hand the task to whoever owns the ticket in Zammad now.

    The team run does this for every imported ticket on every pass
    (``zammad_team._reconcile_assignments``); here it is the same policy
    (``_assignment_for``) asked about one task, so a hand-over shows up the
    moment somebody refreshes instead of on the next full run. Nothing to do
    without a team: the per-user sync has no agent roster to read an owner off.
    """
    if team is None:
        return False
    from application.services import zammad_team as zt

    owners = zt._owner_map(team)
    if not owners:
        return False
    roster_pks = {str(u.pk) for u in owners.values()}
    wanted = owners.get(str(raw.get("owner_id") or ""))
    assignee = item.assigned_to
    target = zt._assignment_for(
        item.owner.pk, assignee.pk if assignee else None, wanted, roster_pks)
    if target is zt.KEEP:
        return False
    item.assign_to(target, by_user=item.owner)
    item.save()
    return True
