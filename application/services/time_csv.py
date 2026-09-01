"""A mite CSV time export, read back into Cairn.

The migration off mite is done (one API import, once), but its *export* keeps
turning up: a colleague notices a stretch of days that never made it over and
sends the sheet. Retyping it is where the hours quietly change, so this module
reads that file instead.

The format is mite's own, semicolon-separated, German headers and decimal
commas::

    Datum;Start;Ende;Kunde;Projekt;Leistung;Benutzer;Bemerkung;Stunden;\
Umsatz (EUR);Stundensatz (EUR);Abgeschlossen

``Bemerkung`` is quoted and frequently spans several lines — the day's list of
what was done — which is why this is a real CSV reader and not a ``split(";")``.

Two decisions worth knowing about:

* **Money comes from the file, the rate from the project.** An entry is
  ``billable`` when the row carries a rate or a revenue; otherwise it is
  internal time and is written non-billable. The frozen rate is the project's
  current one (:func:`add_entry`'s rule), because that is the rate Cairn would
  bill it at today.
* **A row identifies itself.** mite's entry id is not in the export, so the key
  is a hash of the fields a person would use to recognise the booking (day,
  author, customer/project, duration, note). Re-running the same file therefore
  imports nothing twice, and a corrected row reads as a new booking rather than
  silently rewriting one somebody already touched here.
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import date, datetime

COLUMNS = ("Datum", "Start", "Ende", "Kunde", "Projekt", "Leistung",
           "Benutzer", "Bemerkung", "Stunden", "Umsatz (EUR)",
           "Stundensatz (EUR)", "Abgeschlossen")


@dataclass
class Row:
    """One line of the export, in Cairn's terms."""
    line: int
    spent_on: date
    start_time: str
    end_time: str
    customer: str
    project: str
    service: str
    user: str
    note: str
    minutes: int
    rate_cents: int | None
    amount_cents: int | None
    locked: bool
    key: str

    @property
    def billable(self) -> bool:
        return bool(self.rate_cents or self.amount_cents)


@dataclass
class ParseResult:
    rows: list[Row] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _decimal(raw: str) -> float:
    """``"6,0"`` / ``"6.0"`` / ``""`` → a float. German comma, thousands dot."""
    s = (raw or "").strip().replace(" ", "").replace(" ", "")
    if not s:
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def _clock(raw: str) -> str:
    """``"2026-08-28 09:45:00"`` or ``"09:45"`` → ``"09:45"``; junk → ``""``."""
    s = (raw or "").strip()
    if not s:
        return ""
    if " " in s:
        s = s.split(" ", 1)[1]
    parts = s.split(":")
    if len(parts) < 2:
        return ""
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return ""
    if not (0 <= h < 24 and 0 <= m < 60):
        return ""
    return f"{h:02d}:{m:02d}"


def _row_key(spent_on: date, user: str, customer: str, project: str,
             minutes: int, note: str) -> str:
    """What makes this booking recognisable, hashed — see the module docstring."""
    raw = "|".join([spent_on.isoformat(), user.strip().lower(),
                    customer.strip().lower(), project.strip().lower(),
                    str(minutes), " ".join((note or "").split())])
    return "csv:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def parse(text: str) -> ParseResult:
    """Read the export. Bad lines are reported, not raised: a single unreadable
    row must not cost the operator the other twenty."""
    out = ParseResult()
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    missing = [c for c in ("Datum", "Kunde", "Projekt", "Stunden")
               if c not in (reader.fieldnames or [])]
    if missing:
        out.errors.append(
            "not a mite time export — missing column(s): " + ", ".join(missing))
        return out
    for n, raw in enumerate(reader, start=2):  # header is line 1
        try:
            day = datetime.strptime((raw.get("Datum") or "").strip(),
                                    "%Y-%m-%d").date()
        except ValueError:
            out.errors.append(f"line {n}: unreadable date "
                              f"{(raw.get('Datum') or '')!r}")
            continue
        try:
            minutes = round(_decimal(raw.get("Stunden")) * 60)
        except ValueError:
            out.errors.append(f"line {n}: unreadable hours "
                              f"{(raw.get('Stunden') or '')!r}")
            continue
        if minutes <= 0:
            out.errors.append(f"line {n}: no time on it ({raw.get('Stunden')!r})")
            continue
        note = (raw.get("Bemerkung") or "").strip()
        user = (raw.get("Benutzer") or "").strip()
        customer = (raw.get("Kunde") or "").strip()
        project = (raw.get("Projekt") or "").strip()
        rate = round(_decimal(raw.get("Stundensatz (EUR)")) * 100)
        amount = round(_decimal(raw.get("Umsatz (EUR)")) * 100)
        out.rows.append(Row(
            line=n, spent_on=day,
            start_time=_clock(raw.get("Start")),
            end_time=_clock(raw.get("Ende")),
            customer=customer, project=project,
            service=(raw.get("Leistung") or "").strip()[:200],
            user=user, note=note[:1000], minutes=minutes,
            rate_cents=rate or None, amount_cents=amount or None,
            locked=(raw.get("Abgeschlossen") or "").strip().lower()
            in ("ja", "yes", "1", "true"),
            key=_row_key(day, user, customer, project, minutes, note),
        ))
    return out


# ---------------------------------------------------------------------------
# writing them into the book
# ---------------------------------------------------------------------------

def resolve_project(team, customer_name: str, project_name: str):
    """The billing project a row names, or ``None``.

    Matched on (customer, project) by name, case-insensitively — the export
    carries names, not ids, and mite's names are what the migration created the
    Cairn records from. Nothing is created here: a name this team doesn't have
    is a question for the operator (``--project``), not a new project appearing
    in the books because a spreadsheet said so."""
    from application.models.timetracking import Customer, TimeTrackingProject

    cust = next((c for c in Customer.objects(team=team)
                 if (c.name or "").strip().lower()
                 == customer_name.strip().lower()), None)
    if cust is None:
        return None
    return next((p for p in TimeTrackingProject.objects(team=team, customer=cust)
                 if (p.name or "").strip().lower()
                 == project_name.strip().lower()), None)


def existing_keys(team, rows) -> set:
    """Which of these rows are already booked here, by their key.

    One query over the days the file touches rather than one per row — a
    re-import of a whole month is otherwise a hundred round trips."""
    from application.models.timetracking import TimeBucket

    if not rows:
        return set()
    periods = sorted({r.spent_on.isoformat() for r in rows})
    wanted = {r.key for r in rows}
    found = set()
    for bucket in TimeBucket.objects(team=team, period__in=periods).only("entries"):
        for entry in bucket.entries:
            if entry.external_id in wanted:
                found.add(entry.external_id)
    return found


def import_rows(*, team, rows, project=None, author=None, zone=None,
                dry_run: bool = False):
    """Write the parsed rows into the team's books.

    ``project`` pins every row to one billing project — what the export's own
    names could not be resolved to, e.g. mite's per-person "Büro" against
    Cairn's "Büro Jasmin". ``author`` pins the Cairn account the time is
    attributed to; without one the row's ``Benutzer`` is kept as a bare name,
    the same shape an unmatched import leaves behind (People → assign).

    Returns a summary dict. Nothing is written on a dry run."""
    from datetime import timezone as _tz

    from application.models.timetracking import TimeEntry
    from application.services.timetracking import _append_entries

    zone = zone or _tz.utc
    stats = {"imported": 0, "duplicates": 0, "failed": 0, "minutes": 0,
             "errors": []}
    already = existing_keys(team, rows)
    pending: dict = {}
    for row in rows:
        if row.key in already:
            stats["duplicates"] += 1
            continue
        proj = project or resolve_project(team, row.customer, row.project)
        if proj is None:
            stats["failed"] += 1
            stats["errors"].append(
                f"line {row.line}: no billing project “{row.project}” under "
                f"“{row.customer}” — pick a project to book these on")
            continue
        rate = proj.hourly_rate_cents
        # Noon in the team's zone, like every other write path: it keeps the
        # entry on its own day whichever timezone later reads it.
        when = datetime(row.spent_on.year, row.spent_on.month,
                        row.spent_on.day, 12, 0, tzinfo=zone)
        entry = TimeEntry(
            project_id=str(proj.id), project_name=proj.name,
            user_id=str(author.id) if author else None,
            user_name=((author.display_name or author.username) if author
                       else row.user),
            minutes=row.minutes, spent_on=when,
            start_time=row.start_time or None, end_time=row.end_time or None,
            note=row.note, service=row.service,
            hourly_rate_cents=rate,
            amount_cents=(round(rate * row.minutes / 60)
                          if row.billable and rate else None),
            billable=row.billable, locked=row.locked,
            external_id=row.key,
        )
        pending.setdefault((proj.customer, row.spent_on.isoformat()),
                           []).append(entry)
        stats["imported"] += 1
        stats["minutes"] += row.minutes
    if not dry_run:
        for (customer, period), entries in pending.items():
            _append_entries(team, customer, period, entries)
    return stats
