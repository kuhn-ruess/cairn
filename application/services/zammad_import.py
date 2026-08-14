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
* Priority "1 low"/"2 normal"/"3 high" → Cairn P4/P3/P2
* State "closed"/"merged"/"pending close" → ``status=done`` with
  ``completed_at`` stamped; every other state → ``status=open`` (a
  ticket that leaves "pending close" again reopens its Cairn task).
* ``ticket.url`` (if Zammad exposes one) is appended to the note as a
  back-link.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    me_id: Any = None
    fetched_via: str = ""
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
            "me_id": self.me_id,
            "fetched_via": self.fetched_via,
            "errors": list(self.errors),
            "trace": list(self.trace),
        }


def _request(url: str, token: str, *, params: dict | None = None,
             method: str = "GET", body: dict | None = None) -> Any:
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
                chunk = _request(
                    base + "/tickets/search",
                    token,
                    params={
                        "query": f"owner_id:{me_id}",
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
                if not already:
                    stats.filtered_closed += 1
                    continue
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
    if not dry_run:
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

    # A tag is Zammad-owned (so a sync may drop/replace it) if it's under the
    # "Zammad" group OR it was in this task's previous external_tags — i.e. a
    # value Zammad itself set last sync. The group alone wasn't enough: an old
    # workflow label that was never parented under "Zammad" (older imports,
    # manual creates, the ungrouped _ensure_user_has_tags path) wasn't
    # recognised as owned, so the live value got *appended* and the stale one
    # lingered — looking like "the Zammad tag never overwrote the old one".
    # external_tags is the authoritative per-task record of Zammad's own tags,
    # so fold it in. User-added tags (never grouped, never in external_tags)
    # still survive untouched. A failed tag fetch (``tags_fetched`` False)
    # leaves tags as they are rather than wiping the group on a transient error.
    zammad_owned = {t.name for t in (user.tags or [])
                    if t.parent == "Zammad" or t.name == "Zammad"}
    old_tags = list(existing.tags or []) if existing else []
    owned = zammad_owned | (set(existing.external_tags or []) if existing else set())
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
        # Parent stays put either way: the user might have moved the
        # task / project into a specific place after the first import.
        existing.save()
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
            source="zammad",
            tags=list(new_external),
            external_tags=list(new_external),
        )
        if cairn_status != "done":       # a closed ticket stays closed
            apply_label_rules(item, user)
        _sync_article_comments(item, articles, stats, user, me_id)
        item.save()
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
