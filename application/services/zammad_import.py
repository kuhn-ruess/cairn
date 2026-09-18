"""Import tickets from a Zammad instance as Cairn tasks.

Modelled after :mod:`application.services.todoist_import`:

* HTTP token auth (Zammad: ``Authorization: Token token=…``).
* Re-runs are idempotent — each ticket's Zammad id lands in
  ``Item.external_id`` with the prefix ``zammad:<id>`` so the next sync
  updates the existing row rather than creating a duplicate.
* The user picks one Cairn project as the import target. Without a
  target the tickets land in their inbox (parent=None).

Mapping
-------
* Ticket subject → ``title``
* First article body → ``note``; every later reply → a Cairn comment
  (authored by the external sender, deduped by the Zammad article id) so
  ticket updates keep flowing in on re-sync
* The images of an article → stored ON the task (``Item.note_images``, like a
  pasted image) with the body pointed at them, because "it looks like this" is
  half of what a ticket says and every way Zammad points at a picture needs
  Zammad to resolve it (see :func:`_absorb_images`)
* Priority "1 low"/"2 normal"/"3 high" → Cairn P4/P3/P2
* State "closed"/"merged"/"pending close" → ``status=done`` with
  ``completed_at`` stamped; every other state → ``status=open`` (a
  ticket that leaves "pending close" again reopens its Cairn task).
* ``ticket.url`` (if Zammad exposes one) is appended to the note as a
  back-link.
"""
from __future__ import annotations

import itertools
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from application.models.item import Comment, Item
from application.models.user import User

log = logging.getLogger(__name__)


# Transient HTTP statuses worth retrying (rate-limit + gateway/5xx). Anything
# else (auth, not-found, validation) is permanent and fails fast.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


class ZammadError(RuntimeError):
    """Wraps any HTTP or parse error so callers don't see the urllib stack."""


@dataclass
class ZammadStats:
    tickets_created: int = 0
    tickets_updated: int = 0
    tickets_skipped: int = 0
    # How many raw tickets the API actually handed back across all
    # pages — useful for diagnosing "import found nothing" issues
    # (token scope, missing permissions, wrong owner field, etc.).
    tickets_fetched: int = 0
    filtered_assignee: int = 0
    filtered_closed: int = 0
    tickets_dropped: int = 0  # orphans closed (status=done) during reconcile
    comments_imported: int = 0  # ticket replies mirrored in as comments
    images_imported: int = 0    # inline images pulled onto the task itself
    time_entries_imported: int = 0  # Zammad time accounting → Cairn time entries
    time_entries_repaired: int = 0  # older imports backfilled with title + link
    time_users_matched: int = 0     # Zammad agents resolved to a Cairn account
    time_users_unmatched: list = field(default_factory=list)
    time_customers_created: int = 0  # Zammad organisations turned into customers
    time_skipped_unlinked: int = 0   # tickets with time but nowhere to book it
    time_skipped_no_time: int = 0    # tickets Zammad reports no accounted time for
    time_skipped_old: int = 0        # bookings from before the cutoff day
    time_folders_linked: int = 0     # folders bound to a billing project by name
    # Incremental runs: what the ticket list was narrowed to, and how many
    # tickets the run therefore never had to look inside.
    since: Any = None
    tickets_unchanged: int = 0
    # Closed tickets pulled in anyway, because time is booked on them.
    closed_with_time: int = 0
    # Team runs: tickets whose Zammad owner has no Cairn account (filed under
    # the team's admin), and tickets already imported by someone whose account
    # isn't the ticket's Zammad owner — left where they are, on purpose.
    tickets_unowned: int = 0
    tickets_foreign_owner: int = 0
    # Tickets this run looked at whose Zammad owner the roster cannot name.
    # Their task keeps whatever assignment it has — but the miss is the reason
    # a hand-over in Zammad can fail to arrive here, so it is counted rather
    # than shrugged off.
    tickets_owner_unmapped: int = 0
    # Tickets whose Zammad owner changed hands: mirrored into Cairn's own
    # assignment rather than moved (see zammad_team._reconcile_assignments).
    tickets_assigned: int = 0
    time_task_links_cleared: int = 0  # per-task billing links an earlier sync wrote
    # Zammad user id → actor snapshot, cached for the run (not reported).
    actor_cache: dict = field(default_factory=dict)
    # Zammad organisation / customer id → billing project, cached for the run.
    target_cache: dict = field(default_factory=dict)
    # Team id → the ONE Team instance a run mutates. The agent roster lives on
    # the team document, so every write must go through the same object or a
    # second dereference would overwrite the first one's appends.
    team_cache: dict = field(default_factory=dict)
    me_id: Any = None
    fetched_via: str = ""
    # What the run cost. A sync happens inside one web request, so "how many
    # round trips did that take and how long did it run" is the number that
    # decides whether the next one ends at a result page or at the gateway's
    # timeout — worth reporting rather than guessing at.
    api_calls: int = 0
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)
    # Per-ticket decision log for the debug console. Only populated when a
    # sync is run with ``debug=True`` (the Settings → Zammad page), so the
    # frequent navbar sync stays lean. Each entry explains exactly what the
    # sync saw and what it did/would do — especially for tags.
    trace: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tickets_created": self.tickets_created,
            "tickets_updated": self.tickets_updated,
            "tickets_skipped": self.tickets_skipped,
            "tickets_fetched": self.tickets_fetched,
            "filtered_assignee": self.filtered_assignee,
            "filtered_closed": self.filtered_closed,
            "tickets_dropped": self.tickets_dropped,
            "comments_imported": self.comments_imported,
            "images_imported": self.images_imported,
            "time_entries_imported": self.time_entries_imported,
            "time_entries_repaired": self.time_entries_repaired,
            "time_users_matched": self.time_users_matched,
            "time_users_unmatched": list(self.time_users_unmatched),
            "time_customers_created": self.time_customers_created,
            "time_skipped_unlinked": self.time_skipped_unlinked,
            "time_skipped_no_time": self.time_skipped_no_time,
            "time_skipped_old": self.time_skipped_old,
            "time_folders_linked": self.time_folders_linked,
            "since": self.since.isoformat() if self.since else "",
            "tickets_unchanged": self.tickets_unchanged,
            "closed_with_time": self.closed_with_time,
            "tickets_unowned": self.tickets_unowned,
            "tickets_foreign_owner": self.tickets_foreign_owner,
            "tickets_owner_unmapped": self.tickets_owner_unmapped,
            "tickets_assigned": self.tickets_assigned,
            "time_task_links_cleared": self.time_task_links_cleared,
            "me_id": self.me_id,
            "fetched_via": self.fetched_via,
            "api_calls": self.api_calls,
            "elapsed_s": round(self.elapsed_s, 1),
            "errors": list(self.errors),
            "trace": list(self.trace),
        }


# Round trips made by the current run. A module-level counter rather than a
# threaded-through argument: _request is called from a dozen places and the
# number is diagnostics, not logic — two syncs sharing a process would share the
# count, which is a wrong number in a report, never a wrong import. Reset at the
# start of every import.
_CALLS = itertools.count()


def _request(url: str, token: str, *, params: dict | None = None,
             method: str = "GET", body: dict | None = None) -> Any:
    next(_CALLS)
    from application.services.http_safety import (
        UnsafeURLError, safe_urlopen, validate_outbound_url,
    )
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    # The Zammad instance URL is user-supplied — refuse to fetch from
    # private / loopback / link-local ranges so a tampered profile
    # can't aim our bearer-authenticated request at internal services.
    # safe_urlopen below re-runs this on every redirect hop too — without that
    # a public host could 302 our token-bearing request at an internal address.
    try:
        validate_outbound_url(url)
    except UnsafeURLError as exc:
        raise ZammadError(f"refusing to fetch: {exc}") from exc
    data = None
    headers = {
        "Authorization": f"Token token={token}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    # Retry transient failures with exponential backoff. Without this, a
    # single rate-limit (429) or brief network blip on a per-ticket /tags or
    # /articles call just skips that piece for the run — which is why a sync
    # "eventually works after a few tries". Permanent client errors
    # (401/403/404/422 …) are NOT retried — they won't fix themselves.
    last_err: "ZammadError | None" = None
    for attempt in range(_MAX_RETRIES + 1):
        retry_after = None
        try:
            with safe_urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return None
                return json.loads(raw)
        except UnsafeURLError as exc:
            # A redirect aimed somewhere non-routable. Never retried — the
            # server is choosing the target, so it won't resolve itself.
            raise ZammadError(f"refusing to follow redirect: {exc}") from exc
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRY_STATUSES:
                body = exc.read().decode("utf-8", errors="replace")[:300]
                raise ZammadError(f"HTTP {exc.code} from {url}: {body}")
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            last_err = ZammadError(f"HTTP {exc.code} from {url}")
        except urllib.error.URLError as exc:
            last_err = ZammadError(f"network error: {exc.reason}")
        except json.JSONDecodeError as exc:
            # A truncated body from an overloaded server can re-read cleanly.
            last_err = ZammadError(f"unparseable JSON from {url}: {exc}")
        if attempt < _MAX_RETRIES:
            if retry_after and str(retry_after).isdigit():
                delay = min(float(retry_after), 30.0)
            else:
                delay = 2.0 ** attempt          # 1s, 2s, 4s …
            log.warning("Zammad %s — retrying in %.0fs (attempt %d/%d)",
                        last_err, delay, attempt + 1, _MAX_RETRIES)
            time.sleep(delay)
    raise last_err


# States that mean "finished" in Cairn. "pending close" counts: the agent has
# done the work and only Zammad's timer is left, so the task shouldn't keep
# sitting in a perspective. Zammad hands back the state *name*, which is
# English for the stock states even on a German UI — but installs with renamed
# states exist, so the German label is accepted too.
_DONE_STATES = {
    "closed", "merged",
    "pending close", "warten auf schließen", "warten auf schliessen",
}


# How far back an incremental run reaches beyond the last sync. Clocks drift,
# a run takes a moment, and Zammad's index is written a heartbeat after the
# ticket — a few minutes of overlap costs almost nothing (the tickets in it are
# re-checked, not re-created) and is the difference between "cheap" and "loses
# an edit now and then".
_SINCE_OVERLAP_MIN = 10


def import_for_user(
    user: User,
    url: str,
    token: str,
    *,
    project: Item | None = None,
    assignee_only: bool = True,
    include_closed: bool = False,
    dry_run: bool = False,
    debug: bool = False,
    full: bool = False,
    trigger: str = "manual",
) -> ZammadStats:
    """Pull tickets from Zammad and merge them into Cairn for ``user``.

    Read-only against Zammad — we never write anything back to the
    ticketing system. ``project`` is the destination — pass ``None``
    to drop tickets in the user's inbox. ``assignee_only`` filters to
    tickets owned by the token holder.

    Orphan reconcile: any task with ``source="zammad"`` whose
    ``external_id`` didn't come back in this response is treated as
    closed server-side and marked done in Cairn.
    """
    base = url.rstrip("/")
    if not base.endswith("/api/v1"):
        base = base + "/api/v1"

    stats = ZammadStats()
    global _CALLS
    _CALLS = itertools.count()
    started = time.monotonic()

    # Incremental by default: only tickets touched since the last successful
    # run. A sync costs two requests *per ticket* (articles, tags) plus a third
    # when there is time on it, so walking the whole history every few minutes
    # is the difference between a sync that finishes and one that times out —
    # and on a quiet instance, most runs now do nothing but ask.
    # Whether this run cares about time at all — decides, among other things,
    # whether a closed ticket with hours on it is worth pulling in.
    billing = bool(getattr(user, "zammad_import_time", True))
    since = None
    if not full and user.last_zammad_sync_at:
        since = user.last_zammad_sync_at - timedelta(minutes=_SINCE_OVERLAP_MIN)
        stats.since = since

    # /tickets/search needs Elasticsearch on the Zammad side. Plenty
    # of self-hosted installs don't have ES wired up, in which case
    # the search endpoint silently returns nothing. The plain
    # /tickets list endpoint always works — pull from that and filter
    # client-side. expand=true gives us state_name / priority_name in
    # the response so we don't need a second roundtrip per state.
    # Resolve "me" up-front for the assignee filter and store it on
    # stats so the result page can show the actual ID being matched
    # against.
    me_id = None
    try:
        me = _request(base + "/users/me", token)
        me_id = me.get("id") if isinstance(me, dict) else None
        stats.me_id = me_id
    except ZammadError as exc:
        stats.errors.append(f"users/me: {exc}")
        if assignee_only:
            return stats

    # Server-side filter via /tickets/search with a Zammad query
    # expression. ``owner_id:<me>`` scopes to tickets where the token
    # holder is the owner — exactly the "mine" semantic the user
    # expects, without pulling the entire instance's ticket list and
    # filtering locally. (The ``my_assigned`` overview was unreliable:
    # depending on the install it also returns participant / focus
    # relationships, so we ended up either missing tickets or pulling
    # everything visible.)
    overview_used = False
    tickets: list = []

    if assignee_only and me_id is not None:
        page = 1
        while True:
            try:
                query = f"owner_id:{me_id}"
                if since is not None:
                    # Zammad's search speaks Elasticsearch's range syntax.
                    query += f' AND updated_at:>="{since:%Y-%m-%dT%H:%M:%SZ}"'
                chunk = _request(
                    base + "/tickets/search",
                    token,
                    params={
                        "query": query,
                        "expand": "true",
                        "per_page": 200,
                        "page": page,
                    },
                )
            except ZammadError as exc:
                stats.errors.append(f"owner search page {page}: {exc}")
                tickets = []
                break
            if not isinstance(chunk, list):
                # Some Zammad versions wrap the result in
                # ``{tickets:[ids], assets:{Ticket:{...}}}``.
                if isinstance(chunk, dict) and "tickets" in chunk:
                    ids = chunk.get("tickets") or []
                    assets = (chunk.get("assets") or {}).get("Ticket") or {}
                    chunk = [assets[str(i)] for i in ids if str(i) in assets]
                else:
                    chunk = []
            if not chunk:
                break
            tickets.extend(chunk)
            overview_used = True
            page += 1
            if page > 50:
                break

    if not overview_used:
        # Fall-through path: pull every visible ticket and filter
        # client-side. Pagination only stops when Zammad returns an
        # empty page (the server-side per_page cap varies by install).
        page = 1
        while True:
            try:
                chunk = _request(
                    base + "/tickets",
                    token,
                    params={"expand": "true", "per_page": 200, "page": page},
                )
            except ZammadError as exc:
                stats.errors.append(f"page {page}: {exc}")
                break
            if not isinstance(chunk, list) or not chunk:
                break
            tickets.extend(chunk)
            page += 1
            if page > 50:  # safety net: 10 000 tickets max
                stats.errors.append(
                    "stopped paginating after 50 pages — raise the cap if "
                    "you really have more than 10 000 tickets."
                )
                break

    stats.tickets_fetched = len(tickets)
    # Surface whether the server-side filter kicked in — useful when
    # diagnosing why the count looks unexpectedly small (overview path)
    # or large (full list path).
    stats.fetched_via = "overview my_assigned" if overview_used else "full /tickets list"

    # Every Zammad ticket ID we saw in the response — collected BEFORE
    # the local filters so the orphan-reconcile pass can tell apart
    # "ticket vanished from Zammad" (close it) from "ticket got
    # filtered by the user's options" (leave it alone).
    seen_zammad_ids: set = set()
    for raw in tickets:
        zid = raw.get("id")
        if zid is not None:
            seen_zammad_ids.add(f"zammad:{zid}")

    # The fallback path (no Elasticsearch) fetches the list — cheap, a page of
    # 200 at a time — and drops the untouched ones here, before they cost a
    # request each for articles, tags and time.
    if since is not None and not overview_used:
        fresh = []
        for raw in tickets:
            stamp = _stamp(raw.get("updated_at"))
            if stamp is not None and stamp < since:
                stats.tickets_unchanged += 1
                continue
            fresh.append(raw)
        tickets = fresh

    filtered: list[dict] = []
    for raw in tickets:
        # "Assigned to me" needs to cover both sides of a Zammad
        # ticket: the agent (``owner_id``) and the customer
        # (``customer_id``). Newly-filed tickets often have no agent
        # yet — those would silently drop out if we only checked
        # owner_id.
        if assignee_only and me_id is not None:
            owner_id = raw.get("owner_id")
            customer_id = raw.get("customer_id")
            if owner_id != me_id and customer_id != me_id:
                stats.filtered_assignee += 1
                continue
        if not include_closed:
            state = (raw.get("state") or raw.get("state_name") or "").lower()
            if state in _DONE_STATES:
                # Don't create NEW tasks for already-closed tickets (incl.
                # "pending close"), but a ticket we previously imported and
                # that has since been closed in Zammad MUST still flow through
                # so its Cairn task gets marked done — otherwise closing in
                # Zammad never propagates here. Only brand-new closed tickets
                # are skipped; if such a ticket goes back to open later, the
                # next sync imports it as a fresh task.
                zid = raw.get("id")
                already = (
                    zid is not None
                    and Item.objects(
                        owner=user, external_id=f"zammad:{zid}"
                    ).first() is not None
                )
                # …with one exception, and it is the important one: a ticket
                # worked on and closed in the same breath. Its hours are real
                # and someone will invoice them; refusing it because nobody
                # imported the ticket *before* it closed would lose the work,
                # quietly, for exactly the tickets that were finished quickly.
                # Only when there is booked time — and "Zammad didn't say" is
                # asked rather than assumed, inside an incremental window
                # (closed_ticket_has_time).
                if not already and (not billing or not closed_ticket_has_time(
                        base, token, raw, stats, incremental=since is not None)):
                    stats.filtered_closed += 1
                    continue
                if not already:
                    stats.closed_with_time += 1
        filtered.append(raw)
    tickets = filtered

    for raw in tickets:
        try:
            _upsert_ticket(user, raw, base, token, project, stats, dry_run,
                           me_id=me_id, debug=debug)
        except ZammadError as exc:
            stats.errors.append(f"ticket {raw.get('id')}: {exc}")
            stats.tickets_skipped += 1

    # Orphan reconcile: a task with source=zammad whose external_id
    # didn't come back this run is closed server-side → mark done.
    # Tasks filtered out by the user's options stay untouched.
    #
    # Only on a full run: an incremental one deliberately asked for a slice of
    # the instance, so "didn't come back" means "wasn't touched lately", and
    # closing everything on that basis would be a small catastrophe.
    if not dry_run and since is None:
        orphans = Item.objects(
            owner=user, kind="task", source="zammad",
            external_id__startswith="zammad:",
        )
        for it in orphans:
            if it.external_id in seen_zammad_ids:
                continue
            if it.status == "done":
                continue  # already closed, nothing to do
            # mark_done records when + that the Zammad sync closed it.
            it.mark_done(via="zammad")
            stats.tickets_dropped += 1
            if debug:
                stats.trace.append({
                    "number": it.external_ref or "",
                    "zid": (it.external_id or "").split(":", 1)[-1],
                    "title": it.title,
                    "action": "closed-orphan",
                    "note": "no longer returned by Zammad — marked done",
                })

    if not dry_run:
        user.last_zammad_sync_at = datetime.now(timezone.utc)
        user.save()
    stats.api_calls = next(_CALLS)
    stats.elapsed_s = time.monotonic() - started
    if not dry_run:
        # One note per run, kept for 90 days — see services.sync_log.
        from application.services import sync_log
        sync_log.record("zammad-user", team=getattr(user, "team", None),
                        user=user, trigger=trigger, stats=stats,
                        seconds=stats.elapsed_s)
    return stats


def _ticket_tags(base: str, token: str, zid: str) -> list[str]:
    """Current Zammad tags for one ticket (``[]`` on error/none)."""
    try:
        resp = _request(base + "/tags", token,
                        params={"object": "Ticket", "o_id": zid})
    except ZammadError:
        return []
    tags = (resp or {}).get("tags") if isinstance(resp, dict) else []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    return [t for t in (tags or []) if t]


def reconcile_tags(user: User, url: str, token: str, *, dry_run: bool = False) -> dict:
    """Re-overwrite each Zammad task's *Zammad-group* tags with the live set.

    One-off repair to drop stale upstream labels left over from older
    imports. Only tags under the "Zammad" group are reset to exactly what
    Zammad reports now; user-added context tags (and tags from other
    importers) are left untouched. ``apply_label_rules`` then re-derives
    status. This mirrors the normal sync — same group-based overwrite.
    """
    from application.services.external_tags import (
        ensure_user_has_tags, overwrite_group_tags,
    )
    from application.services.label_rules import apply_label_rules

    base = url.rstrip("/")
    if not base.endswith("/api/v1"):
        base = base + "/api/v1"

    changed = 0
    scanned = 0
    for it in Item.all_objects(owner=user, source="zammad",
                               external_id__startswith="zammad:"):
        scanned += 1
        zid = it.external_id.split(":", 1)[1]
        live = _ticket_tags(base, token, zid)
        if live:
            ensure_user_has_tags(user, live, group="Zammad")
        zammad_owned = {t.name for t in (user.tags or [])
                        if t.parent == "Zammad" or t.name == "Zammad"}
        # Fold in the task's previous external_tags — Zammad's own last-set
        # tags — so stale labels that were never parented under "Zammad" still
        # get dropped. (Same reasoning as the per-ticket sync in _upsert.)
        owned = zammad_owned | set(it.external_tags or [])
        new_tags = overwrite_group_tags(it.tags, owned, live)
        if set(new_tags) == set(it.tags or []) and set(live) == set(it.external_tags or []):
            continue
        changed += 1
        if dry_run:
            continue
        it.tags = new_tags
        it.external_tags = list(live)
        apply_label_rules(it, user)
        it.save()
    return {"scanned": scanned, "changed": changed}


def _upsert_ticket(
    user: User,
    raw: dict,
    base: str,
    token: str,
    project: Item | None,
    stats: ZammadStats,
    dry_run: bool,
    me_id=None,
    debug: bool = False,
    opts: "TimeOptions | None" = None,
) -> "Item | None":
    """Create or update a Cairn task for a Zammad ticket and return it.

    Returns ``None`` for dry-run or invalid input. The caller uses the
    returned Item to track which tasks were touched this sync (for
    the orphan-cleanup pass)."""
    zid = str(raw.get("id") or "")
    if not zid:
        stats.tickets_skipped += 1
        return None
    # ``id`` is Zammad's internal numeric id (stable, used for dedup);
    # ``number`` is the user-facing Ticket# shown in Zammad's UI. Both
    # exist on every ticket object — we display ``number`` and dedupe
    # by ``id``.
    znum = str(raw.get("number") or "") or zid

    todoist_key = f"zammad:{zid}"
    title = (raw.get("title") or f"Zammad ticket {zid}").strip()[:500]

    # Identification is strictly via the Zammad-stamped external_id.
    existing = Item.objects(owner=user, external_id=todoist_key).first()
    # Priority is intentionally NOT imported — every Zammad ticket carries a
    # default priority ("2 normal"), which would stamp a Cairn priority on
    # every task. Leave it to the user.
    state = (raw.get("state") or raw.get("state_name") or "").lower()
    # Anything that isn't finished lands as plain "open" — Zammad tickets are
    # inbound work, not something the user already committed to as a next
    # action. Flipping a ticket back out of "pending close" therefore reopens
    # the Cairn task (save() clears the completion record on that transition).
    cairn_status = "done" if state in _DONE_STATES else "open"

    # Pull the whole article thread once. The first article (the original
    # message) is the task note; every later reply is mirrored in as a
    # comment further down so ticket updates actually reach Cairn.
    articles = _fetch_articles(base, token, zid, stats)
    note_parts = []
    if articles:
        first = _article_body(articles[0])
        if first:
            note_parts.append(first)
    # Back-link to the Zammad ticket itself for context.
    web_url = base.replace("/api/v1", "") + f"/#ticket/zoom/{zid}"
    note_parts.append(f"[Zammad #{zid}]({web_url})")
    note = "\n\n".join(note_parts)

    # Mirror Zammad tags onto the Cairn task. The Zammad side is
    # authoritative for tags it owns — they live in ``external_tags``
    # and get replaced wholesale on each sync. Anything the user added
    # in Cairn (= tags present on the task but not in the old
    # external_tags) is preserved untouched.
    #
    # Fetched here (read-only) BEFORE the dry-run gate so the debug console
    # can preview exactly what a real sync would do to the tags.
    #
    # Zammad's /tickets responses don't carry tags (they live in a
    # separate object). We fetch them per ticket via /tags?object=
    # Ticket&o_id=<id>. The endpoint returns ``{"tags": [...]}``.
    new_external: list[str] = []
    tags_fetched = False
    try:
        tag_resp = _request(
            base + "/tags", token,
            params={"object": "Ticket", "o_id": zid},
        )
        z_tags = (tag_resp or {}).get("tags") if isinstance(tag_resp, dict) else []
        if isinstance(z_tags, str):
            z_tags = [t.strip() for t in z_tags.split(",") if t.strip()]
        new_external = [t for t in (z_tags or []) if t]
        tags_fetched = True
    except ZammadError as exc:
        stats.errors.append(f"tags for {zid}: {exc}")

    from application.services.external_tags import (
        ensure_user_has_tags, overwrite_group_tags,
    )
    from application.services.label_rules import apply_label_rules

    # A tag is Zammad-owned — so a sync may drop it — if Zammad SET it: that is
    # this task's previous ``external_tags``, the authoritative per-task record.
    #
    # Group membership is NOT enough on its own, and used to be: a tag the user
    # added by hand in Cairn was deleted on the very next sync purely because it
    # lives under the "Zammad" group ("I tagged it myself because it hadn't come
    # over from Zammad yet"). And the tag took the status with it — the rule that
    # derives waiting_reply from it runs after the tags are written, finds
    # nothing, and the task sits there open. Tagging by hand is the normal way to
    # get ahead of the sync; it must survive it.
    #
    # The group still stands in where there is no record to go by — a task
    # imported before external_tags existed — so a stale label from those days
    # is still replaced. Such a task heals on its first sync: whatever Zammad
    # says then becomes the record.
    #
    # A failed tag fetch (``tags_fetched`` False) leaves tags as they are rather
    # than wiping them on a transient error.
    zammad_owned = {t.name for t in (user.tags or [])
                    if t.parent == "Zammad" or t.name == "Zammad"}
    old_tags = list(existing.tags or []) if existing else []
    recorded = set(existing.external_tags or []) if existing else set()
    owned = recorded or (zammad_owned if existing else set())
    if existing and tags_fetched:
        would_tags = overwrite_group_tags(old_tags, owned, new_external)
    elif existing:
        would_tags = old_tags          # fetch failed → leave tags as-is
    else:
        would_tags = list(new_external)  # new task → exactly the live set

    if debug:
        stats.trace.append({
            "number": znum,
            "zid": zid,
            "title": title,
            "state": state,
            "exists": bool(existing),
            "action": (("would-update" if existing else "would-create")
                       if dry_run else ("update" if existing else "create")),
            "status_to": cairn_status,
            "tags_fetched": tags_fetched,
            "live_tags": list(new_external),
            "old_tags": old_tags,
            "owned": sorted(owned),
            "new_tags": list(would_tags),
            "tag_changed": set(would_tags) != set(old_tags),
        })

    if dry_run:
        if existing:
            stats.tickets_updated += 1
        else:
            stats.tickets_created += 1
        return None

    # Real run only past here. Make sure the live tags exist as styled tags
    # under the Zammad group (skipped above in dry-run so we don't mutate
    # user.tags while only previewing).
    if new_external:
        ensure_user_has_tags(user, new_external, group="Zammad")

    if existing:
        existing.title = title
        existing.note = note
        existing.source = "zammad"
        existing.external_ref = znum
        # Who owns the ticket over there, kept on the task so a later run can
        # still put the assignment right once the roster can name them.
        existing.external_owner = str(raw.get("owner_id") or "")
        if tags_fetched:
            existing.tags = overwrite_group_tags(
                existing.tags, owned, new_external)
            existing.external_tags = new_external
        # A task the user parked as "scheduled" with a plan date is a
        # deliberate deferral ("I'll deal with this on day X"). A sync must
        # leave that status alone — neither the Zammad-state mapping below
        # nor the tag rules may yank it back to "open". A Zammad *close*
        # still wins (cairn_status == "done") so a finished ticket doesn't
        # linger forever as a scheduled task.
        keep_scheduled = (
            existing.kind == "task"
            and existing.status == "scheduled"
            and existing.plan_date is not None
            and cairn_status != "done"
        )
        # Task vs project have completely different status vocabularies
        # (open/next/waiting vs active/on_hold/completed). The
        # ``cairn_status`` we compute from Zammad's state only fits
        # tasks. For a project the user has clearly chosen to manage
        # the lifecycle locally; don't clobber their status / priority.
        if existing.kind == "task":
            if not keep_scheduled:
                existing.status = cairn_status
            # Priority left untouched — not imported from Zammad (see above),
            # and a re-sync must never clobber a priority the user set.
            # Record the close channel when Zammad's state closed it, so
            # it's clear the task was completed by the Zammad sync and when.
            if cairn_status == "done":
                existing.completed_via = "zammad"
                if not existing.completed_at:
                    existing.completed_at = datetime.now(timezone.utc)
            else:
                existing.completed_via = None
        # Tag-driven status rules ("Warte auf Kunde → waiting_reply", etc.)
        # run AFTER the Zammad-state assignment so they win: a tag
        # rule is more specific than the generic state mapping. Skipped
        # for a kept-scheduled task so the deferral survives the sync —
        # and skipped when Zammad says the ticket is finished, because a
        # ticket still carrying e.g. "Warte auf Antwort" (→ next) would
        # otherwise be dragged straight back out of done on every sync.
        if not keep_scheduled and cairn_status != "done":
            apply_label_rules(existing, user)
        _sync_article_comments(existing, articles, stats, user, me_id)
        _absorb_images(existing, articles, base, token, zid, stats)
        # Parent stays put either way: the user might have moved the
        # task / project into a specific place after the first import.
        existing.save()
        _import_time_accounting(user, existing, base, token, zid, znum, stats,
                                me_id, raw=raw, opts=opts)
        stats.tickets_updated += 1
        return existing
    else:
        item = Item(
            owner=user,
            kind="task",
            title=title,
            note=note,
            status=cairn_status,
            parent=project,
            external_id=todoist_key,
            external_ref=znum,
            external_owner=str(raw.get("owner_id") or ""),
            source="zammad",
            tags=list(new_external),
            external_tags=list(new_external),
        )
        if cairn_status != "done":       # a closed ticket stays closed
            apply_label_rules(item, user)
        _sync_article_comments(item, articles, stats, user, me_id)
        item.save()
        # After the save, not before: an image is stored ON the item and the
        # body points at it by the item's own id, which a new task does not
        # have until it has been written once.
        if _absorb_images(item, articles, base, token, zid, stats):
            item.save()
        _import_time_accounting(user, item, base, token, zid, znum, stats,
                                me_id, raw=raw, opts=opts)
        stats.tickets_created += 1
        return item


def _ensure_user_has_tag(user, name: str) -> None:
    """Single-name convenience around the bulk helper below."""
    if name:
        _ensure_user_has_tags(user, [name])


def _ensure_user_has_tags(user, names) -> None:
    """Add any tag names from ``names`` that the user doesn't already
    have to ``user.tags`` — keeps imported Zammad tags from rendering
    as bare strings (no colour, no context flag) in the task list."""
    missing = [n for n in (names or []) if n and not user.tag_by_name(n)]
    if not missing:
        return
    user.ensure_tags(missing)
    user.save()


def _fetch_time_accounting(base: str, token: str, ticket_id: str,
                           stats: ZammadStats) -> list[dict]:
    """The ticket's logged time entries, or ``[]``.

    ``GET /tickets/<id>/time_accountings`` needs ``ticket.agent`` (or
    ``admin.time_accounting``) — on a token without it the call 403s, which we
    swallow: time import is an extra, never a reason for the whole sync to
    fail."""
    try:
        rows = _request(base + f"/tickets/{ticket_id}/time_accountings", token)
    except ZammadError as exc:
        stats.errors.append(f"time accounting for {ticket_id}: {exc}")
        return []
    return rows if isinstance(rows, list) else []


def _stamp(value) -> datetime | None:
    """Zammad's ``updated_at`` as an aware datetime, or ``None`` if unreadable.

    Unreadable means "treat it as changed": an unparseable timestamp must never
    be the reason a ticket is skipped."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ticket_minutes(raw: dict):
    """``Ticket#time_unit`` as a number, or ``None`` when Zammad didn't say.

    The cached sum Zammad maintains whenever a booking is created, riding along
    in the ticket payload (``expand=true``) — so "is there anything to fetch" is
    answered for free. ``None`` means the install doesn't carry the field, which
    is a different answer from "no time"; the two callers below want it treated
    differently."""
    if "time_unit" not in raw:
        return None
    try:
        return float(raw.get("time_unit") or 0)
    except (TypeError, ValueError):
        return None


def default_time_from(user=None) -> str:
    """The cutoff a connection starts with when nobody typed one: today.

    An empty cutoff means "book every hour this instance ever recorded", and
    those hours are usually already here from wherever time was kept before
    (mite) — under that system's own ids, which nothing can match against
    Zammad's. So they arrive a second time. A connection therefore goes live
    *today* and only reaches into the past when someone asks it to by naming a
    day."""
    from application.services.tz import user_zone
    return datetime.now(user_zone(user)).date().isoformat()


def _ticket_has_time(raw: dict) -> bool:
    """Worth asking ``/time_accountings`` about? Unknown counts as yes — an
    install that doesn't maintain ``time_unit`` must not silently lose time."""
    minutes = _ticket_minutes(raw)
    return minutes is None or minutes > 0


def _ticket_books_time(raw: dict) -> bool:
    """Does Zammad *positively state* that time is booked on this ticket?

    The strict reading, for the one decision that has to be certain: whether to
    pull in a closed ticket nobody imported. "Unknown" is not a reason to drag
    an instance's entire closed history in."""
    minutes = _ticket_minutes(raw)
    return bool(minutes and minutes > 0)


def closed_ticket_has_time(base: str, token: str, raw: dict,
                           stats: ZammadStats, *, incremental: bool) -> bool:
    """Is this *closed* ticket worth pulling in for the hours on it?

    The one decision that has to be certain, and the one where "Zammad didn't
    say" is a real answer rather than an edge case: ``Ticket#time_unit`` is a
    cached sum, and an install that doesn't maintain it reports nothing at all
    (:func:`_ticket_minutes` → ``None``). Reading that as "no time" loses every
    ticket that opens, gets booked and closes between two runs — silently,
    because the counter it lands in is called ``filtered_closed``.

    So when the sum is unknown we ask outright, but only inside an incremental
    window, where the question is asked about the handful of tickets that
    changed since the last run. On a full pass the strict reading stands: an
    unknown sum is not a reason to walk an instance's entire closed history one
    request at a time — that is the run that times out."""
    minutes = _ticket_minutes(raw)
    if minutes is not None:
        return minutes > 0
    if not incremental:
        return False
    zid = raw.get("id")
    if zid is None:
        return False
    return bool(_fetch_time_accounting(base, token, str(zid), stats))


@dataclass
class TimeOptions:
    """The three decisions the time import needs, and whose timezone reads a
    booking's day. A per-user run takes them from the user, a team run from the
    team — the import itself no longer has to know which kind of run it is in."""

    enabled: bool = True
    auto_customer: bool = False
    since: str = ""          # "YYYY-MM-DD", the day this team went live
    zone: Any = None         # tzinfo the booking's local day is read in
    team: Any = None         # the team to book into (None = the user's default)

    @classmethod
    def for_user(cls, user):
        from application.services.tz import user_zone
        return cls(
            enabled=bool(getattr(user, "zammad_import_time", True)),
            auto_customer=bool(getattr(user, "zammad_time_auto_customer", False)),
            since=(getattr(user, "zammad_time_from", "") or "").strip(),
            zone=user_zone(user),
        )

    @classmethod
    def for_team(cls, team):
        from application.services.tz import user_zone
        return cls(
            enabled=bool(getattr(team, "zammad_import_time", True)),
            auto_customer=bool(getattr(team, "zammad_time_auto_customer", False)),
            since=(getattr(team, "zammad_time_from", "") or "").strip(),
            # A team's hours are read in the deployment's timezone: the same
            # booking must land on the same day for everyone who looks at it.
            zone=user_zone(None),
            team=team,
        )


def _team_for(stats: ZammadStats, team):
    """The one in-memory ``Team`` a run mutates (see ``stats.team_cache``)."""
    if team is None:
        return None
    return stats.team_cache.setdefault(str(team.pk), team)


def _zammad_actor(base: str, token: str, uid, stats: ZammadStats, team,
                  fallback_user: User) -> dict:
    """Zammad user id → the Cairn user the booking belongs to.

    The team's agent roster answers this (see services.zammad_billing): matched
    by email / login / display name once, remembered on the team, and
    overridable by hand when none of those work. An agent with no Cairn account
    keeps their Zammad name as the entry's snapshot and no id — the reports and
    the customer's sheet then still say who did the work, and the mapping UI can
    offer to fix it. Results are cached for the run, so a ticket thread with
    five bookings from two agents costs two lookups.
    """
    from application.services.zammad_billing import actor_for, record_agent

    key = str(uid or "")
    if key in stats.actor_cache:
        return stats.actor_cache[key]

    agent = None
    if key and team is not None:
        known = team.zammad_agent(key)
        raw = {}
        # Only ask Zammad about an agent we don't know yet — a steady sync
        # never calls /users at all.
        if known is None:
            try:
                raw = _request(base + f"/users/{key}", token) or {}
            except ZammadError as exc:
                stats.errors.append(f"zammad user {key}: {exc}")
        agent = record_agent(
            team, zammad_id=key,
            email=(raw.get("email") or "").strip(),
            login=(raw.get("login") or "").strip(),
            name=" ".join(p for p in (raw.get("firstname"),
                                      raw.get("lastname")) if p).strip(),
        )
    actor = actor_for(agent, fallback_user)
    if agent is not None:
        if agent.user:
            stats.time_users_matched += 1
        elif agent.label not in stats.time_users_unmatched:
            stats.time_users_unmatched.append(agent.label)
    stats.actor_cache[key] = actor
    return actor


def _zammad_org(base: str, token: str, raw: dict, stats: ZammadStats
                ) -> tuple[str, str]:
    """``(external_id, name)`` of the customer a ticket should be billed to.

    The ticket's organisation when it has one; otherwise the requesting person,
    who for a solo client *is* the customer. ``("", "")`` when Zammad tells us
    neither."""
    org_id = raw.get("organization_id")
    if org_id:
        ext = f"zammad:org:{org_id}"
        if ext in stats.target_cache:
            return ext, stats.target_cache[ext]
        try:
            org = _request(base + f"/organizations/{org_id}", token) or {}
        except ZammadError as exc:
            stats.errors.append(f"zammad organization {org_id}: {exc}")
            return "", ""
        name = (org.get("name") or "").strip() or f"Zammad org {org_id}"
        stats.target_cache[ext] = name
        return ext, name

    cust_id = raw.get("customer_id")
    if not cust_id:
        return "", ""
    ext = f"zammad:user:{cust_id}"
    if ext in stats.target_cache:
        return ext, stats.target_cache[ext]
    try:
        person = _request(base + f"/users/{cust_id}", token) or {}
    except ZammadError as exc:
        stats.errors.append(f"zammad user {cust_id}: {exc}")
        return "", ""
    name = " ".join(p for p in (person.get("firstname"),
                                person.get("lastname")) if p).strip()
    name = name or person.get("email") or person.get("login") or f"Zammad user {cust_id}"
    stats.target_cache[ext] = name
    return ext, name


def _matching_container(item: "Item", name: str):
    """The task's nearest folder/project called ``name``, if there is one.

    Billing links belong on containers, not on tasks — that is the whole point
    of inheriting the link down the subtree. So the only binding a sync may
    create by itself is the obvious one: the ticket already sits in a folder
    (or project) named exactly like the customer, and that folder is plainly
    meant to be that customer's. Anything less obvious is a decision for a
    person to make on the project page."""
    want = (name or "").strip().casefold()
    if not want:
        return None
    seen = set()
    node = item.parent
    depth = 0
    while node is not None and depth < 50:
        if node.pk in seen:
            break
        seen.add(node.pk)
        if (node.kind in ("project", "folder")
                and (node.title or "").strip().casefold() == want):
            return node
        node = node.parent
        depth += 1
    return None


def _auto_billing_project(user: User, item: "Item", base: str, token: str,
                          raw: dict, stats: ZammadStats, team=None):
    """Billing project for a ticket whose Cairn subtree isn't linked to one.

    Only reached with ``User.zammad_time_auto_customer`` on. The ticket's Zammad
    organisation becomes a Cairn customer + project, and the time books against
    it — but **no link is written onto the ticket's task**. A link on a task is
    a dead end: it binds one ticket, has to be repeated for the next one, and
    turns the tree into a thousand individual bindings instead of a handful of
    folders. The one link this may create is the unambiguous one — the task
    already lives in a folder named exactly like the customer (see
    :func:`_matching_container`), which is the folder someone would have linked
    by hand anyway. Everything else stays a decision for a person, and costs
    nothing: the time still lands, resolved fresh on each sync."""
    from application.services.timetracking import default_team
    from application.services.zammad_billing import ensure_billing_target

    team = _team_for(stats, team or default_team(user))
    if team is None:
        return None
    ext, name = _zammad_org(base, token, raw, stats)
    if not ext:
        return None
    project, created = ensure_billing_target(team, external_id=ext, name=name)
    if project is None:
        return None
    if created:
        stats.time_customers_created += 1
    container = _matching_container(item, project.customer.name)
    if container is not None and container.time_project is None:
        container.time_project = project
        container.save()
        stats.time_folders_linked += 1
    return project


def _import_time_accounting(user: User, item: "Item", base: str, token: str,
                            zid: str, znum: str, stats: ZammadStats,
                            me_id=None, raw: dict | None = None,
                            opts: "TimeOptions | None" = None) -> None:
    """Mirror the time booked on a Zammad ticket into Cairn's time tracking.

    Two gates, in order. ``User.zammad_import_time`` is the master switch — off
    means not even the ``/time_accountings`` call happens. Then the ticket needs
    a billing project: normally the one its Cairn subtree is linked to (that
    link is the opt-in), or — with ``zammad_time_auto_customer`` on — one
    derived from the ticket's Zammad organisation, created on first sight.

    Every agent's booking on the ticket comes across, each attributed to *its
    own* Cairn user via the team's agent roster, so an invoice built from one
    agent's sync isn't short by everyone else's hours. Zammad's ``time_unit`` is
    minutes, and each booking's ``id`` keeps the import idempotent across
    re-syncs."""
    from application.services.timetracking import (
        has_team, repair_imported_entries, resolve_time_project,
        sync_external_entries,
    )
    from application.services.tz import user_zone

    raw = raw or {}
    opts = opts or TimeOptions.for_user(user)
    if not opts.enabled or not (opts.team or has_team(user)):
        return
    auto = opts.auto_customer
    # Earlier syncs stamped the billing project onto the *task*. Tasks are not
    # where a link belongs (see _auto_billing_project), and leaving those behind
    # would keep a thousand one-ticket bindings alive that nobody can maintain —
    # so the sync takes back what it wrote. The time is unaffected: it resolves
    # through the folder, or through the customer's project as before.
    if item.kind == "task" and item.time_project is not None:
        item.time_project = None
        item.save()
        stats.time_task_links_cleared += 1
    project = resolve_time_project(item)
    if project is None and not auto:
        # Counted, not silent: "why is there no time in Cairn" is otherwise a
        # question with no answer on the result page.
        stats.time_skipped_unlinked += 1
        return
    # Zammad keeps the ticket's total accounted time on the ticket itself, so a
    # ticket nobody booked on costs no request at all. This is the difference
    # between one extra API call for the handful of tickets that have time and
    # one for *every* ticket in the instance — the latter is what made a manual
    # import time out at the gateway.
    if not _ticket_has_time(raw):
        stats.time_skipped_no_time += 1
        return
    bookings = _fetch_time_accounting(base, token, zid, stats)
    # The cutoff: hours booked before the day this team went live here belong to
    # whatever system they were kept in. Applied before anything is created, so
    # a ticket whose only bookings are older leaves no trace at all — no
    # customer, no project, no entry.
    zone = opts.zone or user_zone(user)
    cutoff = opts.since
    if cutoff:
        keep = []
        for booking in bookings:
            day = _article_dt(booking).astimezone(zone).date().isoformat()
            if day < cutoff:
                stats.time_skipped_old += 1
                continue
            keep.append(booking)
        bookings = keep
    if project is None:
        if not bookings:
            # Nothing to book → don't invent a customer for this organisation.
            stats.time_skipped_unlinked += 1
            return
        project = _auto_billing_project(user, item, base, token, raw, stats,
                                        team=opts.team)
        if project is None:
            stats.time_skipped_unlinked += 1
            return
    team = _team_for(stats, project.team)
    # The description is the ticket's own title (what the work was about) —
    # the number and its deep link ride along as their own fields, so reports
    # can render "#4711" as a link without parsing the text.
    web_url = base.replace("/api/v1", "") + f"/#ticket/zoom/{zid}"
    ticket_note = (item.title or f"Zammad #{znum}")[:1000]

    # One entry per day and person, not per booking. Four ten-minute replies to
    # the same ticket on the same day are one piece of work on the sheet and on
    # the customer's — the individual bookings are Zammad's bookkeeping, not
    # something anybody wants to read fifteen lines of. The entry's key is the
    # day (``zammad:day:<ticket>:<day>:<agent>``) and it carries the booking ids
    # it sums up, so a re-sync recognises the total instead of adding the parts
    # back beside it.
    groups: dict = {}
    for booking in bookings:
        try:
            minutes = round(float(booking.get("time_unit") or 0))
        except (TypeError, ValueError):
            continue
        if minutes < 1:
            continue
        # Double-booking is not a risk: the dedup is by the Zammad id across
        # the whole team, so whoever syncs first records it and the next sync
        # recognises it.
        actor = _zammad_actor(base, token, booking.get("created_by_id"),
                              stats, team, user)
        # Localise before grouping: the bucket is picked from the *local* day,
        # so a booking made near midnight can't land in the neighbouring day's
        # document — nor in the neighbouring day's total.
        when = _article_dt(booking).astimezone(zone)
        day = when.date().isoformat()
        who = actor["external_user"] or actor["user_id"] or "-"
        key = (day, who)
        row = groups.get(key)
        if row is None:
            row = groups[key] = {
                "external_id": f"zammad:day:{zid}:{day}:{who}",
                "minutes": 0,
                "covered": [],
                # Per booking, for the one case the total can't absorb: the
                # day's entry is already billed, so what it doesn't cover has
                # to be booked on its own.
                "part_minutes": {},
                "spent_on": when,
                "note": ticket_note,
                "external_ref": znum,
                "external_url": web_url,
                "user_id": actor["user_id"],
                "user_name": actor["user_name"],
                "external_user": actor["external_user"],
            }
        row["minutes"] += minutes
        row["covered"].append(f"zammad:{booking.get('id')}")
        row["part_minutes"][f"zammad:{booking.get('id')}"] = minutes
        # The earliest booking of the day dates the entry — a day's work starts
        # when it starts.
        if when < row["spent_on"]:
            row["spent_on"] = when
    rows = list(groups.values())
    if rows:
        added, fixed = sync_external_entries(user=user, item=item, rows=rows,
                                             project=project, team=opts.team)
        stats.time_entries_imported += added
        stats.time_entries_repaired += fixed
    # Entries whose Zammad booking no longer exists are never in ``rows``, so
    # they'd stay stuck with the old bare description and no link.
    stats.time_entries_repaired += repair_imported_entries(
        user=user, item=item, note=ticket_note,
        external_ref=znum, external_url=web_url)


def _fetch_articles(base: str, token: str, ticket_id: str, stats: ZammadStats) -> list[dict]:
    """Return the ticket's article thread (oldest → newest), or ``[]``."""
    try:
        arts = _request(
            base + f"/ticket_articles/by_ticket/{ticket_id}",
            token,
        )
    except ZammadError as exc:
        stats.errors.append(f"articles for {ticket_id}: {exc}")
        return []
    return arts if isinstance(arts, list) else []


def _strip_quoted_history(html: str) -> str:
    """Drop the quoted mail history from a reply, keeping only the new text.

    Zammad's editor marks the boundary before the quoted thread / signature
    with ``<span class="js-signatureMarker">``; inbound clients usually wrap
    the quote in ``<blockquote>``. Cut at whichever appears first so we don't
    re-import the whole prior conversation on every reply.
    """
    cut = len(html)
    i = html.find("js-signatureMarker")
    if i != -1:
        # back up to the start of the element carrying the marker
        start = html.rfind("<", 0, i)
        cut = min(cut, start if start != -1 else i)
    j = html.lower().find("<blockquote")
    if j != -1:
        cut = min(cut, j)
    return html[:cut]


def _article_body(a: dict) -> str:
    """Markdown body of a single article (HTML emails are converted).

    Quoted mail history below a reply is stripped so only the new message
    is imported.
    """
    body = (a.get("body") or "").strip()
    # Zammad articles come back as HTML for incoming email. Notes and
    # comments in Cairn are Markdown, so convert at the boundary.
    if a.get("content_type") == "text/html":
        from application.services.html_to_markdown import html_to_markdown
        body = html_to_markdown(_strip_quoted_history(body))
    return body


def _article_sender(a: dict) -> str:
    """Real sender name of an article, without the trailing ``<email>``."""
    raw = (a.get("from") or a.get("sender") or "").strip()
    if "<" in raw:  # "Ali Bekiroglu <ali@example.com>" → "Ali Bekiroglu"
        raw = raw.split("<", 1)[0].strip().strip('"').strip()
    return raw or "Zammad"


def _article_dt(a: dict) -> datetime:
    """Parse an article's ``created_at``; fall back to now on bad input."""
    val = a.get("created_at")
    if not val:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _sync_article_comments(item: Item, articles: list[dict], stats: ZammadStats,
                           user: User, me_id=None) -> None:
    """Mirror ticket replies (every article after the first) onto the task
    as comments, so updates made in Zammad show up in Cairn.

    The first article is the task note; the rest become comments. Each is
    flagged external. A reply written by the importing user (Zammad
    ``created_by_id == me_id``) is additionally attributed to their Cairn
    account, so it reads as theirs while still showing the external badge;
    everyone else keeps just their name in ``external_author``. Deduped by
    the Zammad article id in ``Comment.external_id`` so a re-sync refreshes
    instead of duplicating. Cairn-written comments (no ``external_id``) are
    left untouched.
    """
    existing = {c.external_id: c for c in (item.comments or []) if c.external_id}
    for a in articles[1:]:
        aid = str(a.get("id") or "")
        if not aid:
            continue
        body = _article_body(a)
        if not body:
            continue
        is_public = not bool(a.get("internal"))
        mine = me_id is not None and a.get("created_by_id") == me_id
        author = user if mine else None
        sender = None if mine else _article_sender(a)
        comment = existing.get(aid)
        if comment:
            comment.body = body
            comment.author = author
            comment.external_author = sender
            comment.external = True
            comment.is_public = is_public
        else:
            item.comments.append(Comment(
                author=author,
                external_author=sender,
                external=True,
                body=body,
                external_id=aid,
                created_at=_article_dt(a),
                is_public=is_public,
            ))
            stats.comments_imported += 1


# --- images out of Zammad ---------------------------------------------------

# Zammad serves an article's files at /ticket_attachment/<ticket>/<article>/<id>;
# an inline image in a body points there, or at "cid:<Content-ID>" from the mail.
_ATTACH_PATH_RE = re.compile(r"ticket_attachment/\d+/\d+/(\d+)")
# ``![alt](src)``, with an optional title — what markdownify made of an <img>.
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s]+)>?(\s+\"[^\"]*\")?\s*\)")
# An image pasted into Zammad's own editor arrives as a base64 blob in the body.
_DATA_URL_RE = re.compile(r"^data:image/[a-z.+-]+;base64,(.+)$", re.I | re.S)
# Tracking pixels, spacer GIFs, the dots in a mail footer: a handful in every
# signature, and none of them a picture anyone wants on a task. A kilobyte is
# the floor because it is safely above those and safely below a screenshot —
# raise it here if signature logos turn out to be noise.
_MIN_IMAGE_BYTES = 1024


def _absorb_images(item: Item, articles: list[dict], base: str, token: str,
                   zid: str, stats: ZammadStats) -> bool:
    """Store a ticket's images ON the task and point its bodies at them.

    An image in an article is a reference Cairn cannot follow: a ``cid:`` left
    over from the mail, a path into Zammad's API that only opens with the
    sync's token, or a base64 blob that would sit in the note text forever.
    Each one is stored on the item exactly as a pasted image is
    (``Item.note_images``), and the body is rewritten to the app's own URL — so
    the picture is in the task, in the export and in the WebDAV mirror, and is
    still there when the ticket has been closed and archived away.

    Idempotent, which is what makes it cheap: an image's token is derived from
    the Zammad ids, so a re-sync recognises what it already has and downloads
    nothing. Only images are taken; other attachments stay in Zammad, which the
    note's back-link reaches.

    Returns True when it changed the item.
    """
    if not articles:
        return False
    # The first article is the note; every later one is the comment
    # ``_sync_article_comments`` just wrote from it.
    holders = {c.external_id: c for c in (item.comments or []) if c.external_id}
    targets = []
    for i, a in enumerate(articles):
        holder = item if i == 0 else holders.get(str(a.get("id") or ""))
        if holder is not None:
            targets.append((a, holder))

    changed = False
    for article, holder in targets:
        field_name = "note" if holder is item else "body"
        text = getattr(holder, field_name) or ""
        # Nothing in the text to repoint and nothing hanging off the article:
        # the overwhelming majority of articles, and not worth a look.
        if "![" not in text and not article.get("attachments"):
            continue
        rewritten = _rewrite_images(item, text, article, base, token, zid, stats)
        if rewritten != text:
            setattr(holder, field_name, rewritten)
            changed = True
    return changed


def _rewrite_images(item: Item, text: str, article: dict, base: str, token: str,
                    zid: str, stats: ZammadStats) -> str:
    """Every ``![](…)`` in one body, pointed at an image stored on the item."""
    atts = [a for a in (article.get("attachments") or []) if isinstance(a, dict)]
    by_id = {str(a.get("id")): a for a in atts if a.get("id")}
    by_cid = {}
    for a in atts:
        cid = str((a.get("preferences") or {}).get("Content-ID") or "").strip("<>")
        if cid:
            by_cid[cid] = a
    aid = str(article.get("id") or "")

    used: set[str] = set()

    def repl(m):
        alt, src, title = m.group(1), m.group(2), m.group(3) or ""
        att = None
        data_url = _DATA_URL_RE.match(src)
        if data_url:
            url = _store_data_url(item, data_url.group(1), stats)
        else:
            if src.lower().startswith("cid:"):
                att = by_cid.get(src[4:].strip("<>"))
            else:
                path = _ATTACH_PATH_RE.search(src)
                att = by_id.get(path.group(1)) if path else None
            url = _store_attachment(item, att, base, token, zid, aid, stats)
        if not url:
            return m.group(0)
        if att is not None:
            used.add(str(att.get("id")))
        return f"![{alt or 'image'}]({url}{title})"

    out = _MD_IMAGE_RE.sub(repl, text)

    # A screenshot is just as often ATTACHED as pasted into the mail — nothing
    # in the body points at it, and dropping it would lose the half of the
    # ticket that says what the problem looks like. Appended in the order
    # Zammad lists them, so a re-sync writes the very same body.
    for att in atts:
        att_id = str(att.get("id") or "")
        if att_id in used or not _looks_like_image(att):
            continue
        url = _store_attachment(item, att, base, token, zid, aid, stats)
        # Already in the text — either appended by an earlier round or pointed
        # at from where the mail had it inline.
        if not url or url in out:
            continue
        name = str(att.get("filename") or "image").replace("]", ")")
        out = _append_image(out, f"![{name}]({url})")
    return out


# The back-link the importer writes under every note. An attached picture goes
# above it: the link is the note's last line by design.
_BACKLINK_RE = re.compile(r"\[Zammad #\d+\]\([^)]*\)\s*$")


def _append_image(text: str, line: str) -> str:
    """An attached image at the end of a body, above the ticket back-link."""
    if not text.strip():
        return line
    m = _BACKLINK_RE.search(text)
    if m:
        return (text[:m.start()].rstrip() + "\n\n" + line
                + "\n\n" + m.group(0).strip())
    return text.rstrip() + "\n\n" + line


def _looks_like_image(att: dict) -> bool:
    """Zammad's own word for it — the mail's Content-Type, else the filename.
    Only a gate on what to DOWNLOAD; the bytes decide in :func:`_keep_image`."""
    prefs = att.get("preferences") or {}
    ctype = str(prefs.get("Content-Type") or prefs.get("Mime-Type") or "").lower()
    if ctype:
        return ctype.startswith("image/") and "svg" not in ctype
    name = str(att.get("filename") or "").lower()
    return name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))


def _store_data_url(item: Item, b64: str, stats: ZammadStats) -> str:
    """A base64 image out of a body, keyed by its own bytes — the same picture
    pasted into two replies is stored once."""
    import base64
    import hashlib

    try:
        data = base64.b64decode("".join(b64.split()))
    except ValueError:            # binascii.Error is one of these
        return ""
    return _keep_image(item, "zm-" + hashlib.sha1(data).hexdigest()[:16], data, stats)


def _store_attachment(item: Item, att, base: str, token: str, zid: str,
                      aid: str, stats: ZammadStats) -> str:
    """One attachment of one article, downloaded only if we don't have it."""
    from application.views.tasks import NOTE_IMAGE_MAX_BYTES

    if not att or not aid:
        return ""
    att_id = str(att.get("id") or "")
    if not att_id:
        return ""
    key = f"zm-{aid}-{att_id}"
    if any(i.token == key for i in (item.note_images or [])):
        return f"/tasks/{item.pk}/note-image/{key}"
    try:
        size = int(att.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if 0 < size < _MIN_IMAGE_BYTES:
        return ""                 # a spacer or a footer dot — don't even fetch it
    if size > NOTE_IMAGE_MAX_BYTES:
        stats.errors.append(
            f"#{zid}: '{att.get('filename')}' is too large to import "
            f"({size // 1024 // 1024} MB) — it stays in Zammad.")
        return ""
    data = _download(base + f"/ticket_attachment/{zid}/{aid}/{att_id}", token, stats)
    if not data:
        return ""
    return _keep_image(item, key, data, stats)


def _keep_image(item: Item, key: str, data: bytes, stats: ZammadStats) -> str:
    """Put the bytes on the item as a note image, if they are one and they fit."""
    from application.models.item import ITEM_MAX_BYTES, NoteImage
    from application.views.tasks import _sniff_image_type

    if any(i.token == key for i in (item.note_images or [])):
        return f"/tasks/{item.pk}/note-image/{key}"
    if len(data) < _MIN_IMAGE_BYTES:
        return ""                 # a spacer or a footer dot, not a picture
    ctype = _sniff_image_type(data[:12])
    if not ctype:
        return ""                 # not a raster image (SVG included, on purpose)
    item.note_images.append(NoteImage(token=key, content_type=ctype, data=data))
    if item.storage_bytes() > ITEM_MAX_BYTES:
        item.note_images.pop()
        stats.errors.append(
            f"{item.title}: no room for another image — the task is full.")
        return ""
    # Counted here, where an image is really taken in: a re-sync repoints the
    # same bodies at the images it already has, and reporting those as imported
    # would make every run look like it did work.
    stats.images_imported += 1
    return f"/tasks/{item.pk}/note-image/{key}"


def _download(url: str, token: str, stats: ZammadStats) -> bytes:
    """Raw bytes from Zammad. :func:`_request` speaks JSON; an attachment is a
    file, and the other difference is that a lost one is a missing picture, not
    a failed sync — so it is logged and the body keeps the text it had."""
    from application.services.http_safety import (
        UnsafeURLError, safe_urlopen, validate_outbound_url,
    )
    from application.views.tasks import NOTE_IMAGE_MAX_BYTES

    next(_CALLS)
    try:
        validate_outbound_url(url)
    except UnsafeURLError as exc:
        stats.errors.append(f"refusing to fetch: {exc}")
        return b""
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Token token={token}",
        "Accept": "*/*",
    })
    try:
        with safe_urlopen(req, timeout=30) as resp:
            return resp.read(NOTE_IMAGE_MAX_BYTES + 1)
    except (UnsafeURLError, urllib.error.URLError, OSError) as exc:
        stats.errors.append(f"attachment {url.rsplit('/', 1)[-1]}: {exc}")
        return b""
