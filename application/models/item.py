"""Unified ``Item`` model — one document covers tasks, projects,
sections, and folders.

``kind`` discriminates behaviour:
* ``task``    — actionable to-do. Can also have child items (= a project
                in OmniFocus terms).
* ``project`` — a container for tasks; behaves like a task that has
                children + project metadata (status, review, members).
* ``section`` — inline grouping inside a project. Holds child tasks.
* ``folder``  — organisational container; holds projects/folders only,
                no tasks.

Hierarchy is expressed via ``parent`` (self-reference). Any item can
have a parent of any kind, though only certain combos are meaningful:

* task   → parent ∈ {task, project, section}   (sub-task or list item)
* project → parent ∈ {project, folder}         (sub-project)
* section → parent ∈ {project}                 (always inside a project)
* folder → parent ∈ {folder}                   (nested folders)

Lifecycle / cascade rules: never declared via mongoengine
``reverse_delete_rule`` (per-doc fan-out would N+1 at scale). Use
``application.services.cleanup`` for bulk delete pathways.
"""
from datetime import datetime, timedelta, timezone

from mongoengine import (
    BinaryField,
    BooleanField,
    DateTimeField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentListField,
    IntField,
    ListField,
    ReferenceField,
    StringField,
    queryset_manager,
)

from application.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


ITEM_KINDS = ("task", "project", "section", "folder")
TASK_STATUS = ("open", "next", "waiting", "waiting_reply", "agenda",
               "scheduled", "someday", "done", "dropped")
PROJECT_TYPES = ("parallel", "sequential", "single_actions")
PROJECT_STATUS = ("active", "on_hold", "completed", "dropped")
PRIORITIES = (1, 2, 3, 4)
REPEAT_UNITS = ("day", "weekday", "weekend", "week", "month", "year")


class Comment(EmbeddedDocument):
    # ``author`` is the in-app user who wrote the comment. It's optional
    # because comments can also be mirrored in from an external system
    # (e.g. a Zammad ticket reply): those have no Cairn account, so we keep
    # the real sender name in ``external_author`` and leave ``author`` unset.
    # ``external_id`` is the source-side id (Zammad article id) used to
    # dedupe on re-sync so a refreshed thread updates instead of duplicating.
    author = ReferenceField(User)
    external_author = StringField()
    external_id = StringField()
    # ``external`` marks a comment that came from an imported system, kept
    # independent of ``author``: an imported comment written by the importing
    # user gets both a resolved ``author`` (correct attribution) AND this flag
    # (so it still renders with the "external" badge).
    external = BooleanField(default=False)
    body = StringField(required=True)
    created_at = DateTimeField(default=_utcnow)
    is_public = BooleanField(default=False)

    @property
    def is_external(self) -> bool:
        """True for comments mirrored in from an external system.

        Either the explicit flag is set, or there's no app user behind it
        (covers comments imported before the flag existed)."""
        return bool(self.external) or self.author is None

    @property
    def author_name(self) -> str:
        """Display name for the comment, app user or external sender alike."""
        if self.author:
            return self.author.display_name or self.author.username
        return self.external_author or "Unknown"


class ChecklistItem(EmbeddedDocument):
    """A simple checkbox-with-text sub-step on a task. Only the text and
    a checked/unchecked flag are stored — nothing else."""
    text = StringField(required=True, max_length=500)
    done = BooleanField(default=False)


class NoteImage(EmbeddedDocument):
    """A compressed image pasted into a note, stored inline on the item
    (never on the filesystem). The note references it by ``token`` via
    ``/tasks/<id>/note-image/<token>`` instead of carrying a base64 blob."""
    token = StringField(required=True)
    content_type = StringField(default="image/webp")
    data = BinaryField(required=True)
    created_at = DateTimeField(default=_utcnow)


class TimeEntry(EmbeddedDocument):
    """A manually logged chunk of time spent on a task: ``minutes`` worked on
    ``spent_on`` (the date the work happened, tz-aware), with an optional
    note. ``user`` records who logged it, since a task may be shared/assigned
    — the monthly report groups each person's own entries."""
    minutes = IntField(required=True, min_value=1)
    spent_on = DateTimeField(default=_utcnow)
    note = StringField(default="")
    user = ReferenceField(User)
    created_at = DateTimeField(default=_utcnow)


class Item(Document):
    meta = {
        "collection": "items",
        "indexes": [
            "owner",
            "kind",
            "parent",
            "status",
            "assigned_to",
            "assigned_by",
            "due_date",
            "defer_date",
            "members",
            "next_review_at",
            "trashed_at",
            ("owner", "kind"),
            ("owner", "status"),
            ("assigned_to", "status"),
            # Today/Plan/forecast filter by owner+kind on a date field.
            ("owner", "kind", "plan_date"),
            ("owner", "kind", "due_date"),
            # Project detail / sequential-blocking walk children by parent.
            ("owner", "kind", "parent"),
        ],
        "ordering": ["+sort_order", "+created_at"],
    }

    # discriminator
    kind = StringField(choices=ITEM_KINDS, default="task")

    # ownership & hierarchy
    owner = ReferenceField(User, required=True)
    parent = ReferenceField("Item")
    # Per-assignee "second home": when a task is delegated to someone who
    # can't see its ``parent`` project, they may file it under one of THEIR
    # OWN projects/folders. This never touches ``parent`` (the owner's
    # canonical placement), so the task stays put for the owner and only the
    # assignee's views group it here. Always owned by ``assigned_to``; cleared
    # on un-/re-assign. See ``home_for``.
    assignee_parent = ReferenceField("Item")
    sort_order = IntField(default=0)
    # Loose, symmetric "see also" links between items. Purely a reference
    # surfaced in the inspector — deliberately NOT a hierarchy: no query
    # anywhere filters/groups by ``related``, so it never affects lists,
    # perspectives, the tree, or completion. Both sides carry the link
    # (see ``relate_to`` / ``unrelate_from``).
    related = ListField(ReferenceField("Item"))

    # common content
    title = StringField(required=True, max_length=500)
    note = StringField(default="")
    color = StringField(default="", max_length=16)
    tags = ListField(StringField())
    # Tags that came from an external system (Zammad, CSV, …). Kept
    # separate so re-imports can replace these without touching tags
    # the user added by hand: the importer computes
    #   user_added = task.tags − task.external_tags
    #   task.tags  = user_added ∪ new_external_tags
    #   task.external_tags = new_external_tags
    external_tags = ListField(StringField())

    # bookkeeping
    created_at = DateTimeField(default=_utcnow)
    updated_at = DateTimeField(default=_utcnow)
    # Soft-delete: when set, the item is in the trash — hidden everywhere via
    # the default ``objects`` manager (see below) until restored or purged.
    trashed_at = DateTimeField()
    completed_at = DateTimeField()
    # Who/what closed this task (done or dropped). ``completed_by`` is the
    # acting user when known (manual completion); ``completed_via`` records
    # the channel — "user", "zammad", "todoist", "api", "cleanup" … — so a
    # close driven by an external sync is distinguishable from a manual one.
    completed_by = ReferenceField(User)
    completed_via = StringField()
    # Stable identifier from whichever external system the item came
    # from. Conventions for the prefix:
    #   no prefix   — Todoist (the original importer)
    #   ``zammad:`` — Zammad ticket id
    #   ``csv:``    — CSV-import row id
    # Used internally to dedupe on re-import and to find orphans during
    # reconcile. Never shown in the UI.
    external_id = StringField()
    # Bare ticket reference for display + search: ``CHMK-417`` for Jira,
    # ``1234`` for Zammad, the Todoist task id, etc. Importers strip the
    # prefix when filling this so the chip in the UI reads cleanly and
    # users can ⌘P + paste a ticket number to jump to its task.
    external_ref = StringField()
    # Human-readable origin (``todoist`` / ``zammad`` / ``csv`` /
    # ``jira`` / …). Importers fill this so the UI can show a "source"
    # chip without parsing the ``external_id`` prefix.
    source = StringField()

    # ----- task-flavoured fields (also valid on project/section) -----
    status = StringField(default="open")  # values vary by kind
    flagged = BooleanField(default=False)
    priority = IntField(choices=PRIORITIES)
    defer_date = DateTimeField()
    due_date = DateTimeField()
    plan_date = DateTimeField()
    estimated_minutes = IntField()
    # Free-text location (e.g. "REWE, Hauptstraße 1"). Round-trips with the
    # iCalendar LOCATION property so a CalDAV client's location reminder
    # survives. The geofence (radius/coords) from Apple's structured location
    # is not preserved — only the human-readable place.
    location = StringField()
    # Recurrence — back-compat layout:
    #   ``repeat_every_days`` was the only knob in the old schema. It
    #   stays the "how many" counter. ``repeat_unit`` says what one
    #   unit means (day/week/month/year). Together they describe e.g.
    #   "every 2 weeks". Months / years use dateutil.relativedelta so
    #   calendar arithmetic stays honest.
    repeat_every_days = IntField()
    repeat_unit = StringField(choices=REPEAT_UNITS, default="day")
    repeat_from_completion = BooleanField(default=False)
    # Specific weekdays the task repeats on (0=Mon … 6=Sun). When set, the
    # next occurrence lands on the soonest selected weekday after the anchor,
    # ignoring repeat_every_days/unit. Empty = use the every-N-units schedule.
    repeat_weekdays = ListField(IntField(min_value=0, max_value=6))
    is_internal = BooleanField(default=False)

    # ----- follow-up -----
    # For items you have to chase repeatedly (typically waiting on someone).
    # ``followup_at`` is the next reminder to nudge — it drives the Follow-up
    # perspective and is kept *separate* from ``due_date`` on purpose: the
    # deadline of the task and the cadence on which you poke about it are two
    # different things. ``followup_last_at`` records the last nudge.
    # ``followup_every_days`` is the cadence the "Mark followed up" button rolls the
    # next reminder forward by. ``waiting_on`` is a free-text contact for the
    # common case of chasing someone *without* a Cairn account (complements
    # ``assigned_to``, which only points at in-app users).
    waiting_on = StringField()
    followup_at = DateTimeField()
    followup_last_at = DateTimeField()
    followup_every_days = IntField()

    # collaboration / assignment
    assigned_to = ReferenceField(User)
    assigned_by = ReferenceField(User)
    assigned_at = DateTimeField()
    members = ListField(ReferenceField(User))  # for project-like items

    # comments live with the item
    comments = EmbeddedDocumentListField(Comment)

    # simple checklist (checkbox + text) on a task
    checklist = EmbeddedDocumentListField(ChecklistItem)

    # compressed images pasted into the note, stored inline (see NoteImage)
    note_images = EmbeddedDocumentListField(NoteImage)

    # manually logged time spent on this task (see TimeEntry + time report)
    time_entries = EmbeddedDocumentListField(TimeEntry)

    # ----- project-flavoured fields -----
    project_type = StringField(choices=PROJECT_TYPES, default="parallel")
    # Review cadence in days. Applies to tasks AND projects. ``None`` =
    # inherit the nearest ancestor's interval (see effective_review_interval);
    # an explicit value overrides; ``0`` = never review. Projects normally set
    # their own; tasks usually leave it blank to follow their project.
    review_interval_days = IntField(min_value=0)
    last_reviewed_at = DateTimeField()
    next_review_at = DateTimeField()
    task_grouping = StringField(default="section")
    task_grouping_tag = StringField(default="")
    view_mode = StringField(default="list")
    show_blocked_tasks = BooleanField(default=False)
    # When set on a project, completing the last open task anywhere in the
    # project auto-marks the project ``completed`` (see complete()).
    auto_complete_when_done = BooleanField(default=False)

    # ----- CSV-import settings (persisted per project) + Jira deep-link -----
    # On a *project* these remember the last choices made on the import form
    # so the user doesn't re-pick them every time. ``jira_url`` is the base
    # URL of the Jira instance the project's CSV exports come from.
    # On a *task* ``jira_url`` is denormalised from the project at import time
    # so external_url() can build a ``/browse/<KEY>`` deep link without an
    # N+1 project lookup per row when rendering lists.
    csv_import_mode = StringField(default="sync")
    csv_archive_missing = BooleanField(default=False)
    jira_url = StringField()

    # iCalendar UID for tasks created by a CalDAV client (Apple Reminders &
    # co). Cairn-origin tasks use a derived ``cairn-<pk>`` UID and leave this
    # blank; client-origin tasks store the client's UID here so PUT/GET/DELETE
    # on ``<uid>.ics`` map back to the right Item. See application/views/caldav.py.
    caldav_uid = StringField()

    # public share
    share_enabled = BooleanField(default=False)
    share_token = StringField(unique=True, sparse=True)

    # calendar write-back
    gcal_due_event_id = StringField()
    gcal_plan_event_id = StringField()

    # ---------- managers ----------
    # ``Item.objects`` hides trashed items everywhere by default, so the ~170
    # existing query sites need no changes. ``Item.all_objects`` sees the whole
    # collection (trash view, restore, purge, cascade). Reference dereferencing
    # (parent/owner/assigned_to) bypasses the manager, so a trashed parent still
    # resolves normally.
    @queryset_manager
    def objects(doc_cls, queryset):
        return queryset.filter(trashed_at=None)

    @queryset_manager
    def all_objects(doc_cls, queryset):
        return queryset

    # ---------- helpers ----------

    def save(self, *args, **kwargs):
        self.updated_at = _utcnow()
        # Stamp completed_at when a task transitions to done/dropped,
        # clear on reopen. Project lifecycle (active/on_hold/completed/
        # dropped) doesn't go through this path.
        cascade_status = None
        if self.kind == "task":
            if self.status in ("done", "dropped"):
                if not self.completed_at:
                    self.completed_at = _utcnow()
                # Default the channel for direct status writes (edit form,
                # bulk set-status) that didn't go through mark_done/complete.
                # An external sync sets completed_via before saving, so this
                # only fills the gap for manual closes.
                if not self.completed_via:
                    self.completed_via = "user"
                # Closing a task closes its sub-tasks too — but only on the
                # actual transition (status just changed), not on every later
                # save of an already-closed task. New docs (no id yet) skip it;
                # the cascade is a bulk update that needs the saved pk anyway.
                if self.id is not None and "status" in self._get_changed_fields():
                    cascade_status = self.status
            else:
                # Reopened — clear the whole completion record (who/when/how).
                self.completed_at = None
                self.completed_by = None
                self.completed_via = None
        result = super().save(*args, **kwargs)
        if cascade_status:
            self._cascade_close(cascade_status)
        return result

    def _descendant_task_pks(self) -> list:
        """All sub-task pks beneath this task (any depth), excluding self.
        BFS, one query per level — task trees are shallow."""
        out, frontier = [], [self.pk]
        while frontier:
            kids = list(Item.objects(parent__in=frontier, kind="task").only("id"))
            if not kids:
                break
            frontier = [k.pk for k in kids]
            out.extend(frontier)
        return out

    def _cascade_close(self, status: str) -> None:
        """Close every still-open sub-task to ``status`` (done/dropped) in one
        bulk update — no per-child save(), so no recursion."""
        pks = self._descendant_task_pks()
        if not pks:
            return
        Item.objects(pk__in=pks, status__nin=["done", "dropped"]).update(
            set__status=status,
            set__completed_at=_utcnow(),
            set__completed_by=self.completed_by,
            set__completed_via="cascade",
            set__updated_at=_utcnow(),
        )

    def _capture_subtasks(self) -> list:
        """Snapshot the sub-task tree as plain data (for recurrence). Read
        before closing so a fresh occurrence can be rebuilt with the same
        structure, reset to open."""
        snap = []
        for st in self.child_tasks():
            snap.append({
                "title": st.title, "note": st.note, "color": st.color,
                "tags": list(st.tags or []), "flagged": st.flagged,
                "priority": st.priority, "estimated_minutes": st.estimated_minutes,
                "is_internal": st.is_internal, "assigned_to": st.assigned_to,
                "checklist": [ci.text for ci in (st.checklist or [])],
                "children": st._capture_subtasks(),
            })
        return snap

    def _rebuild_subtasks(self, parent: "Item", snap: list) -> None:
        """Recreate captured sub-tasks under ``parent``, reset to open."""
        for d in snap:
            child = Item(
                kind="task", owner=parent.owner, parent=parent,
                title=d["title"], note=d["note"], color=d["color"],
                tags=list(d["tags"]), status="open", flagged=d["flagged"],
                priority=d["priority"], estimated_minutes=d["estimated_minutes"],
                is_internal=d["is_internal"], assigned_to=d["assigned_to"],
                checklist=[ChecklistItem(text=t, done=False) for t in d["checklist"]],
            )
            child.save()
            self._rebuild_subtasks(child, d["children"])

    def mark_done(self, *, by=None, via="user", status="done"):
        """Close this task, recording who/when/how. Use this instead of
        setting ``status`` by hand so the completion bookkeeping
        (completed_at / completed_by / completed_via) is always consistent.

        ``status`` may be ``done`` or ``dropped``. ``via`` is the channel
        ("user", "zammad", "todoist", "api", "cleanup")."""
        self.status = status
        if not self.completed_at:
            self.completed_at = _utcnow()
        self.completed_by = by
        self.completed_via = via
        self.save()

    def related_items(self):
        """Resolved related items for the inspector: skip dangling refs
        (the other side was purged — mongoengine may leave a bare DBRef)
        and trashed items (reference deref bypasses the ``objects`` manager,
        so a trashed link would otherwise still show)."""
        out = []
        for r in self.related or []:
            if not isinstance(r, Item) or r.trashed_at:
                continue
            out.append(r)
        return out

    def child_tasks(self):
        """Direct sub-tasks (``kind='task'`` children). A task can hold these
        while staying a task — it does NOT have to become a project."""
        return Item.objects(parent=self, kind="task").order_by("+sort_order", "+created_at")

    @property
    def related_count(self) -> int:
        """Cheap count of related links for list rows: reads the raw stored
        refs straight from ``_data`` so it never dereferences (accessing
        ``self.related`` would fire one $in query per row → N+1 down a list).
        May include dangling/trashed refs the inspector's ``related_items``
        filters out — a tiny over-count on a badge is the right trade for
        zero extra queries (see CLAUDE.md: minimise DB load per page)."""
        return len(self._data.get("related") or [])

    @property
    def assigned_to_id(self) -> str:
        """Assignee's id as a string without dereferencing the ref — reads
        the raw DBRef from ``_data`` so a list row never fires a per-row
        query just to mark "assigned to me" (see CLAUDE.md: minimise DB
        load per page). Empty string when unassigned."""
        ref = self._data.get("assigned_to")
        return str(ref.id) if ref is not None else ""

    def relate_to(self, other):
        """Create a symmetric related link to ``other`` and persist both
        sides. No-op for a self-link or an existing one. Compared by pk so
        we don't depend on Document __eq__ across lazy dereferences."""
        if other is None or other.pk == self.pk:
            return
        if all(getattr(r, "pk", None) != other.pk for r in self.related or []):
            self.related.append(other)
            self.save()
        if all(getattr(r, "pk", None) != self.pk for r in other.related or []):
            other.related.append(self)
            other.save()

    def unrelate_from(self, other):
        """Drop the symmetric link to ``other`` from both sides. ``other``
        may be None (already purged) — then only this side is cleaned up,
        which also strips any dangling DBRef left in our list."""
        oid = getattr(other, "pk", None)
        self.related = [r for r in self.related or []
                        if isinstance(r, Item) and r.pk != oid]
        self.save()
        if other is not None:
            other.related = [r for r in other.related or []
                             if isinstance(r, Item) and r.pk != self.pk]
            other.save()

    def external_url(self, user=None):
        """Deep link to the upstream item this was imported from, or None.

        ``user`` (optional) supplies the integration config (e.g.
        ``zammad_url``); pass the viewing user when rendering lists so we
        don't dereference ``self.owner`` per row. Falls back to the owner's
        config when ``user`` has none (e.g. an assignee viewing a shared
        task imported by someone else).
        """
        if self.source == "zammad" and (self.external_id or "").startswith("zammad:"):
            base = getattr(user, "zammad_url", None) or getattr(self.owner, "zammad_url", None)
            if not base:
                return None
            base = base.rstrip("/")
            if base.endswith("/api/v1"):
                base = base[: -len("/api/v1")].rstrip("/")
            # Only emit http(s) — a "javascript:"/"data:" zammad_url would
            # otherwise render as an executable href (stored XSS, esp. on
            # shared tasks).
            if not base.startswith(("http://", "https://")):
                return None
            zid = self.external_id.split(":", 1)[1]
            return f"{base}/#ticket/zoom/{zid}"
        if self.source == "jira" and self.jira_url and self.external_ref:
            # external_ref holds the issue key (e.g. CHMK-417); jira_url is
            # denormalised onto the task at import (see import_csv). Only emit
            # http(s) — a "javascript:"/"data:" base would otherwise render as
            # an executable href (stored XSS, esp. on shared tasks).
            base = self.jira_url.rstrip("/")
            if base.startswith(("http://", "https://")):
                return f"{base}/browse/{self.external_ref}"
        if self.source == "todoist" and self.external_ref:
            # Todoist is always todoist.com — no per-task config needed.
            # external_ref holds the Todoist task id.
            return f"https://app.todoist.com/app/task/{self.external_ref}"
        return None

    def effective_jira_url(self) -> str:
        """The Jira base URL set on this item or its nearest ancestor.

        A project inherits the URL configured on the folder that contains it
        (and that folder from its parent folder), so the user can set it once
        per folder instead of on every project. Walks ``parent`` refs — cheap
        and called once per import, not per row.
        """
        cur, seen = self, set()
        while cur is not None and cur.pk not in seen:
            if getattr(cur, "jira_url", None):
                return cur.jira_url
            seen.add(cur.pk)
            cur = cur.parent
        return ""

    # ----- aliases for code/templates from the pre-unified era -----
    # ``task.project`` used to mean "the container this task is in".
    # That's now just ``parent``. ``project.parent`` already worked.
    @property
    def name(self) -> str:
        return self.title

    @name.setter
    def name(self, value: str) -> None:
        self.title = value

    @property
    def project(self):
        return self.parent

    @project.setter
    def project(self, value) -> None:
        self.parent = value

    # Back-compat shorthands used widely in templates.
    @property
    def is_folder(self) -> bool:
        return self.kind == "folder"

    @property
    def is_section(self) -> bool:
        return self.kind == "section"

    @property
    def is_project(self) -> bool:
        return self.kind == "project"

    @property
    def is_task(self) -> bool:
        return self.kind == "task"

    # ----- task behaviour helpers (no-op for non-tasks) -----

    def is_available(self, now: datetime | None = None) -> bool:
        return not self.is_blocked(now)

    def is_blocked(self, now: datetime | None = None) -> bool:
        """Blocked = displayed but not actionable yet.

        Triggers:
          * ``defer_date`` is in the future
          * lives in a sequential parent and an earlier incomplete
            sibling exists
        """
        now = now or _utcnow()
        if self.defer_date and self.defer_date > now:
            return True
        if self.parent and self.parent.project_type == "sequential":
            earlier = (
                Item.objects(parent=self.parent,
                              kind="task",
                              status__nin=["done", "dropped"])
                .order_by("+sort_order", "+created_at")
                .first()
            )
            if earlier and earlier.pk != self.pk:
                return True
        return False

    def complete(self, by_user: User | None = None) -> "Item | None":
        from datetime import timedelta as _td
        from dateutil.relativedelta import relativedelta
        # Snapshot the sub-task tree BEFORE closing — the save() below cascades
        # them to done, so a recurring task needs the structure captured first
        # to recreate it fresh on the next occurrence.
        recurring = self.kind == "task" and bool(
            self.repeat_every_days or self.repeat_weekdays
        )
        captured_subtasks = self._capture_subtasks() if recurring else None
        self.status = "done"
        self.completed_at = _utcnow()
        self.completed_by = by_user
        self.completed_via = "user"
        self.save()  # cascades: closes this task's sub-tasks too
        # Recurrence only makes sense for actual tasks. A project/folder/
        # section never spawns a clone (defensive — ensure_container already
        # strips recurrence on the flip).
        if self.kind != "task":
            return None
        if not self.repeat_every_days and not self.repeat_weekdays:
            self._autocomplete_enclosing_project(by_user)
            return None
        anchor = (
            self.completed_at if self.repeat_from_completion
            else (self.due_date or self.completed_at)
        )
        unit = self.repeat_unit or "day"
        n = self.repeat_every_days

        def _advance_business(d, count, valid_set):
            """Step ``count`` calendar days, skipping any weekday not in
            ``valid_set`` (Mon=0 … Sun=6)."""
            i = 0
            while i < count:
                d = d + _td(days=1)
                if d.weekday() in valid_set:
                    i += 1
            return d

        new_due = None
        defer_delta = None
        # Specific-weekday recurrence (e.g. every Mon/Wed/Fri) takes
        # precedence over the every-N-units schedule and lands on the next
        # selected weekday strictly after the anchor.
        if self.repeat_weekdays and anchor:
            valid = set(self.repeat_weekdays)
            d = anchor
            for _ in range(7):
                d = d + _td(days=1)
                if d.weekday() in valid:
                    break
            new_due = d
        elif anchor:
            if unit == "week":
                new_due = anchor + relativedelta(weeks=n)
                defer_delta = relativedelta(weeks=n)
            elif unit == "month":
                new_due = anchor + relativedelta(months=n)
                defer_delta = relativedelta(months=n)
            elif unit == "year":
                new_due = anchor + relativedelta(years=n)
                defer_delta = relativedelta(years=n)
            elif unit == "weekday":
                new_due = _advance_business(anchor, n, {0, 1, 2, 3, 4})
            elif unit == "weekend":
                new_due = _advance_business(anchor, n, {5, 6})
            else:  # day
                new_due = anchor + relativedelta(days=n)
                defer_delta = relativedelta(days=n)

        # Roll the defer and plan dates forward so their relationship to the
        # due date is preserved each cycle. When the task has a due date we
        # anchor on it directly: keep each date's fixed offset to the due
        # date and reapply it to the new due date. That stays correct even
        # when ``repeat_from_completion`` makes the due date jump by the
        # (possibly large) completion gap rather than by one period — the old
        # ``defer_delta`` shift left the defer date lagging in the past in
        # that case. With no due date to anchor against, fall back to the
        # per-period shift (relativedelta where we have one, else the raw new
        # vs. old due difference for weekday/weekend schedules).
        def _roll(date):
            if not date or new_due is None:
                return None
            if self.due_date is not None:
                return new_due + (date - self.due_date)
            if defer_delta is not None:
                return date + defer_delta
            return date + (new_due - anchor)

        new_defer = _roll(self.defer_date)
        new_plan = _roll(self.plan_date)
        clone = Item(
            kind="task",
            owner=self.owner,
            assigned_to=self.assigned_to,
            title=self.title,
            note=self.note,
            color=self.color,
            parent=self.parent,
            tags=list(self.tags),
            status="next" if not self.defer_date else "scheduled",
            flagged=self.flagged,
            priority=self.priority,
            due_date=new_due,
            defer_date=new_defer,
            plan_date=new_plan,
            estimated_minutes=self.estimated_minutes,
            is_internal=self.is_internal,
            repeat_every_days=self.repeat_every_days,
            repeat_unit=self.repeat_unit,
            repeat_from_completion=self.repeat_from_completion,
            repeat_weekdays=list(self.repeat_weekdays or []),
            # Carry the checklist over but reset every item to unchecked —
            # a fresh occurrence starts with nothing ticked off.
            checklist=[ChecklistItem(text=ci.text, done=False) for ci in (self.checklist or [])],
        )
        clone.save()
        # Recreate the sub-tasks on the fresh occurrence (reset to open).
        if captured_subtasks:
            self._rebuild_subtasks(clone, captured_subtasks)
        # A live clone keeps the project non-empty, so this is a no-op here —
        # called for consistency in case the clone landed elsewhere.
        self._autocomplete_enclosing_project(by_user)
        return clone

    def _autocomplete_enclosing_project(self, by_user: User | None = None) -> None:
        """If this task sits in a project flagged ``auto_complete_when_done``
        and no open task remains anywhere in that project, mark the project
        ``completed``. Climbs through sections / parent tasks to find the
        enclosing project."""
        node = self.parent
        while node is not None and node.kind != "project":
            node = node.parent
        project = node
        if (project is None
                or not project.auto_complete_when_done
                or project.status in ("completed", "dropped")):
            return
        # Any still-open task anywhere in the subtree keeps the project active.
        frontier = [project.pk]
        while frontier:
            children = Item.objects(parent__in=frontier).only("pk", "kind", "status")
            next_frontier = []
            for c in children:
                if c.kind == "task" and c.status not in ("done", "dropped"):
                    return
                next_frontier.append(c.pk)
            frontier = next_frontier
        project.status = "completed"
        project.completed_at = _utcnow()
        project.completed_by = by_user
        project.completed_via = "auto"
        project.save()

    def ensure_container(self) -> bool:
        """Flip kind=task → project so this item can hold children.

        Idempotent. Returns True if the kind changed. Anything that adds
        a child item to a task (drag-merge, sub-task creation, importer
        pass 2) should call this so the parent renders as a real
        project, not a task with hidden descendants.
        """
        if self.kind != "task":
            return False
        self.kind = "project"
        if not self.project_type:
            self.project_type = "parallel"
        # Tasks use status open/next/etc., projects use active/on_hold/...
        if self.status not in ("active", "on_hold", "completed", "dropped"):
            self.status = "active"
        self.completed_at = None
        # Recurrence is a task concept — a project that still carries it would
        # spawn a clone (and look like "the repeating task turned into a
        # project") the next time it's completed. Drop it on the flip.
        self.repeat_every_days = None
        self.repeat_weekdays = []
        self.repeat_from_completion = False
        self.save()
        return True

    def add_comment(self, author: User, body: str) -> Comment:
        comment = Comment(author=author, body=body)
        self.comments.append(comment)
        self.save()
        return comment

    def mark_followed_up(self, by_user: User, note: str = "") -> None:
        """Log a follow-up and roll the next reminder forward.

        One click does the three things the follow-up workflow needs:
        stamp ``followup_last_at`` (answers "when did I last chase"), append a
        timestamped comment to the item's log, and advance ``followup_at`` by
        ``followup_every_days``. With no cadence set it's a one-shot follow-up,
        so the reminder is cleared and the item drops out of the Follow-up view.
        """
        now = _utcnow()
        self.followup_last_at = now
        body = "🔔 Followed up" + (f" — {note}" if note else "")
        self.comments.append(Comment(author=by_user, body=body))
        if self.followup_every_days:
            self.followup_at = now + timedelta(days=self.followup_every_days)
        else:
            self.followup_at = None
        self.save()

    @property
    def repeat_weekday_labels(self) -> list:
        """Short day names for ``repeat_weekdays`` in Mon→Sun order, e.g.
        ``['Mon', 'Wed', 'Fri']``. Empty when no weekday recurrence is set."""
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return [names[d] for d in sorted(self.repeat_weekdays or []) if 0 <= d <= 6]

    # ----- checklist helpers -----
    @property
    def checklist_done(self) -> int:
        return sum(1 for c in self.checklist if c.done)

    @property
    def checklist_progress(self) -> int:
        """Percentage of checklist items done (0-100). 0 when empty."""
        total = len(self.checklist)
        if not total:
            return 0
        return round(self.checklist_done * 100 / total)

    # ----- time tracking -----
    @property
    def tracked_minutes(self) -> int:
        """Total minutes logged across all time entries on this task."""
        return sum(e.minutes or 0 for e in (self.time_entries or []))

    def home_for(self, user):
        """The container this item appears under FOR ``user`` (``None`` = Inbox).

        For a task delegated TO ``user`` (assigned, not owned): their own
        filing wins (``assignee_parent``); else the canonical ``parent`` only
        if they can actually see it; else ``None`` so an untriaged delegated
        task defaults to the assignee's Inbox instead of hiding under the
        owner's invisible project. The owner (and everyone else) always sees
        the canonical ``parent``."""
        if self.assigned_to == user and self.owner != user:
            if self.assignee_parent is not None:
                return self.assignee_parent
            if self.parent is not None and self.parent.can_be_viewed_by(user):
                return self.parent
            return None
        return self.parent

    def set_home_for(self, user, container) -> None:
        """Place this item into ``container`` (``None`` = Inbox) from ``user``'s
        perspective. For a task delegated TO ``user`` (assigned, not owned) this
        writes their personal ``assignee_parent`` (which must be a container
        they own) and NEVER the owner's canonical ``parent`` — so it can't
        vanish from the owner's project. Otherwise it moves the canonical
        ``parent`` as usual. Raises ``ValueError`` on an invalid target."""
        if self.assigned_to == user and self.owner != user:
            if container is not None and container.owner != user:
                raise ValueError("file delegated tasks under your own projects")
            self.assignee_parent = container
        else:
            self.parent = container

    def assign_to(self, new_assignee, *, by_user=None):
        # Assigning a task back to its own owner is the same as unassigning it:
        # the owner already owns it, so there is no delegation to record.
        if new_assignee is not None and new_assignee == self.owner:
            new_assignee = None
        if new_assignee is None:
            self.assigned_to = None
            self.assigned_by = None
            self.assigned_at = None
            # No assignee → their personal filing is meaningless.
            self.assignee_parent = None
            return
        old = self.assigned_to
        # A different assignee inherits no personal filing from the previous one.
        if new_assignee != old:
            self.assignee_parent = None
        self.assigned_to = new_assignee
        self.assigned_by = by_user or self.owner
        self.assigned_at = _utcnow()
        # Inherit tags onto assignee's user.tags.
        if new_assignee != old and self.tags:
            try:
                new_assignee.ensure_tags(self.tags)
                new_assignee.save()
            except Exception:
                pass

    def can_be_viewed_by(self, user: User) -> bool:
        if self.owner == user:
            return True
        if self.assigned_to == user:
            return True
        if self.members and any(m == user for m in self.members):
            return True
        # Inherit visibility from parent project's members.
        if self.parent and self.parent.members and any(m == user for m in self.parent.members):
            return True
        return False

    def can_be_edited_by(self, user: User) -> bool:
        return self.can_be_viewed_by(user)

    def regenerate_share_token(self) -> str:
        import secrets
        self.share_token = secrets.token_urlsafe(24)
        return self.share_token

    # ----- review cadence (tasks + projects) -----

    def effective_review_interval(self) -> int | None:
        """Review cadence in days, honouring inheritance. Own value wins;
        otherwise inherit the nearest ancestor that sets one. ``0`` = never,
        ``None`` = no review configured anywhere up the chain."""
        node = self
        seen = set()
        while node is not None and node.pk not in seen:
            if node.review_interval_days is not None:
                return node.review_interval_days
            seen.add(node.pk)
            node = node.parent
        return None

    def is_review_due(self, now: datetime | None = None) -> bool:
        """True when this item is due for review per its effective interval.
        Never-reviewed items with a positive interval are due immediately."""
        now = now or _utcnow()
        iv = self.effective_review_interval()
        if not iv:  # None or 0 → not on a review schedule
            return False
        if self.last_reviewed_at is None:
            return True
        from datetime import timedelta
        return self.last_reviewed_at + timedelta(days=iv) <= now

    def _stamp_reviewed(self, now: datetime) -> None:
        """Set last/next review timestamps from the effective interval —
        without saving (caller saves)."""
        from datetime import timedelta
        self.last_reviewed_at = now
        iv = self.effective_review_interval()
        self.next_review_at = (now + timedelta(days=iv)) if iv else None

    def mark_reviewed(self, *, cascade: bool = True) -> None:
        """Stamp this item reviewed. When called on a container (project/
        folder/section) with ``cascade`` (default), also resets the review
        clock on every descendant task so one pass clears the project and
        its actions together."""
        now = _utcnow()
        self._stamp_reviewed(now)
        self.save()
        if cascade and self.kind != "task":
            frontier = [self.pk]
            while frontier:
                children = list(Item.objects(parent__in=frontier))
                next_frontier = []
                for c in children:
                    if c.kind == "task":
                        c._stamp_reviewed(now)
                        c.save()
                    next_frontier.append(c.pk)
                frontier = next_frontier

    def __str__(self) -> str:
        return self.title or "(untitled)"
