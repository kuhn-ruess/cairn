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
from datetime import datetime, timezone

from mongoengine import (
    BooleanField,
    DateTimeField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentListField,
    IntField,
    ListField,
    ReferenceField,
    StringField,
)

from application.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


ITEM_KINDS = ("task", "project", "section", "folder")
TASK_STATUS = ("open", "next", "waiting", "scheduled", "someday", "done", "dropped")
PROJECT_TYPES = ("parallel", "sequential", "single_actions")
PROJECT_STATUS = ("active", "on_hold", "completed", "dropped")
PRIORITIES = (1, 2, 3, 4)
REPEAT_UNITS = ("day", "weekday", "weekend", "week", "month", "year")


class Comment(EmbeddedDocument):
    author = ReferenceField(User, required=True)
    body = StringField(required=True)
    created_at = DateTimeField(default=_utcnow)
    is_public = BooleanField(default=False)


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
            ("owner", "kind"),
            ("owner", "status"),
            ("assigned_to", "status"),
        ],
        "ordering": ["+sort_order", "+created_at"],
    }

    # discriminator
    kind = StringField(choices=ITEM_KINDS, default="task")

    # ownership & hierarchy
    owner = ReferenceField(User, required=True)
    parent = ReferenceField("Item")
    sort_order = IntField(default=0)

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
    completed_at = DateTimeField()
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
    # Recurrence — back-compat layout:
    #   ``repeat_every_days`` was the only knob in the old schema. It
    #   stays the "how many" counter. ``repeat_unit`` says what one
    #   unit means (day/week/month/year). Together they describe e.g.
    #   "every 2 weeks". Months / years use dateutil.relativedelta so
    #   calendar arithmetic stays honest.
    repeat_every_days = IntField()
    repeat_unit = StringField(choices=REPEAT_UNITS, default="day")
    repeat_from_completion = BooleanField(default=False)
    is_internal = BooleanField(default=False)

    # collaboration / assignment
    assigned_to = ReferenceField(User)
    assigned_by = ReferenceField(User)
    assigned_at = DateTimeField()
    members = ListField(ReferenceField(User))  # for project-like items

    # comments live with the item
    comments = EmbeddedDocumentListField(Comment)

    # ----- project-flavoured fields -----
    project_type = StringField(choices=PROJECT_TYPES, default="parallel")
    review_interval_days = IntField(default=7, min_value=0)
    last_reviewed_at = DateTimeField()
    next_review_at = DateTimeField()
    task_grouping = StringField(default="section")
    task_grouping_tag = StringField(default="")
    view_mode = StringField(default="list")
    show_blocked_tasks = BooleanField(default=False)

    # public share
    share_enabled = BooleanField(default=False)
    share_token = StringField(unique=True, sparse=True)

    # calendar write-back
    gcal_due_event_id = StringField()
    gcal_plan_event_id = StringField()

    # ---------- helpers ----------

    def save(self, *args, **kwargs):
        self.updated_at = _utcnow()
        # Stamp completed_at when a task transitions to done/dropped,
        # clear on reopen. Project lifecycle (active/on_hold/completed/
        # dropped) doesn't go through this path.
        if self.kind == "task":
            if self.status in ("done", "dropped"):
                if not self.completed_at:
                    self.completed_at = _utcnow()
            else:
                self.completed_at = None
        return super().save(*args, **kwargs)

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
        self.status = "done"
        self.completed_at = _utcnow()
        self.save()
        if not self.repeat_every_days:
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
        if anchor:
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
                defer_delta = None  # weekday math doesn't translate to defer cleanly
            elif unit == "weekend":
                new_due = _advance_business(anchor, n, {5, 6})
                defer_delta = None
            else:  # day
                new_due = anchor + relativedelta(days=n)
                defer_delta = relativedelta(days=n)
        new_defer = (
            (self.defer_date + defer_delta)
            if (self.defer_date and defer_delta is not None) else None
        )
        clone = Item(
            kind="task",
            owner=self.owner,
            assigned_to=self.assigned_to,
            title=self.title,
            note=self.note,
            parent=self.parent,
            tags=list(self.tags),
            status="next" if not self.defer_date else "scheduled",
            flagged=self.flagged,
            priority=self.priority,
            due_date=new_due,
            defer_date=new_defer,
            estimated_minutes=self.estimated_minutes,
            repeat_every_days=self.repeat_every_days,
            repeat_unit=self.repeat_unit,
            repeat_from_completion=self.repeat_from_completion,
        )
        clone.save()
        return clone

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
        self.save()
        return True

    def add_comment(self, author: User, body: str) -> Comment:
        comment = Comment(author=author, body=body)
        self.comments.append(comment)
        self.save()
        return comment

    def assign_to(self, new_assignee, *, by_user=None):
        if new_assignee is None:
            self.assigned_to = None
            self.assigned_by = None
            self.assigned_at = None
            return
        old = self.assigned_to
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

    def mark_reviewed(self) -> None:
        from datetime import timedelta
        now = _utcnow()
        self.last_reviewed_at = now
        if self.review_interval_days:
            self.next_review_at = now + timedelta(days=self.review_interval_days)
        else:
            self.next_review_at = None
        self.save()

    def __str__(self) -> str:
        return self.title or "(untitled)"
