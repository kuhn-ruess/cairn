"""Import projects, labels and tasks from Todoist's REST API v2.

Authentication uses a personal Todoist API token (Settings → Integrations →
Developer in Todoist). No OAuth flow needed.

The importer is **idempotent**: every Todoist object's id is stored in a
`external_id` field. Re-running the importer updates the matching local row
instead of duplicating.

Mapping
-------
* Todoist project  → bktodo Item (type=parallel, status=active).
  Todoist's ``parent_id`` is preserved → bktodo's ``parent`` reference,
  so the original project hierarchy survives the import.
* Todoist section  → bktodo Item parented under the section's Todoist
  project. Section IDs are stored as ``external_id="section:<id>"`` so they
  never collide with project IDs.
* Todoist label    → bktodo Tag (is_context=False — Todoist labels aren't
  strictly @contexts)
* Todoist task     → bktodo Item
    - Priority: Todoist 4 (highest) … 1 (lowest)  →  bktodo 1 … 4 (inverted)
    - Status:   active → next | inbox (no project) ; completed → done
    - Due date: prefers `due.datetime`, falls back to `due.date`
    - Labels mapped through to bktodo Tags
    - Top-level Todoist comments are fetched and embedded as Comment

Limits
------
* Only the user's own projects are imported (no shared workspace projects).
* Recurring tasks: Todoist stores the recurrence string ("every Monday"),
  which bktodo doesn't model. We import the next instance only and leave a
  note containing the original recurrence string.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from application.models.tag import Tag

from application.models.user import User
from application.models.item import Comment, Item

log = logging.getLogger(__name__)

# Todoist consolidated its REST v2 and Sync v9 endpoints into a single
# versioned API at /api/v1. The old /rest/v2 paths return HTTP 410 (Gone)
# with a hint to upgrade. See: developer.todoist.com/api/v1/
TODOIST_API = "https://api.todoist.com/api/v1"

@dataclass
class ImportStats:
    projects_created: int = 0
    projects_updated: int = 0
    tags_created: int = 0
    tags_updated: int = 0
    tasks_created: int = 0
    tasks_updated: int = 0
    comments_imported: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "projects_created": self.projects_created,
            "projects_updated": self.projects_updated,
            "tags_created": self.tags_created,
            "tags_updated": self.tags_updated,
            "tasks_created": self.tasks_created,
            "tasks_updated": self.tasks_updated,
            "comments_imported": self.comments_imported,
            "errors": list(self.errors),
        }

class TodoistError(RuntimeError):
    pass

class TodoistClient:
    def __init__(self, token: str, timeout: float = 30.0):
        if not token:
            raise TodoistError("Todoist API token is required.")
        self.token = token
        self.timeout = timeout

    def _request(self, path: str, params: dict | None = None) -> Any:
        url = f"{TODOIST_API}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", "replace")[:200]
            raise TodoistError(f"Todoist {exc.code} on {path}: {payload}") from exc
        except urllib.error.URLError as exc:
            raise TodoistError(f"network error talking to Todoist: {exc}") from exc

    def _list(self, path: str, params: dict | None = None) -> list[dict]:
        """Fetch a list endpoint, following ``next_cursor`` pagination.

        The new Todoist /api/v1 endpoints may return either:
        * a bare list ``[...]`` (legacy-style), or
        * a paginated object ``{"results": [...], "next_cursor": "..."}``.

        We handle both.
        """
        params = dict(params or {})
        out: list[dict] = []
        while True:
            payload = self._request(path, params)
            if isinstance(payload, list):
                return payload
            if not isinstance(payload, dict):
                raise TodoistError(f"unexpected response type on {path}: {type(payload).__name__}")
            results = payload.get("results")
            if results is None and "items" in payload:
                results = payload["items"]
            if results is None:
                # Some endpoints return a single object — wrap it
                return [payload]
            out.extend(results)
            next_cursor = payload.get("next_cursor")
            if not next_cursor:
                return out
            params["cursor"] = next_cursor

    def projects(self) -> list[dict]:
        return self._list("/projects")

    def sections(self) -> list[dict]:
        return self._list("/sections")

    def labels(self) -> list[dict]:
        return self._list("/labels")

    def tasks(self) -> list[dict]:
        return self._list("/tasks")

    def completed_tasks(self) -> list[dict]:
        """Tasks completed in roughly the last 3 months.

        The new API requires explicit ``since`` and ``until`` for
        ``/tasks/completed-by-completion-date`` (ISO-8601). We pick a generous
        window so re-imports pick up the typical recently-finished tasks.
        """
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=90)).replace(microsecond=0).isoformat()
        until = now.replace(microsecond=0).isoformat()
        return self._list(
            "/tasks/completed-by-completion-date",
            {"since": since, "until": until, "limit": 200},
        )

    def comments_for_task(self, task_id: str) -> list[dict]:
        """Fetch comments for a task.

        Todoist's parameter name has wobbled between ``task_id`` and
        ``item_id`` across API revisions. Try the canonical name and fall
        back if the server complains.
        """
        try:
            return self._list("/comments", {"task_id": task_id})
        except TodoistError as exc:
            msg = str(exc).lower()
            if "task_id" in msg or "400" in msg or "422" in msg:
                return self._list("/comments", {"item_id": task_id})
            raise

def _parse_due(due: dict | None) -> datetime | None:
    if not due:
        return None
    raw = due.get("datetime") or due.get("date")
    if not raw:
        return None
    try:
        if "T" in raw:
            dt = datetime.fromisoformat(raw.rstrip("Z"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        return datetime.fromisoformat(f"{raw}T00:00:00").replace(tzinfo=timezone.utc)
    except ValueError:
        return None

_NUM_WORDS = {
    "other": 2,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
    "ein": 1, "eine": 1, "einem": 1, "einen": 1,
    "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7,
}
_NAMED_DAYS = {
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
    "mon", "tue", "tues", "wed", "thu", "thurs", "fri", "sat", "sun",
    "montag", "dienstag", "mittwoch", "donnerstag", "freitag",
    "samstag", "sonntag", "mo", "di", "mi", "do", "fr", "sa", "so",
}
_WORKDAY_WORDS = {"weekday", "weekdays", "workday", "workdays", "werktag", "werktage"}
_WEEKEND_WORDS = {"weekend", "weekends", "wochenende"}
_UNIT_WORDS = {
    "day": "day", "days": "day", "daily": "day",
    "tag": "day", "tage": "day", "tagen": "day", "täglich": "day", "taeglich": "day",
    "week": "week", "weeks": "week", "weekly": "week",
    "woche": "week", "wochen": "week", "wöchentlich": "week", "woechentlich": "week",
    "fortnight": "week", "biweekly": "week", "fortnightly": "week",
    "month": "month", "months": "month", "monthly": "month",
    "monat": "month", "monate": "month", "monaten": "month",
    "monatlich": "month",
    "year": "year", "years": "year", "yearly": "year", "annually": "year",
    "jahr": "year", "jahre": "year", "jahren": "year", "jährlich": "year", "jaehrlich": "year",
}
# Words that look like recurrence tokens but encode the per-period
# step (biweekly = every 2 weeks, fortnightly = every 2 weeks).
_BUILTIN_STEPS = {
    "biweekly": 2, "fortnightly": 2, "fortnight": 2,
    "biennial": 2, "biannual": 2, "biannually": 2,
}


def _strip_time_suffix(s: str) -> str:
    """Drop ``at HH:MM`` / ``um HH:MM`` / ``at 9am`` time suffixes — the
    recurrence interval doesn't care about wall-clock time."""
    import re
    return re.sub(
        r"\s+(?:at|um|@)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|uhr)?\s*$",
        "",
        s,
        flags=re.IGNORECASE,
    )


def _parse_recurrence(due_string: str) -> tuple[int | None, str | None]:
    """Best-effort parse of a Todoist recurrence phrase into
    ``(every, unit)`` matching ``Item.repeat_every_days`` +
    ``repeat_unit``. Returns ``(None, None)`` if nothing usable was
    recognised — the task then imports as a one-off.

    Handles the common Todoist patterns: ``every day``, ``every 2
    weeks``, ``every Monday``, ``every weekday``, ``every weekend``,
    ``every other day``, ``every month on the 1st``, ``every Mon, Wed,
    Fri``, ``biweekly``, ``monthly``, etc. — plus German counterparts
    (``jeden Tag``, ``alle 2 Wochen``, ``jeden Werktag``)."""
    if not due_string:
        return None, None
    s = _strip_time_suffix(due_string.lower().strip())
    # Drop trailing "on ..." / "on the ..." anchors (specific day names
    # or day-of-month). We treat the unit + step as the source of
    # truth; the specific anchor isn't expressible in Cairn's model.
    import re
    s = re.sub(r"\s+on\s+the\s+\d+(st|nd|rd|th)?$", "", s)
    s = re.sub(r"\s+am\s+\d+(\.|t(en|er))?$", "", s)
    for prefix in ("every ", "ev ", "jeden ", "jede ", "alle "):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    parts = s.split()
    if not parts:
        return None, None

    # ``every 15th`` / ``every 1st``: day-of-month → monthly.
    import re
    if len(parts) == 1 and re.fullmatch(r"\d+(?:st|nd|rd|th)?", parts[0]):
        return 1, "month"

    # Single-word shortcuts (daily / weekly / monthly / yearly /
    # biweekly / fortnightly / etc.)
    head = parts[0].rstrip(",")
    if head in _BUILTIN_STEPS and len(parts) == 1:
        # biweekly alone defaults to "every 2 weeks"
        return _BUILTIN_STEPS[head], "week"
    if head in _UNIT_WORDS and len(parts) == 1:
        return 1, _UNIT_WORDS[head]
    if head in _WORKDAY_WORDS and len(parts) == 1:
        return 1, "weekday"
    if head in _WEEKEND_WORDS and len(parts) == 1:
        return 1, "weekend"
    if head in _NAMED_DAYS and len(parts) == 1:
        return 1, "week"

    # Numeric step + unit.
    step = 1
    cursor = 0
    if parts[0].isdigit():
        step = int(parts[0])
        cursor = 1
    elif parts[0] in _NUM_WORDS:
        step = _NUM_WORDS[parts[0]]
        cursor = 1

    if cursor < len(parts):
        word = parts[cursor].rstrip(",")
        if word in _UNIT_WORDS:
            return step, _UNIT_WORDS[word]
        if word in _WORKDAY_WORDS:
            return step, "weekday"
        if word in _WEEKEND_WORDS:
            return step, "weekend"
        if word in _BUILTIN_STEPS:
            # Unusual ("2 fortnightly") — keep the step the user said
            # alongside the unit baked into the builtin.
            return step * _BUILTIN_STEPS[word], "week"

    # Falls back to: any day name anywhere → weekly recurrence on that
    # day. Catches "Mon, Wed, Fri" (we lose the multi-day selection
    # but at least keep it as a weekly cadence).
    if any(p.rstrip(",") in _NAMED_DAYS for p in parts):
        return 1, "week"
    if any(p in _WORKDAY_WORDS for p in parts):
        return 1, "weekday"
    if any(p in _WEEKEND_WORDS for p in parts):
        return 1, "weekend"
    for p in parts:
        unit = _UNIT_WORDS.get(p.rstrip(","))
        if unit:
            return step, unit
    return None, None

def import_for_user(
    user: User,
    token: str,
    *,
    include_completed: bool = False,
    include_comments: bool = True,
    only_project_ids: Iterable[str] | None = None,
    dry_run: bool = False,
) -> ImportStats:
    """Run the importer for ``user``. Returns aggregate stats."""
    stats = ImportStats()
    client = TodoistClient(token)

    log.info("Fetching Todoist projects, sections, labels, tasks…")
    projects = client.projects()
    sections = {s["id"]: s for s in client.sections()}
    labels = client.labels()
    tasks = list(client.tasks())
    if include_completed:
        tasks.extend(client.completed_tasks())

    if only_project_ids:
        only = set(only_project_ids)
        projects = [p for p in projects if p["id"] in only]
        tasks = [t for t in tasks if t.get("project_id") in only]

    tag_by_name: dict[str, Tag] = _import_labels(user, labels, stats, dry_run)
    project_map: dict[str, Item] = _import_projects(
        user, projects, stats, dry_run
    )
    section_to_project: dict[str, Item] = _import_sections(
        user, sections.values(), project_map, stats, dry_run
    )

    # Pass 1: create / update every task. We record any Todoist
    # parent_id (sub-task relationship) for the second pass — at this
    # point the parent task may not exist yet.
    parent_map: dict[str, str] = {}  # child external_id → parent external_id
    for raw_task in tasks:
        try:
            _import_task(
                user,
                raw_task,
                project_map,
                section_to_project,
                tag_by_name,
                client,
                stats,
                include_comments=include_comments,
                dry_run=dry_run,
            )
            if raw_task.get("parent_id"):
                child_tid = str(raw_task.get("id") or raw_task.get("task_id") or "")
                if child_tid:
                    parent_map[child_tid] = str(raw_task["parent_id"])
        except Exception as exc:  # noqa: BLE001 — record + continue
            log.exception("failed to import task %s", raw_task.get("id"))
            stats.errors.append(f"task {raw_task.get('id')}: {exc}")

    # Pass 2: wire sub-task parents. Look up the child + parent Item
    # rows by their stored external_id, promote each parent to project
    # (since it has children) and reparent the child onto it.
    if parent_map and not dry_run:
        child_qs = Item.objects(
            owner=user, kind="task", external_id__in=list(parent_map.keys())
        )
        # Parents may already be projects (re-import). Match either kind.
        parent_qs = Item.objects(
            owner=user,
            kind__in=["task", "project"],
            external_id__in=list(parent_map.values()),
        )
        parent_by_tid = {p.external_id: p for p in parent_qs}
        for child in child_qs:
            parent_tid = parent_map.get(child.external_id)
            parent_item = parent_by_tid.get(parent_tid) if parent_tid else None
            if not parent_item:
                continue
            parent_item.ensure_container()
            if child.parent != parent_item:
                child.parent = parent_item
                child.save()

    return stats

def _import_labels(user, labels, stats, dry_run) -> dict[str, Tag]:
    """Sync labels into ``user.tags`` (embedded).

    Returns a name → Tag map (in-memory, before save). Pre-seeds from
    existing user.tags so shared labels — which Todoist's /labels endpoint
    doesn't return — keep their existing styling.
    """
    out: dict[str, Tag] = {t.name: t for t in (user.tags or [])}

    for raw in labels:
        existing = out.get(raw["name"])
        if existing:
            existing.color = _todoist_color(raw.get("color")) or existing.color
            existing.is_context = False
            existing.external_id = str(raw["id"])
            stats.tags_updated += 1
        else:
            # New Todoist labels land under a "Todoist" group tag so
            # the tag tree stays tidy. The group is auto-created on
            # first sync; users can drag tags out of it later and the
            # placement sticks (subsequent syncs touch only newly-
            # added labels).
            new_tag = Tag(
                name=raw["name"],
                color=_todoist_color(raw.get("color")) or "#6c757d",
                is_context=False,
                external_id=str(raw["id"]),
                parent="Todoist",
            )
            user.tags.append(new_tag)
            out[raw["name"]] = new_tag
            stats.tags_created += 1

    if out and not user.tag_by_name("Todoist"):
        user.tags.append(Tag(name="Todoist", color="#6c757d", is_context=False))

    if not dry_run:
        user.save()
    return out

def _import_projects(user, projects, stats, dry_run) -> dict[str, Item]:
    """Two-pass import that preserves Todoist's parent/child hierarchy."""
    from application.services.permissions import (
        FREE_PROJECT_LIMIT, project_count,
    )
    free_cap_warned = False
    out: dict[str, Item] = {}
    parents: dict[str, str] = {}  # external_id -> parent external_id

    # Pass 1: create or update each project, ignoring parent for now.
    for raw in projects:
        if raw.get("inbox_project") or raw.get("is_inbox_project"):
            # Todoist's inbox maps to bktodo's status="inbox" tasks; no project.
            continue
        existing = Item.objects(
            owner=user, kind__in=["project", "folder"], external_id=str(raw["id"])
        ).first()
        if existing is None:
            existing = Item.objects(
                owner=user, kind__in=["project", "folder"], title=raw["name"]
            ).first()
        if existing:
            existing.title = raw["name"]
            existing.kind = "project"
            existing.status = "completed" if raw.get("is_archived") else "active"
            existing.external_id = str(raw["id"])
            if not dry_run:
                existing.save()
            stats.projects_updated += 1
        else:
            # Free-tier project cap: stop creating new ones once we hit the
            # limit. Already-imported ones still get linked + their tasks
            # imported. Warn once so the user knows what's skipped.
            if user.level == "free" and project_count(user) >= FREE_PROJECT_LIMIT:
                if not free_cap_warned:
                    stats.errors.append(
                        f"Free plan limit ({FREE_PROJECT_LIMIT} projects) "
                        "reached — additional Todoist projects were skipped."
                    )
                    free_cap_warned = True
                continue
            existing = Item(
                owner=user,
                kind="project",
                title=raw["name"],
                project_type="parallel",
                status="completed" if raw.get("is_archived") else "active",
                review_interval_days=7,
                external_id=str(raw["id"]),
            )
            if not dry_run:
                existing.save()
            stats.projects_created += 1
        out[raw["id"]] = existing
        if raw.get("parent_id"):
            parents[raw["id"]] = str(raw["parent_id"])

    # Pass 2: wire up parents now that every project exists locally.
    for child_id, parent_id in parents.items():
        child = out.get(child_id)
        parent = out.get(parent_id)
        if not child or not parent:
            continue
        if child.parent != parent:
            child.parent = parent
            if not dry_run:
                child.save()

    return out

def _import_sections(user, sections, project_map, stats, dry_run) -> dict[str, Item]:
    """Materialise Todoist sections as bktodo sub-projects under their parent.

    Sections come back in arbitrary order from the API, so we sort by
    ``section_order`` (the user-visible order in Todoist) before importing.
    The order doesn't change anything in bktodo's storage today, but
    keeping a stable iteration order means re-imports are deterministic.
    """
    sorted_sections = sorted(
        sections, key=lambda s: (s.get("project_id") or "", s.get("section_order") or 0)
    )
    out: dict[str, Item] = {}
    for raw in sorted_sections:
        parent = project_map.get(raw.get("project_id"))
        if not parent:
            # Section under the inbox or a project we skipped — drop it.
            continue
        marker = f"section:{raw['id']}"
        existing = Item.objects(
            owner=user, kind__in=["section", "project"], external_id=marker
        ).first()
        if not existing:
            # Recover from older imports that wrote sections as plain
            # projects: find one by name under the right parent and
            # convert it into a section in-place.
            #
            # Skip anything currently held by ``project_map`` — those
            # are projects we just (re)imported this run, including
            # potentially the section's own parent project when the
            # user has a project and section sharing a name (totally
            # legal in Todoist, "Erinnerungen / Erinnerungen" etc.).
            # Without this guard the parent project would be flipped
            # into a section and start pointing at itself.
            already_imported = {p.pk for p in project_map.values()}
            candidate = Item.objects(
                owner=user, kind__in=["project", "section"],
                title=raw["name"], parent=parent,
                pk__nin=list(already_imported),
            ).first()
            if not candidate:
                candidate = Item.objects(
                    owner=user, kind__in=["project", "section"],
                    title=raw["name"], parent=None,
                    pk__nin=list(already_imported),
                ).first()
            if candidate:
                existing = candidate
        if existing:
            existing.title = raw["name"]
            existing.parent = parent
            existing.kind = "section"
            existing.external_id = marker
            if not dry_run:
                existing.save()
            stats.projects_updated += 1
        else:
            existing = Item(
                owner=user,
                kind="section",
                title=raw["name"],
                project_type="parallel",
                status="active",
                parent=parent,
                review_interval_days=0,
                external_id=marker,
            )
            if not dry_run:
                existing.save()
            stats.projects_created += 1
        out[raw["id"]] = existing
    return out

def _import_task(
    user,
    raw,
    project_map,
    section_to_project,
    tag_by_name,
    client,
    stats,
    *,
    include_comments,
    dry_run,
):
    project = None
    if raw.get("section_id") and raw["section_id"] in section_to_project:
        project = section_to_project[raw["section_id"]]
    elif raw.get("project_id"):
        project = project_map.get(raw["project_id"])

    external_id = str(raw.get("id") or raw.get("task_id") or "")
    existing = (
        Item.objects(owner=user, kind="task", external_id=external_id).first()
        if external_id else None
    )

    # Item.tags is a list of tag NAMES now. Make sure each name exists
    # in the user's tag set (creating an embedded Tag if not). New
    # ones inherit ``parent="Todoist"`` so they nest under the source
    # group; existing tags stay where the user placed them.
    tags: list[str] = []
    todoist_labels = raw.get("labels") or []
    if todoist_labels and not user.tag_by_name("Todoist"):
        user.tags.append(Tag(name="Todoist", color="#6c757d", is_context=False))
        if not dry_run:
            user.save()
    for name in todoist_labels:
        if name not in tag_by_name:
            new_tag = Tag(
                name=name, color="#6c757d", is_context=False,
                parent="Todoist",
            )
            user.tags.append(new_tag)
            tag_by_name[name] = new_tag
            stats.tags_created += 1
            if not dry_run:
                user.save()
        tags.append(name)

    completed = bool(raw.get("is_completed") or raw.get("completed_at"))
    # Imports default to "open" (neutral / unprocessed). Tasks landing
    # under no project are automatically the Inbox via project=None.
    base_status = "done" if completed else "open"
    due = _parse_due(raw.get("due"))
    note = raw.get("description") or ""
    repeat_every = None
    repeat_unit = None
    if raw.get("due") and raw["due"].get("string") and raw["due"].get("is_recurring"):
        rec_string = raw["due"]["string"]
        repeat_every, repeat_unit = _parse_recurrence(rec_string)
        if repeat_every is None:
            # Pattern we couldn't parse — preserve the original string
            # in the note so the user knows the recurrence wasn't lost,
            # just unmodelled.
            marker = f"[todoist recurrence: {rec_string}]"
            if marker not in note:
                note = (note + "\n\n" + marker).strip()

    # Todoist's per-container ordering field — `child_order` is the modern
    # name in v1, `order` is the historical fallback. Either gives us a
    # stable sort within a section/project.
    sort_order = raw.get("child_order")
    if sort_order is None:
        sort_order = raw.get("order")
    if sort_order is None:
        sort_order = 0

    from application.services.label_rules import apply_label_rules

    if existing:
        existing.title = raw["content"]
        existing.note = note
        existing.source = "todoist"
        existing.external_ref = external_id
        # IMPORTANT: do NOT overwrite ``project`` or ``sort_order`` —
        # the user may have moved or reordered the task locally (e.g.
        # dragged it into a new merged project) and the sync must not
        # undo that organisation. Comments, labels, status, due_date
        # and priority still mirror Todoist as the source of truth.
        # Tags are split: ``external_tags`` mirrors Todoist's labels
        # 1:1 so they get refreshed on every sync, while tags the user
        # added in Cairn (= present in ``tags`` but not in the previous
        # ``external_tags``) survive untouched.
        from application.services.external_tags import merge_external_tags
        existing.tags = merge_external_tags(
            existing.tags, existing.external_tags, tags)
        existing.external_tags = list(tags)
        existing.status = base_status
        existing.due_date = due
        # Priority intentionally not imported; never clobber a user-set one.
        existing.external_id = external_id
        if repeat_every is not None:
            existing.repeat_every_days = repeat_every
            existing.repeat_unit = repeat_unit
            existing.repeat_from_completion = False
        else:
            # Recurrence either absent or unparsed — drop any prior
            # value so a task that stopped being recurring stops cloning.
            existing.repeat_every_days = None
            existing.repeat_unit = "day"
        if completed and not existing.completed_at:
            existing.completed_at = datetime.now(timezone.utc)
        apply_label_rules(existing, user)
        if not dry_run:
            existing.save()
        stats.tasks_updated += 1
        task = existing
    else:
        task = Item(
            owner=user,
            kind="task",
            title=raw["content"],
            note=note,
            parent=project,
            source="todoist",
            external_ref=external_id,
            tags=list(tags),
            external_tags=list(tags),
            status=base_status,
            due_date=due,
            sort_order=int(sort_order),
            external_id=external_id,
            completed_at=datetime.now(timezone.utc) if completed else None,
            repeat_every_days=repeat_every,
            repeat_unit=(repeat_unit or "day"),
        )
        apply_label_rules(task, user)
        if not dry_run:
            task.save()
        stats.tasks_created += 1

    # Comments: import for both active and completed tasks. The
    # `comment_count` hint is only present on active responses, so when in
    # doubt we still try the endpoint — the Todoist server short-circuits
    # to an empty list which is cheap.
    if include_comments and external_id:
        hint_count = raw.get("comment_count")
        if hint_count is None or hint_count > 0:
            try:
                comments = client.comments_for_task(external_id)
            except TodoistError as exc:
                stats.errors.append(f"comments for {external_id}: {exc}")
                comments = []
            # Dedup against existing comments by (body, created_at) so two
            # legitimately identical comments still both get imported.
            existing_keys = {
                (c.body, (c.created_at.isoformat() if c.created_at else None))
                for c in (task.comments or [])
            }
            added = 0
            for c in comments:
                body = (c.get("content") or "").strip()
                if not body:
                    continue
                posted_at = c.get("posted_at") or c.get("posted")
                created = _parse_due({"datetime": posted_at}) if posted_at else None
                key = (body, created.isoformat() if created else None)
                if key in existing_keys:
                    continue
                # Imported comments are external: no Cairn account behind them,
                # so they carry an external author label and render flagged as
                # "external" (same treatment as the Zammad import).
                kwargs = {"external_author": "Todoist", "body": body}
                if created:
                    kwargs["created_at"] = created
                task.comments.append(Comment(**kwargs))
                existing_keys.add(key)
                added += 1
            stats.comments_imported += added
            if added and not dry_run:
                task.save()

# Todoist's named colors → hex (truncated to the most common ones).
_TODOIST_COLORS = {
    "berry_red": "#b8255f",
    "red": "#db4035",
    "orange": "#ff9933",
    "yellow": "#fad000",
    "olive_green": "#afb83b",
    "lime_green": "#7ecc49",
    "green": "#299438",
    "mint_green": "#6accbc",
    "teal": "#158fad",
    "sky_blue": "#14aaf5",
    "light_blue": "#96c3eb",
    "blue": "#4073ff",
    "grape": "#884dff",
    "violet": "#af38eb",
    "lavender": "#eb96eb",
    "magenta": "#e05194",
    "salmon": "#ff8d85",
    "charcoal": "#808080",
    "grey": "#b8b8b8",
    "taupe": "#ccac93",
}

def _todoist_color(name: str | None) -> str | None:
    if not name:
        return None
    return _TODOIST_COLORS.get(name)
