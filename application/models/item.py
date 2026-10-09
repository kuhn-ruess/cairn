"""Unified ``Item`` model — one document covers tasks, projects,
sections, and folders.

``kind`` discriminates behaviour:
* ``task``    — actionable to-do. Can also have child items (= a project
                in OmniFocus terms).
* ``project`` — a container for tasks; behaves like a task that has
                children + project metadata (status, review, members).
* ``section`` — inline grouping inside a project. Holds child tasks.
* ``folder``  — organisational container; holds projects and folders, and
                MAY hold tasks directly. The app has always offered that —
                the project picker, the move palette, drag-and-drop onto a
                folder row, "+ Task" on a folder page and a folder's CalDAV
                list all file a task there — so the rule is written the way
                the product behaves rather than the other way round. What a
                folder does not have is a project's execution semantics
                (``project_type``, completion); its tasks are parallel and
                are reviewed with the folder (see ``views.review``).
                ``folder_domain`` splits folders into two parallel trees:
                ``tasks`` (the project navigator) and ``notes`` (the notes
                navigator) so the two don't bleed into each other.
* ``note``    — a standalone Markdown note. Lives under a ``notes`` folder
                (or at the top level) and links to tasks (via ``related``)
                and calendar events (via ``linked_events``).

Hierarchy is expressed via ``parent`` (self-reference). Any item can
have a parent of any kind, though only certain combos are meaningful:

* task   → parent ∈ {task, project, section, folder}  (sub-task or list
                                                item; ``None`` = Inbox)
* project → parent ∈ {project, folder}         (sub-project)
* section → parent ∈ {project}                 (always inside a project)
* folder → parent ∈ {folder, project}          (nested folders; also allowed
                                                inside a project to group its
                                                sub-projects)

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


# Stored-document size thresholds. MongoDB hard-caps a document at 16 MB; note
# images and file attachments live inline on the item, so the whole doc must
# stay under that. These drive both the upload guard (views.tasks/notes) and
# the storage meter shown on task/project/note detail pages.
ITEM_DOC_CAP_BYTES = 16 * 1024 * 1024   # MongoDB's hard per-document limit
ITEM_WARN_BYTES = 10 * 1024 * 1024      # warn the user past this
ITEM_MAX_BYTES = 15 * 1024 * 1024       # refuse new attachments past this

ITEM_KINDS = ("task", "project", "section", "folder", "note")
TASK_STATUS = ("open", "next", "waiting", "waiting_reply", "review",
               "scheduled", "someday", "done", "dropped")
# Statuses that make a task non-actionable by definition: you're waiting on
# someone else ("waiting" / "waiting_reply") or you've parked it ("someday").
# They feed ``_own_block`` — so such tasks read as blocked everywhere
# (available-only filters, Next, plan backlog) and hold their sub-tasks back
# too. They still show on their own perspectives, which key off status.
BLOCKED_STATUS = ("waiting", "waiting_reply", "someday")
PROJECT_TYPES = ("parallel", "sequential", "single_actions")
PROJECT_STATUS = ("active", "on_hold", "completed", "dropped")
PRIORITIES = (1, 2, 3, 4)
# Review cadence for a container nobody configured one for. A project created
# through the inspector says 0 when the user means "never"; ``None`` is not
# that answer, it is nobody having been asked.
DEFAULT_REVIEW_INTERVAL_DAYS = 7
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


class NoteFile(EmbeddedDocument):
    """An arbitrary file dragged/pasted onto a note, stored inline on the item
    (same model as NoteImage, never on the filesystem). The note references it
    by ``token`` via ``/tasks/<id>/note-file/<token>`` as a download link.
    Served inline only for raster images, PDF and plain text, as a download
    for every other type (``views.tasks._INLINE_TYPES``)."""
    token = StringField(required=True)
    filename = StringField(required=True)
    content_type = StringField(default="application/octet-stream")
    data = BinaryField(required=True)
    created_at = DateTimeField(default=_utcnow)


class LinkedEvent(EmbeddedDocument):
    """A calendar event linked to a note. Calendar events have no DB document
    in Cairn — only a ``uid`` plus the owning calendar — so we snapshot just
    enough to render the chip (title/start) without a live fetch, and keep
    ``uid`` + ``calendar_pk`` so the link can re-resolve / deep-link later."""
    uid = StringField(required=True)
    calendar_pk = StringField()      # ExternalCalendar.pk_ref, as a string
    title = StringField(default="")
    start = DateTimeField()
    all_day = BooleanField(default=False)
    url = StringField()              # provider's html link, when there is one


class PersonalHome(EmbeddedDocument):
    """One viewer's PERSONAL filing of a delegated task under a container they
    own (``None`` container = their Inbox). Keyed by ``user``, so it's fully
    independent per participant: in a delegation chain (A→B→C) each person who
    can see the task but not its canonical ``parent`` files it under their own
    project without touching anyone else's placement or the owner's ``parent``.

    Replaces the single-slot ``assignee_parent``, whose one slot belonged to the
    current assignee and got wiped on every (re)assign — so B lost their filing
    the moment they handed the task to C, and a B→C→B round trip couldn't restore
    it. Per-user entries survive re-assignment and round trips untouched."""
    user = ReferenceField(User, required=True)
    container = ReferenceField("Item")


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
            "personal_homes.user",
            "personal_homes.container",
            "shared_teams",
            "next_review_at",
            "trashed_at",
            "depends_on",
            "note_links",
            "block_ids",
            ("owner", "kind"),
            ("owner", "status"),
            # Template library items are hidden from every normal list by the
            # ``objects`` manager; the templates view filters owner+template.
            ("owner", "template"),
            ("assigned_to", "status"),
            # Today/Plan/forecast filter by owner+kind on a date field.
            ("owner", "kind", "plan_date"),
            ("owner", "kind", "due_date"),
            # Project detail / sequential-blocking walk children by parent.
            ("owner", "kind", "parent"),
            # Reverse lookup: notes linked to a calendar event (day/plan view).
            ("owner", "kind", "linked_events.uid"),
            # Reverse lookup: which projects/folders point at a notes folder.
            "notes_folder",
        ],
        "ordering": ["+sort_order", "+created_at"],
    }

    # discriminator
    kind = StringField(choices=ITEM_KINDS, default="task")
    # For ``folder`` items only: which navigator the folder belongs to —
    # ``tasks`` (the project tree) or ``notes`` (the notes tree). Lets the two
    # folder hierarchies stay separate without a second kind. Missing/absent
    # counts as ``tasks`` (every pre-notes folder is a project folder).
    folder_domain = StringField(choices=("tasks", "notes"), default="tasks")

    # ownership & hierarchy
    owner = ReferenceField(User, required=True)
    parent = ReferenceField("Item")
    # Per-user "second home": when a task is delegated to someone who can't see
    # its ``parent`` project, they may file it under one of THEIR OWN projects/
    # folders. This never touches ``parent`` (the owner's canonical placement),
    # so the task stays put for the owner and only that user's views group it
    # here. One entry per participant, independent of who is currently assigned —
    # so a delegation chain (A→B→C) and B→C→B round trips keep every filing
    # intact. Each entry's container is owned by that entry's ``user``. See
    # ``home_for`` / ``set_home_for`` / ``PersonalHome``.
    personal_homes = EmbeddedDocumentListField(PersonalHome)
    sort_order = IntField(default=0)
    # Template-library flag. A ``template`` item (and its whole subtree —
    # folders/projects/tasks/sub-tasks) lives in the Templates area and is
    # hidden from EVERY normal list/perspective/navigator by the ``objects``
    # manager below, exactly like ``trashed_at``. "Using" a template deep-copies
    # its subtree into real items (template=False) — see
    # ``application/services/templates.py``. Template folders reuse ``kind``
    # ``folder``; organisation inside the area is by these folders.
    template = BooleanField(default=False)
    # Loose, symmetric "see also" links between items. Purely a reference
    # surfaced in the inspector — deliberately NOT a hierarchy: no query
    # anywhere filters/groups by ``related``, so it never affects lists,
    # perspectives, the tree, or completion. Both sides carry the link
    # (see ``relate_to`` / ``unrelate_from``).
    related = ListField(ReferenceField("Item"))
    # Directed, BLOCKING task dependencies: the tasks that must be completed
    # before THIS task becomes actionable. Unlike ``related`` (symmetric, no
    # effect on availability), this is a one-way prerequisite edge that feeds
    # ``is_blocked`` (see ``_own_block``): a task with any open dependency is
    # blocked — drops out of Next/Available, greyed in lists — until every
    # dependency is done/dropped. The edge is stored only on the dependent
    # side; the reverse ("what does this task block") is derived by query
    # (``dependents``). Read-time blocking, so completing a dependency frees
    # its dependents automatically with nothing stored on their side. A cycle
    # guard on add keeps the graph a DAG (see ``add_dependency``).
    depends_on = ListField(ReferenceField("Item"))

    # common content
    title = StringField(required=True, max_length=500)
    note = StringField(default="")
    # Which of the two editors this body belongs in: an OUTLINE (bullets, the
    # outliner) or a plain MARKDOWN document (prose, written here or in any
    # Markdown editor and stored exactly as typed). Empty means nobody has
    # said, and then the text answers for itself — see
    # ``services.outline.format_of``, which is the only place that decides.
    # It is also a front-matter field in the mirrored file (``format:``), so
    # the choice can be made from the outside.
    note_format = StringField(choices=("", "outline", "markdown"), default="")
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
    # Why it is in the trash, written by ``services.trash.trash_item`` and by
    # nothing else: ``trashed_source`` is the channel (a key of
    # ``trash.SOURCES``, what the trash page groups and filters by),
    # ``trashed_via`` the readable detail ("Web: shortcut d", "API (token
    # cli)"), ``trashed_by`` the acting account when there is one and
    # ``trashed_reason`` what the user typed into the confirm dialog.
    trashed_source = StringField()
    trashed_via = StringField()
    trashed_by = ReferenceField(User)
    trashed_reason = StringField()
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
    # Who owns this over there, as of the last sync (a Zammad user id). The
    # import files a ticket under the Cairn account its Zammad owner maps to,
    # and that mapping can arrive LATER than the ticket — an agent nobody had
    # matched yet, a hand-over between two runs. Keeping the upstream owner on
    # the task is what lets a later run put the assignment right without asking
    # the ticket again: see ``zammad_team._reconcile_assignments``.
    external_owner = StringField()
    # The ticket's workflow status over there ("In Arbeit", "Zugewiesen"), as
    # of the last import — shown beside the source chip. Deliberately apart
    # from ``status``: a re-import refreshes this and leaves the user's own
    # status alone (see ``views.projects.import_csv``).
    external_status = StringField()
    # Human-readable origin (``todoist`` / ``zammad`` / ``csv`` /
    # ``jira`` / ``helix`` / …). Importers fill this so the UI can show a "source"
    # chip without parsing the ``external_id`` prefix.
    source = StringField()

    # ----- task-flavoured fields (also valid on project/section) -----
    status = StringField(default="open")  # values vary by kind
    flagged = BooleanField(default=False)
    priority = IntField(choices=PRIORITIES)
    defer_date = DateTimeField()
    due_date = DateTimeField()
    plan_date = DateTimeField()
    # Short quick-plan comment set alongside ``plan_date`` from the inspector's
    # quick-plan block. Rendered before the task name in list views
    # ("comment (Task name)") so a planned item reads as an intention.
    plan_note = StringField(default="", max_length=200)
    # Gantt / timeline scheduling window ("Target start" / "Target end"). Kept
    # independent of ``due_date`` (a hard deadline) and ``defer_date`` (which
    # hides a task until it passes): these two are pure planning bounds that the
    # Gantt view lays a bar between. Either can be set alone. See
    # application/services/gantt.py.
    start_date = DateTimeField()
    end_date = DateTimeField()
    estimated_minutes = IntField()
    # Free-text location (e.g. "REWE, Hauptstraße 1"). Round-trips with the
    # iCalendar LOCATION property so a CalDAV client's location reminder
    # survives. The geofence (radius/coords) from Apple's structured location
    # is not preserved — only the human-readable place.
    location = StringField()
    # A link the task points AT, as a CalDAV client sends it in the VTODO's
    # ``URL`` property: a reminder made from Apple Mail carries the mail as
    # ``message://%3C<message-id>%3E`` there. Stored as sent (any scheme) so the
    # round trip hands the client back exactly what it gave; the templates only
    # make it clickable through the ``safe_link`` filter.
    url = StringField()
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
    # Fixed day-of-month recurrence (e.g. "the 2nd of every month"). When set,
    # the task repeats monthly — every ``repeat_every_days`` months (default 1)
    # — landing on this calendar day, clamped to the month's length (so 31 lands
    # on the last day of a short month). Takes precedence over the every-N-units
    # schedule, like ``repeat_weekdays``; the two are mutually exclusive in the
    # UI. Empty = not a monthly-on-fixed-day schedule.
    repeat_monthday = IntField(min_value=1, max_value=31)
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
    # Share with whole teams. Inherited down the tree; a descendant that sets its
    # OWN share (members/shared_teams) overrides the inherited one — see
    # can_be_viewed_by. String ref avoids a circular import with Team.
    shared_teams = ListField(ReferenceField("Team"))

    # comments live with the item
    comments = EmbeddedDocumentListField(Comment)

    # simple checklist (checkbox + text) on a task
    checklist = EmbeddedDocumentListField(ChecklistItem)

    # compressed images pasted into the note, stored inline (see NoteImage)
    note_images = EmbeddedDocumentListField(NoteImage)

    # arbitrary files dragged/pasted onto the note, stored inline (see NoteFile)
    note_files = EmbeddedDocumentListField(NoteFile)

    # Calendar events linked to a ``note`` item (see LinkedEvent). Tasks are
    # linked via the symmetric ``related`` field instead, so they back-link
    # automatically from the task side too.
    linked_events = EmbeddedDocumentListField(LinkedEvent)

    # ---- Markdown mirror (services/notes_sync.py) ----
    # Where this note's file lives in the mirrored folder, the hash of what was
    # written there, and the server's change validator at that moment. The hash
    # is the common ancestor a two-way round compares both sides against; empty
    # ``mirror_path`` means "not mirrored yet".
    mirror_path = StringField()
    mirror_hash = StringField()
    mirror_tag = StringField()

    # Note-to-note wiki links: the notes this note's body references via
    # ``[[Title]]``. DIRECTED and derived — rebuilt from the body on every
    # save (services/note_links.py), never edited by hand. Backlinks
    # ("linked mentions") are the reverse query ``Item.objects(note_links=x)``,
    # which is why this is stored instead of scanning bodies with a regex.
    note_links = ListField(ReferenceField("Item"))

    # Every ``id::`` in this note's outline. Derived, like ``note_links``:
    # rebuilt from the body on save (services/block_refs.py), never edited by
    # hand. It exists so that resolving the ``((uuid))`` references on a page
    # is ONE indexed query over the notes that hold them, instead of parsing
    # every body in the collection to find out who owns a block.
    block_ids = ListField(StringField())

    # Does the title follow the first block? True while nobody has given the
    # note a name of its own — you write the first line and that IS the name,
    # in the tree, in search and in the mirrored file. Typing in the title
    # field turns this off (the name is yours now); emptying that field turns
    # it back on. See views.notes.edit.
    title_auto = BooleanField(default=False)

    # ----- project-flavoured fields -----
    project_type = StringField(choices=PROJECT_TYPES, default="parallel")
    # Review cadence in days. Applies to tasks AND projects. ``None`` =
    # inherit the nearest ancestor's interval (see effective_review_interval);
    # an explicit value overrides; ``0`` = never review. Projects normally set
    # their own; tasks usually leave it blank to follow their project.
    # A FOLDER's value is not a user decision — the inspector has no field for
    # it and the save path writes 0 — so it is skipped when reading a cadence.
    review_interval_days = IntField(min_value=0)
    last_reviewed_at = DateTimeField()
    next_review_at = DateTimeField()
    task_grouping = StringField(default="section")
    task_grouping_tag = StringField(default="")
    view_mode = StringField(default="list")
    show_blocked_tasks = BooleanField(default=False)
    # Per-project "available only" filter: when set, the whole project page
    # drops blocked tasks (deferred / ancestor-deferred / follow-up-pending /
    # sequentially-blocked). Available for ANY project, not just sequential.
    show_available_only = BooleanField(default=False)
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

    # Team time-tracking link. On a folder/project this binds the GTD subtree to
    # a billing project; tasks inherit it by climbing parents (see
    # application.services.timetracking.resolve_time_project). Stored as a string
    # ref so the dependency points one way (timetracking → item, never back).
    time_project = ReferenceField("TimeTrackingProject")

    # Whose work this is. A folder/project names its customer directly and the
    # whole subtree below inherits it — the same climb ``time_project`` gets
    # (application.services.customers.resolve_customer,
    # application.services.containers.customer_labels).
    #
    # It cannot drift from the billing binding, because it is not allowed to:
    # a container bound to a ``time_project`` takes that project's customer on
    # save (``_follow_billing_customer``, whatever set the binding). So there
    # is one truth — "Acme" — reachable either by naming the customer on a
    # folder that bills nowhere, or by binding the folder to one of Acme's
    # billing projects.
    customer = ReferenceField("Customer")

    # Notes link. On a project/folder this points at a folder in the *notes*
    # tree (``kind="folder"``, ``folder_domain="notes"``), so everything below
    # gets a one-click jump into its notes — tasks inherit it by climbing
    # parents, exactly like ``time_project``
    # (application.services.containers.resolve_notes_folder). One direction
    # only: the notes side is found with a reverse query, so there is no pair
    # of links that can drift apart.
    notes_folder = ReferenceField("Item")

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
        # Hide trashed AND template-library items from every default query, so
        # the ~30 list/perspective/navigator sites need no changes. The
        # Templates view uses ``all_objects`` (owner + template=True) instead.
        return queryset.filter(trashed_at=None, template__ne=True)

    @queryset_manager
    def all_objects(doc_cls, queryset):
        return queryset

    # ---------- helpers ----------

    def _prune_orphan_attachments(self, grace_seconds: int = 300) -> int:
        """Drop inline note images/files whose token is no longer referenced by
        any of this item's text (note body + comment bodies), reclaiming the
        BSON document space. Returns how many blobs were removed.

        A freshly-created blob (younger than ``grace_seconds``) is always kept:
        the paste endpoint stores the image BEFORE its Markdown link exists in
        the editor, so a save that races the client inserting the link (paste
        round-trip / debounced autosave) must not reap it. Called from ``save``
        only when ``note``/``comments`` changed."""
        imgs = self.note_images or []
        files = self.note_files or []
        if not imgs and not files:
            return 0
        haystack = self.note or ""
        for c in (self.comments or []):
            haystack += "\n" + (c.body or "")
        now = _utcnow()

        def keep(blob) -> bool:
            if blob.token and blob.token in haystack:
                return True
            ca = blob.created_at
            if ca is not None:
                if ca.tzinfo is None:
                    ca = ca.replace(tzinfo=timezone.utc)
                if (now - ca).total_seconds() < grace_seconds:
                    return True
            return False

        kept_imgs = [i for i in imgs if keep(i)]
        kept_files = [f for f in files if keep(f)]
        removed = (len(imgs) - len(kept_imgs)) + (len(files) - len(kept_files))
        if removed:
            self.note_images = kept_imgs
            self.note_files = kept_files
        return removed

    def _follow_billing_customer(self) -> None:
        """A bound container's customer IS its billing project's customer.

        Here rather than in the form that happened to be open, because the
        binding is set from four places — the inspector, the link button on a
        billing project's page, filing unassigned time, the Zammad import — and
        only one of them went through a save path that knew about the customer.
        A folder bound to Acme's project therefore *says* Acme, whoever bound
        it; "the folder says Acme while its hours invoice to Globex" stays
        unreachable, and picking a customer by hand on a bound container is a
        contradiction the binding wins.

        An ARCHIVED billing project says nothing, the same rule
        ``resolve_time_project`` and ``customer_labels`` follow: it is out of
        circulation, so it neither answers "whose work is this" nor overwrites
        an answer given by hand.

        Only when one of the two fields was actually touched, so an ordinary
        save costs no dereference."""
        if self._data.get("time_project") is None:
            return
        touched = (["time_project"] if self.id is None
                   else self._get_changed_fields())
        if "time_project" not in touched and "customer" not in touched:
            return
        try:
            tp = self.time_project
            if tp.archived or tp.customer.archived:
                return
            self.customer = tp.customer
        except Exception:      # a binding whose project or customer is gone
            pass

    def save(self, *args, **kwargs):
        self.updated_at = _utcnow()
        self._follow_billing_customer()
        # Reclaim space from inline images/files whose link was removed from the
        # note/comments. Guarded to the saves that actually change that text so
        # it never scans on unrelated writes; the grace window inside protects a
        # just-pasted blob (see _prune_orphan_attachments).
        if self.id is not None:
            _changed = self._get_changed_fields()
            if "note" in _changed or "comments" in _changed:
                self._prune_orphan_attachments()
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
            # A task that becomes actionable again (next/open) must not stay
            # hidden behind a pending follow-up: a future ``followup_at`` blocks
            # the task (see ``_own_block``), so e.g. when a customer replies and
            # a Zammad sync flips ``waiting`` → ``open``, the leftover reminder
            # would keep it invisible. Drop the pending follow-up on that
            # transition (only on the actual status change, so a follow-up set
            # by hand on an already-next task is left alone).
            if (self.status in ("next", "open")
                    and self.followup_at is not None
                    and self.id is not None
                    and "status" in self._get_changed_fields()):
                # Through set_followup, so the defer date that was only
                # mirroring this follow-up goes with it — otherwise the task
                # stays hidden with nothing left to say why.
                self.set_followup(None)
        result = super().save(*args, **kwargs)
        if cascade_status:
            self._cascade_close(cascade_status)
        # An assignment made before the first save had no pk to link to —
        # now it does (see _notify_assignee).
        if getattr(self, "_pending_assign_notify", False):
            self._pending_assign_notify = False
            self._notify_assignee()
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

    def set_container_completion(self, done: bool, *, by=None, via="user"):
        """Complete or reopen a container (project / folder / section).

        Container lifecycle (active/on_hold/completed/dropped) does NOT go
        through the task ``save()`` completion path — that only stamps
        ``kind='task'`` — so record the completion bookkeeping here instead
        of setting ``status`` by hand. Mirrors ``mark_done`` for tasks and
        the auto-complete path (``_autocomplete_enclosing_project``)."""
        if done:
            self.status = "completed"
            if not self.completed_at:
                self.completed_at = _utcnow()
            self.completed_by = by
            self.completed_via = via
        else:
            self.status = "active"
            self.completed_at = None
            self.completed_by = None
            self.completed_via = None
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

    def related_for(self, user):
        """``related_items`` as ``user`` may see them: a linked NOTE they can't
        open is dropped (a task shared with a team must not show the others
        its owner's private notes, not even by title). Their own notes pass on
        the raw owner id, without a deref; only someone else's note goes
        through ``can_be_viewed_by``. Other kinds are left as they were."""
        uid = str(user.pk)
        out = []
        for r in self.related_items():
            if r.kind == "note":
                o = r._data.get("owner")
                oid = getattr(o, "id", None) or getattr(o, "pk", None)
                if str(oid) != uid and not r.can_be_viewed_by(user):
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

    # ----- task dependencies (directed, blocking) -----

    def dependency_items(self):
        """All resolved dependency tasks for the inspector (done ones too),
        skipping dangling refs (target purged → bare DBRef) and trashed
        targets (reference deref bypasses the ``objects`` manager)."""
        out = []
        for d in self.depends_on or []:
            if not isinstance(d, Item) or d.trashed_at:
                continue
            out.append(d)
        return out

    def blocking_dependencies(self):
        """Resolved dependency tasks that are still OPEN — the ones currently
        blocking this task. Short-circuits to ``[]`` with NO query when the
        task has no dependencies (reads the raw refs from ``_data`` so a plain
        blocked-check down a list never fires a per-row dereference)."""
        if not self._data.get("depends_on"):
            return []
        return [d for d in self.dependency_items()
                if d.status not in ("done", "dropped")]

    def dependents(self):
        """Tasks that depend on THIS one (reverse edge, derived by query).
        These become available once this task is completed."""
        return list(Item.objects(depends_on=self, kind="task")
                    .order_by("+sort_order", "+created_at"))

    def _dependency_cycle_with(self, other) -> bool:
        """True if making self depend on ``other`` would create a cycle — i.e.
        ``other`` already (transitively) depends on self. BFS over depends_on
        edges; depth-capped as a guard. Keeps the dependency graph a DAG so
        blocking can never deadlock and the graph view stays layerable."""
        target = self.pk
        frontier, seen, depth = [other], set(), 0
        while frontier and depth < 200:
            nxt = []
            for node in frontier:
                if not isinstance(node, Item):
                    continue
                if node.pk == target:
                    return True
                if node.pk in seen:
                    continue
                seen.add(node.pk)
                nxt.extend(node.depends_on or [])
            frontier, depth = nxt, depth + 1
        return False

    def add_dependency(self, other) -> bool:
        """Make this task depend on ``other`` (one-way, blocking). No-op —
        returns False — for a self-edge, an existing edge, or one that would
        create a cycle. Stored only on this side; the reverse is by query."""
        if other is None or other.pk == self.pk:
            return False
        if any(getattr(d, "pk", None) == other.pk for d in self.depends_on or []):
            return False
        if self._dependency_cycle_with(other):
            return False
        self.depends_on.append(other)
        self.save()
        return True

    def remove_dependency(self, other_pk) -> None:
        """Drop the dependency on ``other_pk`` (also strips any dangling
        DBRefs left by a purged target)."""
        self.depends_on = [d for d in self.depends_on or []
                           if isinstance(d, Item) and str(d.pk) != str(other_pk)]
        self.save()

    def zammad_ticket_id(self) -> str:
        """Zammad's own ticket id for this item, or ``""`` when it has none.

        The single answer to "is this a Zammad ticket, and which one" — asked
        by the template that offers the inspector's refresh button and by the
        service behind it, so the two can't disagree about what counts.
        ``external_ref`` is the human Ticket#; the id that addresses the API
        is the one stamped into ``external_id``.
        """
        if self.source != "zammad":
            return ""
        ext = self.external_id or ""
        if not ext.startswith("zammad:"):
            return ""
        return ext.split(":", 1)[1].strip()

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

    @property
    def is_note(self) -> bool:
        return self.kind == "note"

    @property
    def is_shared(self) -> bool:
        """Does this item carry a share of its OWN — members or teams?

        Read off the raw values: ``members``/``shared_teams`` are reference
        lists, and touching them dereferences every user on the way past. A
        list that only wants to know *whether* something is shared must not
        pay a query per row for it.
        """
        return bool(self._data.get("members") or self._data.get("shared_teams"))

    # ----- task behaviour helpers (no-op for non-tasks) -----

    def is_available(self, now: datetime | None = None) -> bool:
        return not self.is_blocked(now)

    def _own_block(self, now: datetime) -> bool:
        """This item's OWN blocked state, ignoring ancestors:
          * ``status`` in ``BLOCKED_STATUS`` — parked on someone/something else
          * ``defer_date`` in the future
          * ``followup_at`` in the future
          * has an unfinished task dependency (``depends_on``)
          * sits in a sequential parent behind an earlier incomplete sibling

        A FOLDER blocks nothing, and neither does being inside one. A folder
        carries no execution semantics (see the module docstring), and the
        fields that would say otherwise are not the user's: the inspector shows
        no defer date and no type for a folder and writes both back empty, so a
        value there can only have come from the API or an import. Honouring it
        held every task in the folder back — invisible in the available-only
        views and dropped from the CalDAV export — with nothing on screen that
        could explain why, let alone undo it.
        """
        if self.kind == "folder":
            return False
        if self.status in BLOCKED_STATUS:
            return True
        if self.defer_date and self.defer_date > now:
            return True
        if self.followup_at and self.followup_at > now:
            return True
        if self.blocking_dependencies():
            return True
        if (self.parent
                and self.parent.kind in ("project", "section")
                and self.parent.project_type == "sequential"):
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

    def is_blocked(self, now: datetime | None = None) -> bool:
        """Blocked = displayed but not actionable yet.

        A subtask of a blocked task is blocked too: we walk from this item up
        the parent chain (tasks → section → project → folder) and report
        blocked if THIS item or ANY ancestor is blocked for any reason
        (waiting_reply / future defer / future follow-up / sequential). So
        deferring, waiting-on or sequentially-gating a parent holds its whole
        subtree back — read-time, nothing stored on the children. The
        waiting-reply / follow-up views key off status / followup_at directly,
        so blocked items still appear there. Depth-capped as a cycle guard;
        mongoengine caches each parent deref, and the per-node sequential query
        only fires where a node's parent is actually sequential.
        """
        now = now or _utcnow()
        node, depth = self, 0
        while isinstance(node, Item) and depth < 50:
            if node._own_block(now):
                return True
            node = node.parent
            depth += 1
        return False

    def complete(self, by_user: User | None = None) -> "Item | None":
        from datetime import timedelta as _td
        from dateutil.relativedelta import relativedelta
        # Snapshot the sub-task tree BEFORE closing — the save() below cascades
        # them to done, so a recurring task needs the structure captured first
        # to recreate it fresh on the next occurrence.
        recurring = self.kind == "task" and bool(
            self.repeat_every_days or self.repeat_weekdays or self.repeat_monthday
        )
        captured_subtasks = self._capture_subtasks() if recurring else None
        self.status = "done"
        self.completed_at = _utcnow()
        self.completed_by = by_user
        self.completed_via = "user"
        self.save()  # cascades: closes this task's sub-tasks too
        # If this was delegated, let the delegator know it's done.
        from application.services.notifications import notify_completed
        notify_completed(self, by_user=by_user)
        # Recurrence only makes sense for actual tasks. A project/folder/
        # section never spawns a clone (defensive — ensure_container already
        # strips recurrence on the flip).
        if self.kind != "task":
            return None
        if (not self.repeat_every_days and not self.repeat_weekdays
                and not self.repeat_monthday):
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
        # Fixed day-of-month recurrence (e.g. "the 2nd of every month") takes
        # precedence over both weekday and every-N-units schedules: advance
        # ``n`` months (default 1) from the anchor and land on the target day,
        # clamped to the destination month's length so day 31 falls on the last
        # day of a short month.
        if self.repeat_monthday and anchor:
            import calendar as _cal
            step = n if (n and n > 0) else 1
            base = anchor + relativedelta(months=step)
            last = _cal.monthrange(base.year, base.month)[1]
            new_due = base.replace(day=min(self.repeat_monthday, last))
            defer_delta = relativedelta(months=step)
        # Specific-weekday recurrence (e.g. every Mon/Wed/Fri) takes
        # precedence over the every-N-units schedule and lands on the next
        # selected weekday strictly after the anchor.
        elif self.repeat_weekdays and anchor:
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
            repeat_monthday=self.repeat_monthday,
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
        self.repeat_monthday = None
        self.repeat_from_completion = False
        self.save()
        return True

    def add_comment(self, author: User, body: str) -> Comment:
        comment = Comment(author=author, body=body)
        self.comments.append(comment)
        self.save()
        return comment

    def set_followup(self, when) -> None:
        """Set the next follow-up, and let the defer date come along.

        A follow-up is a decision that there is nothing to do until then — you
        are chasing someone, and the answer isn't due yet. Leaving the task
        actionable in the meantime means seeing it in Next every day and
        deciding "not yet" every time, which is the work the defer date exists
        to remove. So the two move together.

        Only where the defer date isn't saying something else:

        * empty — nothing to lose,
        * equal to the follow-up it had — this coupling put it there, so it
          follows the follow-up wherever it goes, including to nothing,
        * already in the past — it stopped deferring anything a while ago.

        A defer date set to a *different future* day is somebody's decision
        ("start this on Monday whatever else happens") and is left alone.
        """
        def _same_moment(a, b):
            # To the minute: a date the form round-trips comes back without
            # seconds (``datetime-local``), so an exact match would say "the
            # user changed this" about a value nobody touched.
            return (a is not None and b is not None
                    and abs((a - b).total_seconds()) < 60)

        old = self.followup_at
        tracked = _same_moment(self.defer_date, old)
        stale = self.defer_date is not None and self.defer_date <= _utcnow()
        free = self.defer_date is None or tracked or stale
        self.followup_at = when
        if when is not None:
            if free:
                self.defer_date = when
        elif tracked:
            # The follow-up is gone; the defer date that was only mirroring it
            # would otherwise keep the task hidden for no stated reason.
            self.defer_date = None

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
        # Rolling the reminder forward moves the defer date with it — the same
        # statement as before ("nothing to do here until then"), made again.
        self.set_followup(now + timedelta(days=self.followup_every_days)
                          if self.followup_every_days else None)
        self.save()

    @property
    def repeat_weekday_labels(self) -> list:
        """Short day names for ``repeat_weekdays`` in Mon→Sun order, e.g.
        ``['Mon', 'Wed', 'Fri']``. Empty when no weekday recurrence is set."""
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return [names[d] for d in sorted(self.repeat_weekdays or []) if 0 <= d <= 6]

    @property
    def repeat_monthday_label(self) -> str:
        """Ordinal day-of-month for ``repeat_monthday`` (e.g. ``2nd``), or ``""``
        when no fixed day-of-month recurrence is set. ``31`` reads as
        ``last day`` — it always clamps to the month's real length (see
        ``complete``), so it means "the last day of every month"."""
        d = self.repeat_monthday
        if not d or not (1 <= d <= 31):
            return ""
        if d == 31:
            return "last day"
        suffix = "th" if 11 <= d % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
        return f"{d}{suffix}"

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

    @staticmethod
    def _ph_uid(ph) -> str | None:
        """The user pk (as str) of a ``PersonalHome`` entry, read raw so we
        never dereference the ``user`` ref while scanning the list."""
        ref = ph._data.get("user")
        rid = getattr(ref, "id", None) or getattr(ref, "pk", None)
        return str(rid) if rid is not None else None

    def _personal_home(self, user):
        """This item's ``PersonalHome`` entry for ``user`` (a User doc or a raw
        pk/str), or ``None``. Matched by raw id — no dereference."""
        uid = str(getattr(user, "pk", user))
        for ph in self.personal_homes or []:
            if self._ph_uid(ph) == uid:
                return ph
        return None

    def _set_personal_home(self, user, container) -> None:
        """Upsert (``container`` set) or drop (``container is None``) ``user``'s
        personal filing. One entry per user."""
        uid = str(user.pk)
        homes = [ph for ph in (self.personal_homes or []) if self._ph_uid(ph) != uid]
        if container is not None:
            homes.append(PersonalHome(user=user, container=container))
        self.personal_homes = homes

    def _is_delegated_to(self, user) -> bool:
        """True when this task is assigned to ``user`` but owned by someone
        else. Compared by raw id so a list row never dereferences owner/assignee
        (see CLAUDE.md: minimise DB load per page)."""
        uid = str(getattr(user, "pk", user))
        if self.assigned_to_id != uid:
            return False
        o = self._data.get("owner")
        oid = getattr(o, "id", None) or getattr(o, "pk", None)
        return str(oid) != uid if oid is not None else False

    def home_for(self, user):
        """The container this item appears under FOR ``user`` (``None`` = Inbox).

        A user's own personal filing wins (``personal_homes`` — a container they
        own). Otherwise: a task delegated TO them (assigned, not owned) with no
        filing falls back to the canonical ``parent`` only if they can actually
        see it, else ``None`` (their Inbox) so an untriaged delegated task
        doesn't hide under the owner's invisible project. The owner and plain
        shared-project members always get the canonical ``parent`` (cheap — no
        visibility walk). Independent per user, so a delegation chain (A→B→C)
        and B→C→B round trips keep each participant's placement intact."""
        ph = self._personal_home(user)
        if ph is not None:
            return ph.container
        if self._is_delegated_to(user):
            if self.parent is not None and self.parent.can_be_viewed_by(user):
                return self.parent
            return None
        return self.parent

    def set_home_for(self, user, container) -> None:
        """Place this item into ``container`` (``None`` = Inbox) from ``user``'s
        perspective. For a task delegated TO ``user`` (assigned, not owned) this
        writes their PERSONAL filing (``personal_homes`` — a container they own)
        and NEVER the owner's canonical ``parent`` — so it can't vanish from the
        owner's project, survives re-assignment, and ``None`` just clears that
        user's own filing. Everyone else (the owner, plain shared-project
        members editing a shared tree) moves the canonical ``parent`` as usual.
        Raises ``ValueError`` on an invalid target."""
        if self._is_delegated_to(user):
            if container is not None and container.owner != user:
                raise ValueError("file delegated tasks under your own projects")
            self._set_personal_home(user, container)
        else:
            self.parent = container

    def assignee_filing(self):
        """The container the CURRENT assignee filed this delegated task under
        (their personal home), or ``None`` — for the owner's list badge that
        shows 'assignee › their project'. Dereferences the container; callers
        rendering a list should prefetch ``personal_homes`` containers."""
        aid = self.assigned_to_id
        if not aid:
            return None
        ph = self._personal_home(aid)
        return ph.container if ph is not None else None

    def assignee_filing_id(self) -> str:
        """``assignee_filing`` as a raw pk string with NO dereference (for
        serializers over a list). Empty string when unfiled/unassigned."""
        aid = self.assigned_to_id
        if not aid:
            return ""
        ph = self._personal_home(aid)
        if ph is None:
            return ""
        ref = ph._data.get("container")
        rid = getattr(ref, "id", None) or getattr(ref, "pk", None)
        return str(rid) if rid is not None else ""

    def assign_to(self, new_assignee, *, by_user=None):
        # Assigning a task back to its own owner is the same as unassigning it:
        # the owner already owns it, so there is no delegation to record.
        if new_assignee is not None and new_assignee == self.owner:
            new_assignee = None
        actor = by_user or self.owner
        old = self.assigned_to
        # A non-owner actor who (re)delegates stays in the loop: keep them as a
        # member so the task doesn't vanish from their lists once ``assigned_to``
        # moves on. Applies on unassign too (handing a task back keeps you
        # informed) and — with per-user ``personal_homes`` — keeps their own
        # filing valid across a round trip. Owners always see it via ``owner``.
        if (actor is not None and actor != self.owner
                and not any(m == actor for m in (self.members or []))):
            self.members = list(self.members or []) + [actor]

        # When the assignee changes, drop the OUTGOING assignee's personal filing
        # only if they no longer retain access (not owner, not a member) — so a
        # stale entry can't resurface their now-invisible task in their project.
        # A re-delegator (just added to members above) keeps their filing, which
        # is what makes B→C→B round trips lossless.
        def _prune_home(u):
            if u is None or u == self.owner:
                return
            if any(m == u for m in (self.members or [])):
                return
            self._set_personal_home(u, None)

        if new_assignee is None:
            _prune_home(old)
            # Who is losing this task from their list — captured before the
            # fields that say so are cleared.
            delegator = self.assigned_by
            self.assigned_to = None
            self.assigned_by = None
            self.assigned_at = None
            # Handing it back is news for the people it lands on again: the
            # owner, and whoever delegated it if that was somebody else.
            if old is not None and actor is not None and actor != self.owner:
                self._notify_handed_back(actor, [self.owner, delegator])
            return
        if new_assignee != old:
            _prune_home(old)
        self.assigned_to = new_assignee
        self.assigned_by = actor
        self.assigned_at = _utcnow()
        # Inherit tags onto assignee's user.tags.
        if new_assignee != old and self.tags:
            try:
                new_assignee.ensure_tags(self.tags)
                new_assignee.save()
            except Exception:
                pass
        # Tell the assignee — only on a real (re)assignment, and never for
        # self-assignment (handled above by collapsing to None).
        if new_assignee != old:
            self._notify_assignee()

    def _notify_handed_back(self, actor, recipients) -> None:
        """Bell entry for an assignment somebody took off themselves."""
        if self.pk is None:
            return
        from application.services.notifications import notify_handed_back
        seen, to = set(), []
        for r in recipients:
            key = str(getattr(r, "pk", "")) or ""
            if r is None or not key or key in seen:
                continue
            seen.add(key)
            to.append(r)
        notify_handed_back(self, by_user=actor, to_users=to)

    def _notify_assignee(self) -> None:
        """Push the “you were assigned X” bell entry for ``assigned_to``.

        A brand-new item has no pk yet, so the notification's link would come
        out empty and the dropdown entry would be dead. Assigning *while*
        creating is the normal case (the create form runs ``assign_to`` before
        the first ``save``), so defer the push to just after the insert
        instead of building a link to nowhere.
        """
        if self.pk is None:
            self._pending_assign_notify = True
            return
        from application.services.notifications import notify_assigned
        notify_assigned(self, by_user=self.assigned_by)

    @classmethod
    def note_counts_for(cls, items) -> dict:
        """Map ``str(pk) -> number of linked NOTES`` for the given task-kind
        items, for the note-button badge. Reads the raw ``related`` refs (no
        per-row deref → no N+1) and resolves which are notes in one indexed
        ``$in`` query. (``related_count`` would over-count: it includes related
        tasks/projects, not just notes.)"""
        related_by_item = {}
        all_ids = set()
        for it in items or []:
            if getattr(it, "kind", None) != "task":
                continue
            rids = []
            for r in (it._data.get("related") or []):
                rid = getattr(r, "id", None) or getattr(r, "pk", None)
                if rid:
                    rids.append(str(rid))
            if rids:
                related_by_item[str(it.pk)] = rids
                all_ids.update(rids)
        if not all_ids:
            return {}
        note_ids = {str(pk) for pk in cls.objects(
            pk__in=list(all_ids), kind="note").values_list("id")}
        return {pk: sum(1 for rid in rids if rid in note_ids)
                for pk, rids in related_by_item.items()}

    def storage_bytes(self) -> int:
        """Size of this item's stored BSON document. Dominated by inline note
        images/files; drives the upload guard and the storage meter."""
        from bson import BSON
        return len(BSON.encode(self.to_mongo()))

    def storage_info(self) -> dict:
        """Storage usage for the meter: used size (human-readable — most items
        are a few KB, which rounds to 0.0 MB, so we pick B/KB/MB), the 16 MB
        doc cap, percent full, and a Bootstrap colour level."""
        used = self.storage_bytes()
        level = ("danger" if used >= ITEM_MAX_BYTES
                 else "warning" if used >= ITEM_WARN_BYTES else "success")
        if used < 1024:
            used_label = f"{used} B"
        elif used < 1024 * 1024:
            used_label = f"{used / 1024:.0f} KB"
        else:
            used_label = f"{used / 1024 / 1024:.1f} MB"
        return {
            "used_bytes": used,
            "used_mb": round(used / 1024 / 1024, 1),
            "used_label": used_label,
            "max_mb": ITEM_DOC_CAP_BYTES // 1024 // 1024,
            "pct": min(100, round(used / ITEM_DOC_CAP_BYTES * 100)),
            "level": level,
        }

    def _user_team_ids(self, user):
        from application.services.permissions import teams_of
        return {str(t.pk) for t in teams_of(user)}

    def _explicit_share_node(self):
        """Nearest CONTAINER up the chain (self first) that defines its OWN
        share (members or shared_teams), or None if nobody up the chain shares.
        This is what gives override semantics: the closest container share wins.

        A ``task``'s own share is deliberately NOT a boundary here: a task's
        members/shared_teams (e.g. accumulated by delegation, see ``assign_to``)
        only ADD viewers, they never narrow the enclosing project's share out —
        so a member of a project sees every task in it, even a delegated or
        individually-shared one. The additive grant is applied directly in
        ``can_be_viewed_by``. Sub-projects/sub-folders with their own share DO
        still narrow (the folder principle)."""
        node, seen = self, set()
        while node is not None:
            pk = str(getattr(node, "pk", "") or "")
            if pk and pk in seen:
                break
            if pk:
                seen.add(pk)
            if node.kind != "task" and (node.members or node.shared_teams):
                return node
            node = node.parent
        return None

    def _ancestor_grant(self, user) -> bool:
        """Walk the ancestor chain for the two grants that a descendant share
        can NOT narrow away (unlike a regular member, see
        ``_explicit_share_node``):

        * you OWN an ancestor — owning a task or project means owning
          everything filed under it, so subtasks someone else created under
          your task are yours to see, edit and complete;
        * you MANAGE a team an ancestor is shared with (team admin).

        One traversal for both, since ``can_be_viewed_by`` needs each. Starts
        at ``self`` — redundant for the owner check (the caller already tested
        it) but the team-admin check must still see this item's own
        ``shared_teams``."""
        from application.services.permissions import managed_team_ids
        mids = managed_team_ids(user)
        node, seen = self, set()
        while node is not None:
            pk = str(getattr(node, "pk", "") or "")
            if pk and pk in seen:
                break
            if pk:
                seen.add(pk)
            if node.owner == user:
                return True
            if mids and node.shared_teams \
                    and any(str(t.pk) in mids for t in node.shared_teams):
                return True
            node = node.parent
        return False

    def can_be_viewed_by(self, user: User) -> bool:
        if self.owner == user:
            return True
        if self.assigned_to == user:
            return True
        if self._ancestor_grant(user):
            return True
        # An item's OWN share is additive: members/teams set directly on it can
        # only GRANT access, never narrow. For a task this is the whole effect
        # of its share — it's checked here (not as a boundary) so a task's own
        # members don't hide it from the enclosing project's members.
        if self.members and any(m == user for m in self.members):
            return True
        if self.shared_teams:
            tids = self._user_team_ids(user)
            if any(str(t.pk) in tids for t in self.shared_teams):
                return True
        # Nearest explicit CONTAINER share wins: walk up to the first container
        # (self, then ancestors) that defines its own share. A descendant
        # container with its own members/shared_teams OVERRIDES (narrows) what a
        # parent shared — e.g. a sub-folder shared with one person hides it from
        # the team the parent was shared with. Tasks are not boundaries (above).
        node = self._explicit_share_node()
        if node is None:
            return False
        if node.members and any(m == user for m in node.members):
            return True
        if node.shared_teams:
            team_ids = self._user_team_ids(user)
            if any(str(t.pk) in team_ids for t in node.shared_teams):
                return True
        return False

    def effective_viewers(self):
        """Who can see this item, for display ("wer sieht was"). Combines the
        nearest explicit-share CONTAINER node's members/teams (inherited or set
        on this item) with this item's OWN additive share (a task's own members/
        teams add viewers on top of the enclosing project's — see
        ``can_be_viewed_by``). ``inherited`` reflects whether the container
        share comes from an ancestor rather than this item."""
        node = self._explicit_share_node()
        members = list(node.members or []) if node else []
        teams = list(node.shared_teams or []) if node else []
        # Merge this item's own share (deduped) — relevant when the container
        # boundary is an ancestor but the item itself also shares (e.g. a task).
        if node is None or node.pk != self.pk:
            for m in (self.members or []):
                if all(m != x for x in members):
                    members.append(m)
            for t in (self.shared_teams or []):
                if all(getattr(t, "pk", None) != getattr(x, "pk", None) for x in teams):
                    teams.append(t)
        if not members and not teams:
            return {"members": [], "teams": [], "inherited": False, "source": None}
        return {
            "members": members,
            "teams": teams,
            "inherited": bool(node and node.pk != self.pk),
            "source": node,
        }

    def can_be_edited_by(self, user: User) -> bool:
        return self.can_be_viewed_by(user)

    def can_be_deleted_by(self, user: User) -> bool:
        """Who may put this in the trash: whoever may edit it.

        Deleting is a soft delete — the subtree goes to the trash and comes
        back from it — so the question is not "may this be destroyed" but "is
        this yours to tidy up", and for shared work the answer is everyone who
        works in it. Reserving it for the owner is what left a folder shared
        with a whole team removable by nobody present: the account that created
        it may be a bot, or long gone from the team.

        Named apart from :meth:`can_be_edited_by` on purpose — this is the one
        place to narrow it again if a delete ever stops being reversible."""
        return self.can_be_edited_by(user)

    def regenerate_share_token(self) -> str:
        import secrets
        self.share_token = secrets.token_urlsafe(24)
        return self.share_token

    # ----- review cadence (tasks + projects) -----

    def effective_review_interval(self) -> int:
        """Review cadence in days, honouring inheritance. Own value wins;
        otherwise inherit the nearest ancestor that sets one. ``0`` = never.
        Nobody having set one anywhere is not "never" — it is a container that
        was created without being asked, so it falls back to
        ``DEFAULT_REVIEW_INTERVAL_DAYS`` and comes up for review like the rest.

        A **folder** is skipped on the way up: its stored value is written by
        the code (the inspector has no review field for folders), so inheriting
        it silenced every project underneath — the review would never mention
        them again, and nothing on screen said why.
        """
        node = self
        seen = set()
        while node is not None and node.pk not in seen:
            if node.kind != "folder" and node.review_interval_days is not None:
                return node.review_interval_days
            seen.add(node.pk)
            node = node.parent
        return DEFAULT_REVIEW_INTERVAL_DAYS

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
        its actions together. The cascade stops at sub-project / folder
        boundaries: those are opaque, reviewed via their own queue entry, so
        crossing into them would clear their (maybe override-due) tasks
        without the user ever having looked at them."""
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
                        next_frontier.append(c.pk)  # descend into sub-tasks
                    elif c.kind == "section":
                        next_frontier.append(c.pk)  # transparent grouping
                    # sub-project / folder: opaque — cleared by their own pass
                frontier = next_frontier

    def __str__(self) -> str:
        return self.title or "(untitled)"
