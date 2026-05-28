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
* Ticket article body (the first one) → ``note``
* Priority "1 low"/"2 normal"/"3 high" → Cairn P4/P3/P2
* State "open"/"new"/"pending close" → ``status=next``;
  "closed"/"merged" → ``status=done`` with ``completed_at`` stamped;
  other states stay ``open``.
* ``ticket.url`` (if Zammad exposes one) is appended to the note as a
  back-link.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from application.models.item import Item
from application.models.user import User

log = logging.getLogger(__name__)


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
    me_id: Any = None
    fetched_via: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tickets_created": self.tickets_created,
            "tickets_updated": self.tickets_updated,
            "tickets_skipped": self.tickets_skipped,
            "tickets_fetched": self.tickets_fetched,
            "filtered_assignee": self.filtered_assignee,
            "filtered_closed": self.filtered_closed,
            "tickets_dropped": self.tickets_dropped,
            "me_id": self.me_id,
            "fetched_via": self.fetched_via,
            "errors": list(self.errors),
        }


def _request(url: str, token: str, *, params: dict | None = None,
             method: str = "GET", body: dict | None = None) -> Any:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = None
    headers = {
        "Authorization": f"Token token={token}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise ZammadError(f"HTTP {exc.code} from {url}: {exc.read().decode('utf-8', errors='replace')[:300]}")
    except urllib.error.URLError as exc:
        raise ZammadError(f"network error: {exc.reason}")
    except json.JSONDecodeError as exc:
        raise ZammadError(f"unparseable JSON from {url}: {exc}")


_PRIORITY_MAP = {
    "1 low": 4, "2 normal": 3, "3 high": 2,
    "1": 4, "2": 3, "3": 2,
}
_DONE_STATES = {"closed", "merged"}
_OPEN_STATES = {"open", "new", "pending close", "pending reminder"}


def import_for_user(
    user: User,
    url: str,
    token: str,
    *,
    project: Item | None = None,
    assignee_only: bool = True,
    include_closed: bool = False,
    dry_run: bool = False,
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

    closed_states = {"closed", "merged"}
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
            if state in closed_states:
                stats.filtered_closed += 1
                continue
        filtered.append(raw)
    tickets = filtered

    for raw in tickets:
        try:
            _upsert_ticket(user, raw, base, token, project, stats, dry_run)
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
            it.status = "done"
            it.save()  # Item.save stamps completed_at automatically
            stats.tickets_dropped += 1

    if not dry_run:
        user.last_zammad_sync_at = datetime.now(timezone.utc)
        user.save()
    return stats


def _upsert_ticket(
    user: User,
    raw: dict,
    base: str,
    token: str,
    project: Item | None,
    stats: ZammadStats,
    dry_run: bool,
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
    priority = _PRIORITY_MAP.get((raw.get("priority") or "").lower())
    state = (raw.get("state") or "").lower()
    if state in _DONE_STATES:
        cairn_status = "done"
    elif state in _OPEN_STATES:
        cairn_status = "next"
    else:
        cairn_status = "open"

    note_parts = []
    article = _first_article(base, token, zid, stats)
    if article:
        note_parts.append(article)
    # Back-link to the Zammad ticket itself for context.
    web_url = base.replace("/api/v1", "") + f"/#ticket/zoom/{zid}"
    note_parts.append(f"[Zammad #{zid}]({web_url})")
    note = "\n\n".join(note_parts)

    if dry_run:
        if existing:
            stats.tickets_updated += 1
        else:
            stats.tickets_created += 1
        return None

    # Mirror Zammad tags onto the Cairn task. The Zammad side is
    # authoritative for tags it owns — they live in ``external_tags``
    # and get replaced wholesale on each sync. Anything the user added
    # in Cairn (= tags present on the task but not in the old
    # external_tags) is preserved untouched.
    #
    # Zammad's /tickets responses don't carry tags (they live in a
    # separate object). We fetch them per ticket via /tags?object=
    # Ticket&o_id=<id>. The endpoint returns ``{"tags": [...]}``.
    new_external: list[str] = []
    try:
        tag_resp = _request(
            base + "/tags", token,
            params={"object": "Ticket", "o_id": zid},
        )
        z_tags = (tag_resp or {}).get("tags") if isinstance(tag_resp, dict) else []
        if isinstance(z_tags, str):
            z_tags = [t.strip() for t in z_tags.split(",") if t.strip()]
        new_external = [t for t in (z_tags or []) if t]
    except ZammadError as exc:
        stats.errors.append(f"tags for {zid}: {exc}")
    if new_external:
        from application.services.external_tags import ensure_user_has_tags
        ensure_user_has_tags(user, new_external, group="Zammad")

    from application.services.external_tags import merge_external_tags

    if existing:
        existing.title = title
        existing.note = note
        existing.source = "zammad"
        existing.external_ref = znum
        existing.tags = merge_external_tags(
            existing.tags, existing.external_tags, new_external)
        existing.external_tags = new_external
        # Task vs project have completely different status vocabularies
        # (open/next/waiting vs active/on_hold/completed). The
        # ``cairn_status`` we compute from Zammad's state only fits
        # tasks. For a project the user has clearly chosen to manage
        # the lifecycle locally; don't clobber their status / priority.
        if existing.kind == "task":
            existing.status = cairn_status
            if priority is not None:
                existing.priority = priority
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
            priority=priority,
            parent=project,
            external_id=todoist_key,
            external_ref=znum,
            source="zammad",
            tags=list(new_external),
            external_tags=list(new_external),
        )
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


def _first_article(base: str, token: str, ticket_id: str, stats: ZammadStats) -> str:
    """Pull the ticket's first article (the original message) for the note."""
    try:
        arts = _request(
            base + f"/ticket_articles/by_ticket/{ticket_id}",
            token,
        )
    except ZammadError as exc:
        stats.errors.append(f"articles for {ticket_id}: {exc}")
        return ""
    if not isinstance(arts, list) or not arts:
        return ""
    a = arts[0]
    body = (a.get("body") or "").strip()
    # Zammad articles come back as HTML for incoming email. Notes in
    # Cairn are Markdown, so convert at the boundary.
    if a.get("content_type") == "text/html":
        from application.services.html_to_markdown import html_to_markdown
        body = html_to_markdown(body)
    return body
