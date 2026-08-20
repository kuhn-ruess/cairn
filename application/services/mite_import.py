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

Scope
-----
A run can bring over the structure only (``with_entries=False``) and limit
itself to what is still active in mite (``include_archived=False``) — the usual
"set Cairn up from my mite account, keep the history where it is" move. Because
everything is matched by mite id, that run can be repeated, or later widened to
a full import, without creating anything twice.

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
from datetime import date, datetime, timezone
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
    # Marked archived by this run (from mite's archived lists) — reported
    # because "where did my projects go" and "why are they all back" are both
    # questions this number answers.
    customers_archived: int = 0
    projects_archived: int = 0
    entries_imported: int = 0
    entries_skipped: int = 0  # already imported by an earlier run
    # mite lets an entry exist with no time on it at all (a started-and-never-
    # filled row, a leftover timer). Those are not "already there" and counting
    # them as such made a fresh import claim it had seen hundreds of entries
    # before — they are simply nothing to import.
    entries_empty: int = 0
    entries_undated: int = 0  # a date mite gave us that we could not read
    entries_updated: int = 0  # changed in mite after we imported them
    entries_frozen: int = 0   # changed in mite, but already billed here
    entries_fetched: int = 0
    users_matched: int = 0
    unmatched_users: list = field(default_factory=list)
    dry_run: bool = False
    # What the run was asked to do — echoed back so the caller's summary can
    # say "no time entries were requested" instead of implying there were none.
    with_entries: bool = True
    include_archived: bool = True
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "customers_created": self.customers_created,
            "customers_updated": self.customers_updated,
            "projects_created": self.projects_created,
            "projects_updated": self.projects_updated,
            "customers_archived": self.customers_archived,
            "projects_archived": self.projects_archived,
            "entries_imported": self.entries_imported,
            "entries_skipped": self.entries_skipped,
            "entries_empty": self.entries_empty,
            "entries_undated": self.entries_undated,
            "entries_updated": self.entries_updated,
            "entries_frozen": self.entries_frozen,
            "entries_fetched": self.entries_fetched,
            "users_matched": self.users_matched,
            "unmatched_users": list(self.unmatched_users),
            "dry_run": self.dry_run,
            "with_entries": self.with_entries,
            "include_archived": self.include_archived,
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


# "(10:00 bis 12:00) Abstimmungs-Calls" — mite has no start/end fields, so the
# times of day live in the note by convention. Cairn *does* have the fields, and
# the customer's sheet has a column for them, so the convention is decoded on
# the way in: accepted at the start of the note, in brackets or parens, with
# "bis" / "to" / "until" / a dash between them, and hours written 10:00 or 10.00.
_CLOCK_RE = re.compile(
    r"^\s*[\(\[]\s*(\d{1,2})[:.](\d{2})\s*(?:bis|to|until|-|–|—|until)\s*"
    r"(\d{1,2})[:.](\d{2})\s*[\)\]]\s*[-–—:]?\s*",
    re.IGNORECASE)


def split_clock(note: str) -> tuple[str, str, str]:
    """``"(10:00 bis 12:00) Call"`` → ``("10:00", "12:00", "Call")``.

    Returns ``("", "", note)`` when the note doesn't open with a time range —
    which is most of them. The times are taken out of the text because Cairn
    shows them in their own column; leaving them in would print every entry's
    times twice, and the note is what the customer reads."""
    m = _CLOCK_RE.match(note or "")
    if not m:
        return "", "", (note or "")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return "", "", (note or "")
    return f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}", (note or "")[m.end():].strip()


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


def _match_users(client: MiteClient, stats: MiteStats, team=None) -> dict:
    """mite user id → ``(cairn_user_id_or_empty, display_name)``.

    Email first — the one identifier both systems agree on — then, *within the
    team*, the display name. Email alone leaves everyone who signed up here
    under a different address unattributed: their hours then show their mite
    name in every report and belong to no account, so the report's user filter
    can never find them and their own day sheet never shows them. Nobody is
    created; an unmatched author still keeps their mite name as the entry's
    snapshot, which is all a report strictly needs."""
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
    # Display names of the team, for the second pass.
    by_name = {}
    if team is not None:
        from application.services.permissions import team_members
        for m in team_members(team):
            for key in ((m.display_name or "").strip().lower(),
                        (m.username or "").strip().lower()):
                if key:
                    by_name.setdefault(key, m)

    for r in rows:
        mail = (r.get("email") or "").strip().lower()
        cairn = found.get(mail)
        name = (r.get("name") or mail or "").strip()
        if cairn is None:
            cairn = by_name.get(name.lower())
        if cairn is not None:
            out[str(r.get("id"))] = (str(cairn.id),
                                     cairn.display_name or cairn.username or name)
            stats.users_matched += 1
        else:
            out[str(r.get("id"))] = ("", name)
            if name:
                stats.unmatched_users.append(name)
    return out


def _import_customers(client, team, stats, dry_run, include_archived=True) -> dict:
    """mite customer id → Cairn :class:`Customer` (created or refreshed).

    Which list a row came from decides ``archived``, not the row's own flag: a
    customer fetched from ``/customers/archived.json`` is archived by
    definition."""
    rows = [(r, False) for r in client.get("/customers.json")]
    if include_archived:
        try:
            rows += [(r, True)
                     for r in client.get("/customers/archived.json")]
        except MiteError as exc:
            stats.errors.append(f"archived customers: {exc}")

    # Two indexes over the team's customers: by every key they are known by,
    # and by name. The second one is what lets a re-import *supplement* what is
    # already here — a customer Zammad created under its organisation key is the
    # same client, and mite must adopt it rather than open a second file on it.
    known = list(Customer.objects(team=team))
    existing, by_name = {}, {}
    for c in known:
        for key in [c.external_id] + list(c.external_ids or []):
            if key:
                existing[key] = c
        by_name.setdefault((c.name or "").strip().lower(), c)

    out = {}
    for r, from_archive in rows:
        ext = f"mite:{r.get('id')}"
        name = (r.get("name") or "").strip()[:200] or f"mite customer {r.get('id')}"
        cust = existing.get(ext)
        if cust is None:
            twin = by_name.get(name.strip().lower())
            if twin is not None:
                # Adopt it, and remember the mite key on it so the next run goes
                # straight there.
                cust = twin
                if not cust.external_id:
                    cust.external_id = ext
                else:
                    cust.external_ids = list(cust.external_ids or []) + [ext]
                existing[ext] = cust
        if cust is None:
            # Built even on a dry run (just never saved), so the projects and
            # entries below still resolve and the preview reports real numbers.
            cust = Customer(team=team, name=name, external_id=ext)
            stats.customers_created += 1
        else:
            stats.customers_updated += 1
        cust.name = name
        cust.note = (r.get("note") or "").strip()
        archived = from_archive or bool(r.get("archived"))
        if archived and not cust.archived:
            stats.customers_archived += 1
        cust.archived = archived
        if not dry_run:
            cust.save()
        out[str(r.get("id"))] = cust
    return out


def _import_projects(client, team, customers, stats, dry_run,
                     include_archived=True) -> dict:
    """mite project id → Cairn :class:`TimeTrackingProject`.

    With ``include_archived=False`` the archived ones are simply not fetched:
    anything already imported keeps whatever state it has here (nothing is
    removed or un-archived), so widening to a full import later still lands
    correctly.

    mite stays authoritative for ``archived``, and *which list a row came from*
    is what decides it — a project fetched from ``/projects/archived.json`` is
    archived whether or not the row repeats the flag."""
    rows = [(r, False) for r in client.get("/projects.json")]
    if include_archived:
        try:
            rows += [(r, True) for r in client.get("/projects/archived.json")]
        except MiteError as exc:
            stats.errors.append(f"archived projects: {exc}")

    known = list(TimeTrackingProject.objects(team=team))
    existing, by_name = {}, {}
    for p in known:
        for key in [p.external_id] + list(p.external_ids or []):
            if key:
                existing[key] = p
        # Same-name-under-the-same-customer is the identity a person would use;
        # see _import_customers for why adopting beats duplicating.
        by_name.setdefault((str(p.customer.id), (p.name or "").strip().lower()), p)

    valid_budget = ("minutes", "cents", "minutes_per_month", "cents_per_month")
    out = {}
    for r, from_archive in rows:
        cust = customers.get(str(r.get("customer_id")))
        if cust is None:
            stats.errors.append(
                f"project '{r.get('name')}' skipped — its customer is not in the import")
            continue
        ext = f"mite:{r.get('id')}"
        name = (r.get("name") or "").strip()[:200] or f"mite project {r.get('id')}"
        proj = existing.get(ext)
        if proj is None and cust.id:
            twin = by_name.get((str(cust.id), name.strip().lower()))
            if twin is not None:
                proj = twin
                if not proj.external_id:
                    proj.external_id = ext
                else:
                    proj.external_ids = list(proj.external_ids or []) + [ext]
                existing[ext] = proj
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
        archived = from_archive or bool(r.get("archived"))
        if archived and not proj.archived:
            stats.projects_archived += 1
        proj.archived = archived
        if not dry_run:
            proj.save()
        out[str(r.get("id"))] = proj
    return out


def _known_entries(team) -> dict:
    """Every ``mite:*`` entry already in this team, keyed by its mite id.

    One pass over the team's buckets, read once per import rather than per
    entry, so a re-run of a multi-year history costs a single scan. It carries
    enough of each entry to answer the only question that matters on a re-run:
    *has this changed in mite since we took it?* — which is what turns the
    import from "skip what I have" into "keep what I have correct"."""
    known = {}
    for b in TimeBucket.objects(team=team).only("id", "customer", "period",
                                                "entries"):
        for e in b.entries:
            if not (e.external_id or "").startswith("mite:"):
                continue
            known[e.external_id] = {
                "bucket_pk": b.pk,
                "period": b.period,
                "customer_id": str(b.customer.id) if b.customer else "",
                "entry_id": e.entry_id,
                "entry": e,
            }
    return known


# What mite owns on an entry it gave us. Compared field by field on a re-run;
# anything that differs is mite's newer truth and is written back — an entry
# corrected in mite the day after an import is otherwise silently wrong here
# forever.
def _entry_corrections(entry, want: dict) -> dict:
    changes = {}
    for field, value in want.items():
        if getattr(entry, field, None) != value:
            changes[f"set__entries__S__{field}"] = value
    return changes


def _drop_entry(hit: dict) -> None:
    """Pull one entry out of its bucket — for an entry whose day or customer
    changed in mite, which means it belongs in a different bucket document
    entirely. It is written back through the normal insert path afterwards."""
    from application.services.timetracking import _utcnow

    e = hit["entry"]
    TimeBucket.objects(pk=hit["bucket_pk"]).update_one(
        pull__entries__entry_id=hit["entry_id"],
        inc__entry_count=-1,
        inc__total_minutes=-((e.minutes or 0) if e.status == "active" else 0),
        set__updated_at=_utcnow(),
    )
    left = TimeBucket.objects(pk=hit["bucket_pk"]).first()
    if left is not None and not left.entries:
        left.delete()


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
                    with_entries: bool = True, include_archived: bool = True,
                    dry_run: bool = False) -> MiteStats:
    """Import a mite account into ``team``. Returns what happened.

    ``date_from`` / ``date_to`` bound the time entries. ``with_entries=False``
    brings the structure over on its own — the usual way to *start* using
    Cairn: set up the customers and projects from mite, then log new time here
    while the old history stays in mite. ``include_archived=False`` limits that
    to what is still current. Both are re-runnable: matching is by mite id, so
    a structure-only run can later be followed by a full one (or by another
    structure run that picks up renames and new projects) without duplicating
    anything.

    ``dry_run`` touches nothing and reports what a real run would do."""
    from application.services.tz import user_zone

    stats = MiteStats(dry_run=dry_run, with_entries=with_entries,
                      include_archived=include_archived)
    client = MiteClient(account, api_key)
    zone = user_zone(user)

    # Users are only needed to attribute time entries.
    users = _match_users(client, stats, team) if with_entries else {}
    customers = _import_customers(client, team, stats, dry_run, include_archived)
    projects = _import_projects(client, team, customers, stats, dry_run,
                                include_archived)
    if not with_entries:
        return stats

    params: dict = {}
    if date_from:
        params["from"] = date_from.isoformat()
        params["to"] = (date_to or date.today()).isoformat()
    elif date_to:
        params["from"] = "2000-01-01"
        params["to"] = date_to.isoformat()

    # Read even on a dry run: what is already here, and what of it mite has
    # changed since, is the single most useful thing a preview can tell you.
    known = _known_entries(team)
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
        proj = projects.get(str(raw.get("project_id")))
        if proj is None:
            # mite allows time without a project; Cairn always bills to one.
            stats.errors.append(
                f"entry {raw.get('id')} ({raw.get('date_at')}) skipped — no project")
            continue
        when = _spent_on(raw.get("date_at"), zone)
        minutes = int(raw.get("minutes") or 0)
        if when is None:
            stats.entries_undated += 1
            continue
        if minutes < 1:
            # Nothing was booked. Not a duplicate, not an error — mite simply
            # keeps rows with no time on them.
            stats.entries_empty += 1
            continue
        uid, uname = users.get(str(raw.get("user_id")), ("", raw.get("user_name") or ""))
        billable = bool(raw.get("billable"))
        rate = _cents(raw.get("hourly_rate"))
        revenue = _cents(raw.get("revenue"))
        # mite keeps no start/end fields — by convention they are written into
        # the note as "(10:00 bis 12:00) …". Cairn has the fields, so the
        # convention is decoded here and the note keeps only the words.
        start, end, note = split_clock(raw.get("note") or "")
        period = when.strftime("%Y-%m-%d")
        owned = {
            "project_id": str(proj.id) if proj.id else "",
            "project_name": proj.name,
            "minutes": minutes,
            "spent_on": when,
            "note": note[:1000],
            "service": (raw.get("service_name") or "")[:200],
            "start_time": start or None,
            "end_time": end or None,
            "hourly_rate_cents": rate,
            "amount_cents": revenue if billable else None,
            "billable": billable,
            "locked": bool(raw.get("locked")),
        }

        hit = known.get(ext)
        if hit is not None:
            stored = hit["entry"]
            if stored.status != "active":
                # Deleted here on purpose. mite is not allowed to undo that.
                stats.entries_skipped += 1
                continue
            moved = (hit["period"] != period
                     or hit["customer_id"] != str(proj.customer.id))
            if not moved:
                changes = _entry_corrections(stored, owned)
                if not changes:
                    stats.entries_skipped += 1
                    continue
                if stored.locked and owned["locked"]:
                    # Settled on both sides — a correction after billing is a
                    # conversation, not an import.
                    stats.entries_frozen += 1
                    stats.errors.append(
                        f"entry {raw.get('id')} ({raw.get('date_at')}) changed in "
                        f"mite but is already billed here — left untouched")
                    continue
                if not dry_run:
                    TimeBucket.objects(
                        pk=hit["bucket_pk"], entries__entry_id=hit["entry_id"],
                    ).update_one(
                        inc__total_minutes=minutes - (stored.minutes or 0),
                        set__updated_at=datetime.now(timezone.utc), **changes)
                stats.entries_updated += 1
                continue
            # Its day or its customer changed, so it belongs in a different
            # bucket document: take it out here and let it be written below.
            if stored.locked:
                stats.entries_frozen += 1
                stats.errors.append(
                    f"entry {raw.get('id')} moved in mite but is already billed "
                    f"here — left where it was")
                continue
            if not dry_run:
                _drop_entry(hit)
            stats.entries_updated += 1
        else:
            stats.entries_imported += 1

        entry = TimeEntry(user_id=uid or None, user_name=uname,
                          external_id=ext, **owned)
        known.pop(ext, None)
        pending.setdefault((proj.customer, period), []).append(entry)
        if len(pending) > 200:
            flush()
    flush()
    return stats
