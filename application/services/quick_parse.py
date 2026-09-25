"""Todoist-style quick-add string parser.

Recognises a single line of free text and extracts task metadata:

* ``@name``      → person (assignee — the same ``@name`` that mentions a
  teammate in a comment); a name with spaces is written ``@"Bastian Kuhn"``
* ``+name``      → tag (case-insensitive match on the user's existing tags);
  a tag whose name has spaces is written ``+"Review AI Work"``
* ``#name``      → project (case-insensitive match on the user's projects)
* ``p1``…``p4``  → priority (Todoist convention — 1 highest, 4 lowest)
* ``!``          → flag the task
* date words: ``today``, ``tomorrow``, weekday names (``monday``, ``mon``…),
  ``in 3 days``, ``next week``, ISO ``YYYY-MM-DD``, German ``DD.MM.YYYY``

Everything that isn't a recognised token becomes the task title.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
    # German
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
    "freitag": 4, "samstag": 5, "sonntag": 6,
}

PRIORITY_RE = re.compile(r"(?<!\S)p([1-4])(?!\S)", re.IGNORECASE)
# ``@`` is a PERSON everywhere in Cairn — in a comment, in quick-add, and on a
# line captured out of a note — and ``+`` is a tag. Either is one word, or any
# name at all between quotes: that is the only way to reach a tag whose name has
# spaces ("Review AI Work") or a person written by display name.
# A bare tag name may carry a colon INSIDE it (``+AI:local``) — a namespaced tag
# is a common shape and the token used to be cut at the colon, so ``+AI:local``
# came out as the tag "AI" with ``:local`` left sitting in the title. The colon
# only counts when another name character follows it, so a ``+tag`` that merely
# ends a clause (``+phone: ring him back``) still stops at the word.
_TAG_WORD = r"[A-Za-z0-9_\-äöüÄÖÜß]+(?::[A-Za-z0-9_\-äöüÄÖÜß]+)*"
TAG_RE = re.compile(r'(?<!\S)\+(?:"([^"\n]+)"|(' + _TAG_WORD + r')\b)')
PROJECT_RE = re.compile(r"(?<!\S)#([A-Za-z0-9_\-äöüÄÖÜß]+)\b")
ASSIGNEE_RE = re.compile(r'(?<!\S)@(?:"([^"\n]+)"|([A-Za-z0-9._\-äöüÄÖÜß]+)\b)')
FLAG_RE = re.compile(r"(?<!\S)!(?!\S)")
TODAY_RE = re.compile(r"\b(today|heute)\b", re.IGNORECASE)
TOMORROW_RE = re.compile(r"\b(tomorrow|morgen)\b", re.IGNORECASE)
YESTERDAY_RE = re.compile(r"\b(yesterday|gestern)\b", re.IGNORECASE)
IN_DAYS_RE = re.compile(r"\bin\s+(\d+)\s+(day|days|tag|tage)\b", re.IGNORECASE)
NEXT_WEEK_RE = re.compile(r"\bnext\s+week\b|\bnaechste\s+woche\b", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
DE_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
WEEKDAY_RE = re.compile(
    r"\b(?:next\s+)?(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

@dataclass
class ParsedTask:
    title: str = ""
    project: Any = None
    tags: list[str] = field(default_factory=list)   # names, as on ``Item.tags``
    priority: int | None = None
    due_date: datetime | None = None
    flagged: bool = False
    assignee: Any = None
    unresolved_tags: list[str] = field(default_factory=list)
    unresolved_project: str | None = None
    unresolved_assignee: str | None = None

    def as_summary(self) -> dict:
        return {
            "title": self.title,
            "project": str(self.project) if self.project else None,
            "tags": list(self.tags),
            "priority": self.priority,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "flagged": self.flagged,
            "assignee": str(self.assignee) if self.assignee else None,
            "unresolved_tags": self.unresolved_tags,
            "unresolved_project": self.unresolved_project,
            "unresolved_assignee": self.unresolved_assignee,
        }

def _end_of_day(d: datetime) -> datetime:
    return d.replace(hour=23, minute=59, second=0, microsecond=0)

def parse_task_text(text: str, user, default_project=None) -> ParsedTask:
    """Pull every recognised token out of ``text`` and return a ``ParsedTask``.

    ``user`` is required to resolve tag / project names against what the user
    actually owns (case-insensitive). Anything that doesn't resolve is kept
    aside in ``unresolved_*`` for the UI to surface.
    """
    from application.models.item import Item as Item

    parsed = ParsedTask(title=text, project=default_project)

    # Priority
    m = PRIORITY_RE.search(text)
    if m:
        parsed.priority = int(m.group(1))
        text = PRIORITY_RE.sub(" ", text, count=1)

    # Flag
    if FLAG_RE.search(text):
        parsed.flagged = True
        text = FLAG_RE.sub(" ", text)

    # Tags — multiple allowed; resolved against the user's embedded tags, and
    # handed back as NAMES, which is what ``Item.tags`` is a list of. Handing
    # back the embedded tag document instead is how a caller ended up assigning
    # objects to a list of strings and 500ing on save.
    for tm in TAG_RE.finditer(text):
        name = tm.group(1) or tm.group(2)
        tag = user.tag_by_name(name)
        if tag:
            parsed.tags.append(tag.name)
        else:
            parsed.unresolved_tags.append(name)
    text = TAG_RE.sub(" ", text)

    # Item — only first match wins; replaces the default if found.
    pm = PROJECT_RE.search(text)
    if pm:
        name = pm.group(1)
        # ``Item.name`` is a Python alias for ``title`` — only the real
        # field can be used in a mongo query.
        project = Item.objects(
            owner=user, kind__in=["project", "folder"], title__iexact=name,
        ).first()
        if project:
            parsed.project = project
        else:
            parsed.unresolved_project = name
        text = PROJECT_RE.sub(" ", text, count=1)

    # Assignee — ``@name`` resolves to a user the actor may assign to.
    am = ASSIGNEE_RE.search(text)
    if am:
        from application.services.permissions import resolve_assignable_user_by_name
        name = am.group(1) or am.group(2)
        assignee = resolve_assignable_user_by_name(user, name)
        if assignee:
            parsed.assignee = assignee
        else:
            parsed.unresolved_assignee = name
        text = ASSIGNEE_RE.sub(" ", text, count=1)

    # ---- date parsing ----
    now = datetime.now(timezone.utc)

    def consume(pat, replacement=" "):
        nonlocal text
        text = pat.sub(replacement, text, count=1)

    if parsed.due_date is None:
        if TODAY_RE.search(text):
            parsed.due_date = _end_of_day(now); consume(TODAY_RE)
        elif TOMORROW_RE.search(text):
            parsed.due_date = _end_of_day(now + timedelta(days=1)); consume(TOMORROW_RE)
        elif YESTERDAY_RE.search(text):
            parsed.due_date = _end_of_day(now - timedelta(days=1)); consume(YESTERDAY_RE)
        elif NEXT_WEEK_RE.search(text):
            parsed.due_date = _end_of_day(now + timedelta(days=7)); consume(NEXT_WEEK_RE)
        else:
            im = IN_DAYS_RE.search(text)
            if im:
                parsed.due_date = _end_of_day(now + timedelta(days=int(im.group(1))))
                consume(IN_DAYS_RE)

    if parsed.due_date is None:
        wm = WEEKDAY_RE.search(text)
        if wm:
            target = WEEKDAYS[wm.group(1).lower()]
            delta = (target - now.weekday()) % 7
            if delta == 0:
                delta = 7
            parsed.due_date = _end_of_day(now + timedelta(days=delta))
            consume(WEEKDAY_RE)

    if parsed.due_date is None:
        im = ISO_DATE_RE.search(text)
        if im:
            try:
                dt = datetime.fromisoformat(im.group(1)).replace(tzinfo=timezone.utc)
                parsed.due_date = _end_of_day(dt)
                consume(ISO_DATE_RE)
            except ValueError:
                pass

    if parsed.due_date is None:
        dm = DE_DATE_RE.search(text)
        if dm:
            try:
                d = datetime(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)),
                             tzinfo=timezone.utc)
                parsed.due_date = _end_of_day(d)
                consume(DE_DATE_RE)
            except ValueError:
                pass

    # What's left after stripping tokens is the title.
    parsed.title = " ".join(text.split()).strip()
    return parsed
