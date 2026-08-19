"""Import an existing mite.de account into the time-tracking module.

Cairn's time tracking is a mite replacement, so this is the migration path off
mite: point it at an account, and its customers, projects and the whole time
history land in the team's billing data.

Authentication is mite's API key (mite → "Mein Konto" → API-Schlüssel), sent as
``X-MiteApiKey`` against ``https://<account>.mite.de``.

Mapping
-------
* mite customer     → :class:`~application.models.timetracking.Customer`
* mite project      → :class:`TimeTrackingProject` — the rate (``hourly_rate``,
  cents) and budget carry over unchanged; mite's ``budget_type`` vocabulary is
  identical to ours (``minutes`` / ``cents`` / ``*_per_month``).
* mite service      → kept as the entry's ``service`` label. Cairn has no
  service entity, and inventing one for imported history would be a bigger
  change than the data justifies — the name is preserved so reports still show
  *what* was worked on.
* mite time entry   → a :class:`TimeEntry` in the ``(team, customer, day)``
  bucket. ``billable``, ``locked`` (= billed), the frozen hourly rate and the
  revenue all carry over, so imported history bills exactly as it did in mite.
* mite user         → matched to a Cairn user by email, so each entry keeps its
  author. Unmatched authors keep their mite name as the snapshot and simply
  have no Cairn account behind them, which every read path already tolerates.

Idempotency
-----------
Customers and projects carry ``external_id = "mite:<id>"``; entries carry it on
``TimeEntry.external_id``. Re-running updates names/rates and appends only time
entries that aren't there yet, so an import can be repeated (e.g. a first run
for the history, a second one right before the switch-over).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from application.models.timetracking import (
    MAX_ENTRIES_PER_BUCKET,
    Customer,
    TimeBucket,
    TimeEntry,
    TimeTrackingProject,
)
from application.models.user import User

# mite's own limit for a page of results. Asking for more is not an error, but
# the account gets slower for everyone — page instead.
PAGE_SIZE = 1000

_ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class MiteError(RuntimeError):
    """Any HTTP/parse failure, with the urllib stack swallowed."""


@dataclass
class MiteStats:
    customers_created: int = 0
    customers_updated: int = 0
    projects_created: int = 0
    projects_updated: int = 0
    entries_imported: int = 0
    entries_skipped: int = 0  # already imported by an earlier run
    entries_fetched: int = 0
    users_matched: int = 0
    unmatched_users: list = field(default_factory=list)
    dry_run: bool = False
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "customers_created": self.customers_created,
            "customers_updated": self.customers_updated,
            "projects_created": self.projects_created,
            "projects_updated": self.projects_updated,
            "entries_imported": self.entries_imported,
            "entries_skipped": self.entries_skipped,
            "entries_fetched": self.entries_fetched,
            "users_matched": self.users_matched,
            "unmatched_users": list(self.unmatched_users),
            "dry_run": self.dry_run,
            "errors": list(self.errors),
        }


def normalise_account(raw: str) -> str:
    """``"https://acme.mite.de/"`` / ``"acme.mite.de"`` / ``"acme"`` → ``"acme"``.

    The account name becomes a hostname, so it is validated as a plain slug —
    anything else could aim an authenticated request somewhere unintended."""
    name = (raw or "").strip().lower()
    name = re.sub(r"^https?://", "", name)
    name = name.split("/")[0]
    if name.endswith(".mite.de"):
        name = name[: -len(".mite.de")]
    if not _ACCOUNT_RE.match(name):
        raise MiteError(f"'{raw}' is not a valid mite account name")
    return name


class MiteClient:
    """Thin read-only client. Every response element is wrapped by mite in its
    type (``{"project": {...}}``); :meth:`get` unwraps that away."""

    def __init__(self, account: str, api_key: str):
        self.base = f"https://{normalise_account(account)}.mite.de"
        self.api_key = (api_key or "").strip()
        if not self.api_key:
            raise MiteError("a mite API key is required")

    def _request(self, path: str, params: dict | None = None) -> Any:
        from application.services.http_safety import (
            UnsafeURLError, safe_urlopen, validate_outbound_url,
        )
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            validate_outbound_url(url)
        except UnsafeURLError as exc:
            raise MiteError(f"refusing to fetch: {exc}") from exc
        req = urllib.request.Request(url, headers={
            "X-MiteApiKey": self.api_key,
            "Accept": "application/json",
            "User-Agent": "Cairn/mite-import",
        })
        try:
            with safe_urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise MiteError("mite rejected the API key (401/403)") from exc
            if exc.code == 404:
                raise MiteError(f"mite has no {path} (404) — check the account name") from exc
            raise MiteError(f"mite {path}: HTTP {exc.code}") from exc
        except OSError as exc:
            raise MiteError(f"mite {path}: {exc}") from exc
        if not raw:
            return []
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise MiteError(f"mite {path}: invalid JSON") from exc

    def get(self, path: str, params: dict | None = None) -> list[dict]:
        """A list endpoint, unwrapped: ``[{"project": {…}}]`` → ``[{…}]``."""
        data = self._request(path, params)
        if isinstance(data, dict):
            data = [data]
        out = []
        for row in data or []:
            if isinstance(row, dict) and len(row) == 1:
                inner = next(iter(row.values()))
                out.append(inner if isinstance(inner, dict) else row)
            elif isinstance(row, dict):
                out.append(row)
        return out

    def paginate(self, path: str, params: dict | None = None):
        """Yield every row of a list endpoint, a page at a time."""
        page = 1
        while True:
            rows = self.get(path, {**(params or {}), "limit": PAGE_SIZE, "page": page})
            if not rows:
                return
            yield from rows
            if len(rows) < PAGE_SIZE:
                return
            page += 1


def _cents(value) -> int | None:
    """mite money is integer cents; be forgiving about floats/strings/None."""
    if value in (None, ""):
        return None
    try:
        return max(0, round(float(value)))
    except (TypeError, ValueError):
        return None


def _spent_on(date_str: str, zone) -> datetime | None:
    """mite's ``date_at`` (a plain day) → noon in the importing user's zone.

    Noon, like every other write path here: it keeps the entry on its own day
    no matter which timezone later reads it."""
    try:
        d = date.fromisoformat((date_str or "").strip())
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=zone)


def _match_users(client: MiteClient, stats: MiteStats) -> dict:
    """mite user id → ``(cairn_user_id_or_empty, display_name)``.

    Matching is by email: it is the one identifier both systems agree on.
    Nobody is created — an unmatched author keeps their mite name as the
    entry's snapshot, which is all the reports ever need."""
    out = {}
    rows = list(client.get("/users.json"))
    try:
        rows += list(client.get("/users/archived.json"))
    except MiteError as exc:  # archived users are a nice-to-have
        stats.errors.append(f"archived users: {exc}")
    emails = {(r.get("email") or "").strip().lower(): r for r in rows if r.get("email")}
    found = {}
    if emails:
        for u in User.objects(email__in=list(emails)).only("id", "email",
                                                           "display_name", "username"):
            found[(u.email or "").lower()] = u
    for r in rows:
        mail = (r.get("email") or "").strip().lower()
        cairn = found.get(mail)
        name = (r.get("name") or mail or "").strip()
        if cairn is not None:
            out[str(r.get("id"))] = (str(cairn.id),
                                     cairn.display_name or cairn.username or name)
            stats.users_matched += 1
        else:
            out[str(r.get("id"))] = ("", name)
            if name:
                stats.unmatched_users.append(name)
    return out


def _import_customers(client, team, stats, dry_run) -> dict:
    """mite customer id → Cairn :class:`Customer` (created or refreshed)."""
    rows = list(client.get("/customers.json"))
    try:
        rows += list(client.get("/customers/archived.json"))
    except MiteError as exc:
        stats.errors.append(f"archived customers: {exc}")

    existing = {c.external_id: c for c in Customer.objects(team=team)
                if c.external_id}
    out = {}
    for r in rows:
        ext = f"mite:{r.get('id')}"
        name = (r.get("name") or "").strip()[:200] or f"mite customer {r.get('id')}"
        cust = existing.get(ext)
        if cust is None:
            # Built even on a dry run (just never saved), so the projects and
            # entries below still resolve and the preview reports real numbers.
            cust = Customer(team=team, name=name, external_id=ext)
            stats.customers_created += 1
        else:
            stats.customers_updated += 1
        cust.name = name
        cust.note = (r.get("note") or "").strip()
        cust.archived = bool(r.get("archived"))
        if not dry_run:
            cust.save()
        out[str(r.get("id"))] = cust
    return out


def _import_projects(client, team, customers, stats, dry_run) -> dict:
    """mite project id → Cairn :class:`TimeTrackingProject`."""
    rows = list(client.get("/projects.json"))
    try:
        rows += list(client.get("/projects/archived.json"))
    except MiteError as exc:
        stats.errors.append(f"archived projects: {exc}")

    existing = {p.external_id: p for p in TimeTrackingProject.objects(team=team)
                if p.external_id}
    valid_budget = ("minutes", "cents", "minutes_per_month", "cents_per_month")
    out = {}
    for r in rows:
        cust = customers.get(str(r.get("customer_id")))
        if cust is None:
            stats.errors.append(
                f"project '{r.get('name')}' skipped — its customer is not in the import")
            continue
        ext = f"mite:{r.get('id')}"
        name = (r.get("name") or "").strip()[:200] or f"mite project {r.get('id')}"
        proj = existing.get(ext)
        if proj is None:
            proj = TimeTrackingProject(team=team, customer=cust, name=name,
                                       external_id=ext)
            stats.projects_created += 1
        else:
            stats.projects_updated += 1
        proj.customer = cust
        proj.name = name
        proj.note = (r.get("note") or "").strip()
        proj.hourly_rate_cents = _cents(r.get("hourly_rate"))
        budget = r.get("budget") or 0
        btype = r.get("budget_type")
        # mite's budget vocabulary is ours; anything unexpected → no budget
        # rather than a wrong one.
        proj.budget_value = budget if (budget and btype in valid_budget) else None
        proj.budget_type = btype if proj.budget_value else None
        proj.archived = bool(r.get("archived"))
        if not dry_run:
            proj.save()
        out[str(r.get("id"))] = proj
    return out


def _known_entry_ids(team) -> set:
    """Every ``mite:*`` id already imported into this team — the dedup set.

    One pass over the team's buckets. It is read once per import rather than
    per entry, so a re-run of a multi-year history costs a single scan."""
    known = set()
    for b in TimeBucket.objects(team=team).only("entries"):
        for e in b.entries:
            if e.external_id and e.external_id.startswith("mite:"):
                known.add(e.external_id)
    return known


def _write_day(team, customer, period: str, entries: list) -> None:
    """Append a day's imported entries to ``(team, customer, day)``.

    A fresh day is inserted whole (one write per day, chunked into parts if the
    day is enormous) instead of one atomic push per entry — an import writes
    thousands of entries and nobody else is touching those buckets yet. A day
    that already has a bucket falls back to the normal atomic append, so a
    re-run can never clobber what is already there."""
    from application.services.timetracking import _target_part, _utcnow

    if not entries:
        return
    if TimeBucket.objects(team=team, customer=customer, period=period).count():
        for e in entries:
            TimeBucket.objects(
                team=team, customer=customer, period=period,
                part=_target_part(team, customer, period),
            ).update_one(
                upsert=True, push__entries=e,
                inc__total_minutes=(e.minutes or 0) if e.status == "active" else 0,
                inc__entry_count=1, set__updated_at=_utcnow(),
                set_on_insert__created_at=_utcnow(),
            )
        return
    for i in range(0, len(entries), MAX_ENTRIES_PER_BUCKET):
        chunk = entries[i:i + MAX_ENTRIES_PER_BUCKET]
        TimeBucket(
            team=team, customer=customer, period=period,
            part=i // MAX_ENTRIES_PER_BUCKET + 1,
            entries=chunk,
            entry_count=len(chunk),
            total_minutes=sum(e.minutes or 0 for e in chunk),
        ).save()


def import_for_user(user, *, account: str, api_key: str, team,
                    date_from: date | None = None, date_to: date | None = None,
                    dry_run: bool = False) -> MiteStats:
    """Import a mite account into ``team``. Returns what happened.

    ``date_from`` / ``date_to`` bound the time entries (customers and projects
    always come across in full — they are the structure the entries hang off).
    ``dry_run`` touches nothing and reports what a real run would do."""
    from application.services.tz import user_zone

    stats = MiteStats(dry_run=dry_run)
    client = MiteClient(account, api_key)
    zone = user_zone(user)

    users = _match_users(client, stats)
    customers = _import_customers(client, team, stats, dry_run)
    projects = _import_projects(client, team, customers, stats, dry_run)

    params: dict = {}
    if date_from:
        params["from"] = date_from.isoformat()
        params["to"] = (date_to or date.today()).isoformat()
    elif date_to:
        params["from"] = "2000-01-01"
        params["to"] = date_to.isoformat()

    # Read even on a dry run: "already imported" is the single most useful
    # thing a preview can tell you before a re-import.
    known = _known_entry_ids(team)
    # Entries are grouped by their (customer, day) bucket before writing, which
    # is also mite's own sort order — so the grouping stays small in memory even
    # for a multi-year account.
    pending: dict = {}

    def flush():
        if dry_run:
            pending.clear()
            return
        for (cust, period), rows in pending.items():
            _write_day(team, cust, period, rows)
        pending.clear()

    for raw in client.paginate("/time_entries.json", params):
        stats.entries_fetched += 1
        ext = f"mite:{raw.get('id')}"
        if ext in known:
            stats.entries_skipped += 1
            continue
        proj = projects.get(str(raw.get("project_id")))
        if proj is None:
            # mite allows time without a project; Cairn always bills to one.
            stats.errors.append(
                f"entry {raw.get('id')} ({raw.get('date_at')}) skipped — no project")
            continue
        when = _spent_on(raw.get("date_at"), zone)
        minutes = int(raw.get("minutes") or 0)
        if when is None or minutes < 1:
            stats.entries_skipped += 1
            continue
        uid, uname = users.get(str(raw.get("user_id")), ("", raw.get("user_name") or ""))
        billable = bool(raw.get("billable"))
        rate = _cents(raw.get("hourly_rate"))
        revenue = _cents(raw.get("revenue"))
        entry = TimeEntry(
            project_id=str(proj.id) if proj.id else "",
            project_name=proj.name,
            user_id=uid or None,
            user_name=uname,
            minutes=minutes,
            spent_on=when,
            note=(raw.get("note") or "")[:1000],
            service=(raw.get("service_name") or "")[:200],
            hourly_rate_cents=rate,
            amount_cents=revenue if billable else None,
            billable=billable,
            locked=bool(raw.get("locked")),
            external_id=ext,
        )
        known.add(ext)
        stats.entries_imported += 1
        pending.setdefault((proj.customer, when.strftime("%Y-%m-%d")), []).append(entry)
        if len(pending) > 200:
            flush()
    flush()
    return stats
