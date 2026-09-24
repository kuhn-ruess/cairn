"""User and API token models."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from flask_login import UserMixin
from mongoengine import (
    BooleanField,
    DateTimeField,
    Document,
    EmailField,
    EmbeddedDocument,
    EmbeddedDocumentField,
    EmbeddedDocumentListField,
    IntField,
    ListField,
    MapField,
    ObjectIdField,
    ReferenceField,
    StringField,
)
from werkzeug.security import check_password_hash, generate_password_hash


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(raw: str) -> str:
    """SHA-256 of the raw token. Used as the at-rest representation —
    a DB dump never reveals the live token."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ApiToken(EmbeddedDocument):
    """Personal API token, embedded on the owning user.

    Only the SHA-256 hash of the raw token is stored — the raw string
    is shown to the user exactly once at mint time and then forgotten.
    Lookup goes through the ``api_tokens.token_hash`` index.
    """
    pk_ref = ObjectIdField(default=lambda: ObjectId())  # stable id for revoke / display
    name = StringField(max_length=128, default="default")
    token_hash = StringField(required=True)
    created_at = DateTimeField(default=_utcnow)
    last_used_at = DateTimeField()
    revoked = BooleanField(default=False)
    # Read-only tokens may only drive safe (GET/HEAD/OPTIONS) API requests —
    # enforced in token_required. Handy for sharing live read access.
    read_only = BooleanField(default=False)

    def mark_used(self) -> None:
        self.last_used_at = _utcnow()


class User(UserMixin, Document):
    meta = {
        "collection": "users",
        # Keys in the stored document that no field claims are IGNORED rather
        # than fatal. Schemas here change directly, without migrations, and
        # mongoengine's default is to raise FieldDoesNotExist on load — so a
        # renamed field turned every stored user into an unreadable document,
        # and with it every page that dereferences one (which is every page).
        "strict": False,
        # ``team`` indexed so bulk team-membership ops stay fast.
        # ``api_tokens.token`` indexed so token-based auth stays a
        # single keyed lookup (the embedded layout doesn't change the
        # underlying B-tree shape).
        # ``booking.slug`` indexed because every hit on a public /book/<slug>
        # page resolves the user through it.
        "indexes": ["email", "username", "team", "api_tokens.token_hash",
                    "booking.slug"],
    }

    username = StringField(required=True, unique=True, max_length=64)
    email = EmailField(required=True, unique=True)
    display_name = StringField(max_length=128)
    # Other names this person is written as in systems we import from: the
    # e-mail of a Jira/DRV account, a foreign username, a spelling of their
    # name nobody here uses. An import can only assign to a teammate, so a row
    # naming an address Cairn has never seen lands unassigned — which reads
    # exactly like a task nobody has to do. Listing the address here is how the
    # owner of the account says "that is also me".
    # Matching only: an alias never beats a real identity (see
    # services.permissions.assignee_index) and is not a way to sign in.
    import_aliases = ListField(StringField(max_length=200))
    password_hash = StringField(required=True)
    is_active_user = BooleanField(default=True)
    is_admin = BooleanField(default=False)
    created_at = DateTimeField(default=_utcnow)
    last_login_at = DateTimeField()

    # Account tier — controls limits and capabilities.
    #   free         — solo, capped at FREE_PROJECT_LIMIT projects, no sharing
    #   premium      — solo, no project cap, may belong to a team
    #   team_admin   — owns one team, can invite members and share within it
    #   global_admin — full admin, the only role that can grant levels
    level = StringField(
        choices=["free", "premium", "team_admin", "global_admin"],
        default="free",
    )
    # The team this user belongs to (set by accepting an invite). For
    # ``team_admin`` this is also the team they own. Being in a team always
    # grants premium for free — it's bundled in the admin's team subscription.
    team = ReferenceField("Team")

    # ---- Subscription / billing (Stripe) ----
    # ``level`` above stays the single source of truth for every gate in the
    # app; the billing layer (webhooks + the nightly ``reconcile-subscriptions``
    # sweep, see application.services.billing) is what WRITES it based on the
    # state below. ``global_admin`` is never touched automatically.
    #
    #   trial_ends_at        — app-managed 30-day premium trial set at signup
    #                          (no card, no Stripe customer yet). Expired + no
    #                          active sub + not in a team → falls back to free.
    #   subscription_plan    — "" | "premium" (€2) | "team" (€5)
    #   subscription_status  — Stripe status: "" | trialing | active | past_due
    #                          | canceled. Only active/trialing entitle.
    stripe_customer_id = StringField(default="")
    subscription_id = StringField(default="")
    subscription_plan = StringField(default="")
    subscription_status = StringField(default="")
    current_period_end = DateTimeField()
    cancel_at_period_end = BooleanField(default=False)
    trial_ends_at = DateTimeField()
    # Operator-granted comp plan ("" | premium | team). Set by ``set-level``,
    # it entitles like a paid subscription so manually-promoted accounts
    # (friends, grandfathered users) survive the nightly reconcile sweep.
    comp_plan = StringField(default="")

    # Saved per-user secrets (encrypted at rest via services.crypto).
    # The corresponding OAuth client / API client lives in app config so
    # users never touch credentials.
    todoist_token = StringField()
    show_todoist_sync = BooleanField(default=False)
    last_todoist_sync_at = DateTimeField()

    # ---- Microsoft (Entra) — connected only to create Teams meetings for
    # booking-page appointments. Delegated consent per user, because a Teams
    # meeting has an organiser and that is whoever owns the page; the app
    # registration itself is deployment-wide (BKTODO_MS_CLIENT_ID/_SECRET).
    # See services/msgraph.py. Tokens encrypted like every secret here.
    ms_refresh_token = StringField()
    ms_access_token = StringField()
    ms_token_expires_at = DateTimeField()
    # The account that was linked ("someone@contoso.com"), read once at
    # connect time so the settings page can name it.
    ms_account = StringField(max_length=255, default="")

    # ---- Notes mirror: the notes tree as Markdown files on a WebDAV share
    # (a Synology, so DEVONthink can index them). Two-way; see
    # services/notes_sync.py. The password is encrypted like every other
    # per-user secret here (services/crypto.py).
    notes_mirror_enabled = BooleanField(default=False)
    notes_mirror_url = StringField()
    notes_mirror_user = StringField()
    notes_mirror_password = StringField()
    # Write the tasks out too (Tasks/ on the share, one file per task with
    # its comments — a copy, never read back; see services/tasks_mirror).
    notes_mirror_tasks = BooleanField(default=False)
    # Minutes between background rounds; 0 = only when the button is pressed.
    notes_mirror_interval = IntField(default=15)
    last_notes_mirror_at = DateTimeField()
    notes_mirror_status = StringField(default="")

    # Zammad import — URL + token live with the user so each user can
    # point at their own ticketing instance. Orphan reconcile keys off
    # ``Item.source="zammad"`` now, no separate label tag needed.
    zammad_url = StringField()
    zammad_token = StringField()
    show_zammad_sync = BooleanField(default=False)
    last_zammad_sync_at = DateTimeField()
    # Background auto-sync (Celery beat enqueues opted-in users periodically).
    # The import options below mirror the user's last manual sync choices so
    # the background run drops tickets in the same place. zammad_sync_project
    # holds a project id ("" = inbox).
    # The master switch for *this user's own* Zammad sync. Off means: no
    # background run, no sync button, and the manual import declines — the one
    # place to say "my team syncs centrally, stop fetching the same tickets
    # twice" without unpicking every option below it.
    zammad_enabled = BooleanField(default=True)
    zammad_auto_sync = BooleanField(default=False)
    zammad_sync_project = StringField(default="")
    zammad_assignee_only = BooleanField(default=True)
    zammad_include_closed = BooleanField(default=False)
    # Mirror the time booked on a ticket into the time-tracking module. The
    # master switch for the whole time import: off means no time_accountings
    # call is made at all (see services.zammad_import._import_time_accounting).
    zammad_import_time = BooleanField(default=True)
    # Where imported time lands when the ticket's Cairn subtree isn't linked to
    # a billing project: off = nowhere (the link is the only opt-in), on = the
    # ticket's Zammad organisation becomes a Cairn customer + billing project,
    # created on first sight and reused afterwards. See
    # services.zammad_billing.ensure_billing_target.
    zammad_time_auto_customer = BooleanField(default=False)
    # Only import bookings made on or after this day ("YYYY-MM-DD", read in the
    # user's timezone). The day you went live: older hours belong to whatever
    # system you kept them in, and dragging them in once the logging here is
    # productive means two records of the same month. Empty = no cutoff.
    zammad_time_from = StringField(max_length=10, default="")

    # The billing projects this user last booked time on, newest first (a short
    # list, capped in services.timetracking.remember_project). The capture bar
    # offers these before anything else and only ships them to the browser —
    # a team with a thousand projects must not put a thousand <option>s on a
    # page that is re-rendered after every single booking.
    tt_recent_projects = ListField(StringField(max_length=32))

    # Password reset
    password_reset_token = StringField()
    password_reset_expires_at = DateTimeField()

    # Calendar write-back: which connected calendar to push tasks to and
    # which date fields trigger event creation.
    calendar_write_target = StringField()  # ExternalCalendar.pk as string
    calendar_write_due = BooleanField(default=False)
    calendar_write_plan = BooleanField(default=False)

    # IANA timezone (e.g. ``Europe/Berlin``) used to interpret naive
    # datetimes coming from the datetime-local form input when we push
    # events to Google Calendar. Empty → use the server's configured
    # ``DEFAULT_TZ``.
    timezone = StringField(default="")

    # Task-row display: list of field keys the user wants HIDDEN in the
    # task list rows. Empty (default) = show everything. Stored as a
    # blocklist so newly-added fields show up without a migration.
    task_row_hidden = ListField(StringField())

    # Per-view overrides of ``task_row_hidden``. Map key → list of
    # hidden fields. Missing key → fall back to the global setting
    # above. View keys are short strings (built-in perspective keys, or
    # ``cp:<pid>`` for custom perspectives).
    task_row_hidden_by_view = MapField(ListField(StringField()))

    # Inspector section layout (see application.services.inspector_layout).
    # ``inspector_layout`` is the full ordered list of section keys; an empty
    # list means the default order. ``inspector_hidden`` is the blocklist of
    # sections to hide. Splitting order from visibility lets a newly shipped
    # section default to visible (absent from hidden) while still being
    # placed by order — without confusing "never seen" with "hidden".
    # The ``title`` section is pinned and appears in neither.
    inspector_layout = ListField(StringField())
    inspector_hidden = ListField(StringField())

    # Optional user override to delete done/dropped tasks SOONER than their
    # plan's limit (see ``effective_retention_days``). ``0`` = use the plan
    # default (free 60d / premium 180d). Sweep: ``cairn cleanup-completed-tasks``.
    completed_task_retention_days = IntField(default=0, min_value=0)
    # Premium (and team) users may switch the sweep off entirely and keep their
    # finished tasks for good. Free stays on its plan window — that limit is
    # what the plan *is*, not a preference.
    keep_completed_forever = BooleanField(default=False)

    # Trash: items deleted by the user are soft-deleted (Item.trashed_at) and
    # purged this many days later. ``0`` keeps trash until emptied by hand.
    # Sweep is run by ``cairn cleanup-trash`` (cron-able).
    trash_retention_days = IntField(default=30, min_value=0)

    # Items the user has collapsed in tree views (sub-task children
    # hidden under their parent). Stored as a list of string PKs so a
    # single read returns the whole set per request.
    collapsed_items = ListField(StringField())

    # Favorites pinned to the sidebar. Heterogeneous targets stored as
    # namespaced tokens (list order = display order), resolved at render time:
    #   item:<pk>     — a task or note
    #   persp:<key>   — a built-in perspective
    #   cp:<pid>      — a custom perspective
    #   org:<slug>    — an "Organize" nav entry (projects, notes, tree, …)
    favorites = ListField(StringField())

    # Vim keys in the notes/scratchpad outliner (static/js/outliner-vim.js).
    # Off by default: modal editing is a preference, and a writer who never
    # asked for it would find the keyboard answering commands instead of text.
    editor_vim_mode = BooleanField(default=False)

    # Scratchpad — the raw Markdown outline the user is drafting on
    # the /scratchpad page. Persisted server-side so it survives across
    # devices/browsers (not just localStorage) and stays put after a convert.
    scratchpad = StringField(default="")

    # Today perspective preferences (persisted across sessions).
    today_grouping = StringField(default="none")
    today_filter_tag = StringField()  # Tag.pk to filter Today to a single tag, optional
    # Last-used Today window length (duration in days) and layout (list/
    # calendar) — remembered so the view comes back the way the user left it.
    today_days = IntField(default=1, min_value=1)
    today_view_mode = StringField(default="list")
    # Perspectives (Today, Next, Flagged, Waiting, …) default to a PERSONAL
    # scope — only tasks you own or are currently assigned. When this is on, they
    # also include team-shared / re-delegated tasks you can see but don't own.
    # Toggled from the perspective toolbar. See own_or_assigned_q / _perspective_scope.
    perspectives_show_shared = BooleanField(default=False)
    # OmniFocus-style availability: deferred projects (defer_date in the
    # future) are hidden from the project list + sidebar unless this is on.
    show_deferred_projects = BooleanField(default=False)
    # Weekly review: a project/folder with no open task anywhere below it has
    # nothing to review and stays out of the queue unless this is on. Toggled
    # from the review index ("Show empty"). See views/review.py.
    review_show_empty = BooleanField(default=False)
    # OmniFocus-style "Focus": when set to an Item.pk (folder/project/section),
    # the whole app only shows tasks beneath that item. "" = no focus.
    # See application/services/focus.py.
    focused_item_pk = StringField(default="")
    # When on, login lands on a folder picker (focus.choose) so the user can
    # set the focus for this session before seeing anything else.
    focus_on_login = BooleanField(default=False)
    # CalDAV calendar feed: whether the read-only "Cairn (Kalender)" VEVENT
    # collection is exposed at all, and whether a task's event is placed on its
    # due date or its plan date. See application/views/caldav.py.
    caldav_calendar_enabled = BooleanField(default=False)
    caldav_calendar_source = StringField(default="due", choices=("due", "plan"))
    # Working-day window (hours) for the Today "Plan" timeline + auto-plan:
    # auto-scheduling only fills free slots between these two hours.
    plan_day_start = IntField(default=9, min_value=0, max_value=23)
    plan_day_end = IntField(default=18, min_value=1, max_value=24)
    # How the Plan view's Unplanned / Next-actions rails are split up:
    # ``none`` is one flat list, ``project`` groups by the owning project or
    # folder, ``customer`` by the customer the subtree bills to (see
    # services.containers.customer_labels). The timeline itself is never
    # grouped — it is chronological, which is the whole point of it.
    plan_grouping = StringField(default="none",
                                choices=("none", "project", "customer"))
    # Buffer (minutes) auto-plan keeps between consecutive tasks.
    plan_buffer_min = IntField(default=0, min_value=0, max_value=120)
    # Lunch break kept free by auto-plan, as minutes since midnight (12:30 is
    # 750) — a break is rarely a whole hour on the hour. start == end → off.
    plan_lunch_start_min = IntField(default=0, min_value=0, max_value=1439)
    plan_lunch_end_min = IntField(default=0, min_value=0, max_value=1439)

    # Custom sidebar perspective ordering. Empty = use the canonical default
    # (all perspectives shown). When non-empty, only these keys are shown,
    # in this exact order.
    sidebar_perspectives = ListField(StringField())

    # User-defined custom perspectives (max 20 enforced on save).
    from application.models.perspective import CustomPerspective as _CP  # noqa: E402
    custom_perspectives = EmbeddedDocumentListField(_CP)

    # Tags live inside the user — see application.models.tag for the
    # rationale. Stored as embedded so a user's full tag set is one read.
    from application.models.tag import Tag as _Tag  # noqa: E402
    tags = EmbeddedDocumentListField(_Tag)

    # API tokens (embedded — see ApiToken docstring).
    api_tokens = EmbeddedDocumentListField("ApiToken")

    # External calendars (embedded — every per-user query that touches
    # them now reuses the User document already in scope).
    from application.models.calendar import ExternalCalendar as _ExternalCalendar  # noqa: E402
    calendars = EmbeddedDocumentListField(_ExternalCalendar)

    # In-app notifications (navbar bell). Embedded so the unread badge is free
    # on every page — see application.models.notification. Capped on write.
    from application.models.notification import Notification as _Notification  # noqa: E402
    notifications = EmbeddedDocumentListField(_Notification)

    # The public booking page (embedded — the slug lookup already has to load
    # the user document, and the page then reads its whole configuration out
    # of it). See application.models.booking.
    from application.models.booking import BookingPage as _BookingPage  # noqa: E402
    booking = EmbeddedDocumentField(_BookingPage, default=_BookingPage)

    # Time tracking: the running stopwatch (at most one) and the billing
    # project last logged to. Embedded so the navbar timer chip and the
    # "log time on this task" defaults cost no extra query. It lives in its own
    # module because application.models.timetracking imports User — see there.
    from application.models.timer import RunningTimer as _RunningTimer  # noqa: E402
    running_timer = EmbeddedDocumentField(_RunningTimer)
    tt_last_project = StringField(default="")
    # Which team the time-tracking module opens in when no team is in the URL.
    # A plain id string (not a reference): it is a UI preference, and a team the
    # user has since left must degrade to "first team", not to a broken deref.
    tt_default_team = StringField(default="")
    # The same, for the team pages. Kept apart from ``tt_default_team`` on
    # purpose: "whose hours am I looking at" and "whose team am I
    # administering" are the same answer for most people and emphatically not
    # for everyone, and one pin silently moving the other is the kind of
    # surprise a preference must not spring.
    default_team = StringField(default="")

    def save(self, *args, **kwargs):
        # Enforce the 20-perspective ceiling at the model layer so the API
        # path can't sneak around the form.
        if self.custom_perspectives and len(self.custom_perspectives) > 20:
            raise ValueError("A user can keep at most 20 custom perspectives.")
        return super().save(*args, **kwargs)

    # ---------- tag helpers ----------

    def tag_by_name(self, name: str):
        if not name:
            return None
        lname = name.lower()
        for t in self.tags or []:
            if t.name.lower() == lname:
                return t
        return None

    def tag_names(self) -> list[str]:
        return [t.name for t in (self.tags or [])]

    # ---------- notification helpers ----------

    NOTIFICATION_CAP = 50
    NOTIFICATION_KEEP_DAYS = 30

    @property
    def unread_notification_count(self) -> int:
        return sum(1 for n in (self.notifications or []) if not n.read)

    def prune_notifications(self) -> bool:
        """Drop READ notifications older than ``NOTIFICATION_KEEP_DAYS``.

        An unread one never expires by age — it is still waiting to be seen,
        and only the cap can push it out. Runs on the two occasions the user
        document is written anyway (a new notification, and marking read), so
        the list ages out without a sweep to schedule.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.NOTIFICATION_KEEP_DAYS)
        keep = []
        for n in self.notifications or []:
            created = n.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if n.read and created is not None and created < cutoff:
                continue
            keep.append(n)
        if len(keep) == len(self.notifications or []):
            return False
        self.notifications = keep
        return True

    def add_notification(self, **kwargs) -> None:
        """Append a notification (newest last) and trim to the cap."""
        from application.models.notification import Notification
        self.prune_notifications()
        self.notifications.append(Notification(**kwargs))
        overflow = len(self.notifications) - self.NOTIFICATION_CAP
        if overflow > 0:
            del self.notifications[:overflow]

    def mark_notifications_read(self, ids=None) -> bool:
        """Mark all (``ids is None``) or the given pk_refs read. Returns
        True if anything changed (so the caller can skip a no-op save)."""
        wanted = set(ids) if ids is not None else None
        changed = False
        for n in self.notifications or []:
            if wanted is not None and str(n.pk_ref) not in wanted:
                continue
            if not n.read:
                n.read = True
                changed = True
        return self.prune_notifications() or changed

    # ---------- favorites ----------

    def is_favorite(self, token: str) -> bool:
        return token in (self.favorites or [])

    def toggle_favorite(self, token: str) -> bool:
        """Add/remove a favorite token (list order = sidebar order). Returns the
        new state (True = now favorited). Caller saves."""
        favs = list(self.favorites or [])
        if token in favs:
            favs.remove(token)
            now_fav = False
        else:
            favs.append(token)
            now_fav = True
        self.favorites = favs
        return now_fav

    def tag_tree(self) -> list[tuple[int, "object"]]:
        """The user's tags in depth-first parent→child order, each paired
        with its depth (0 = root). Used wherever a tag picker should show
        the hierarchy instead of a flat list."""
        by_parent: dict = {}
        for t in self.tags or []:
            by_parent.setdefault(t.parent or None, []).append(t)
        out: list = []

        def walk(parent_name, depth):
            for t in sorted(by_parent.get(parent_name, []), key=lambda x: x.name.lower()):
                out.append((depth, t))
                walk(t.name, depth + 1)

        walk(None, 0)
        # Orphans (parent set but missing) would be skipped above — surface
        # them at the root so they never silently vanish from a picker.
        seen = {id(t) for _d, t in out}
        for t in self.tags or []:
            if id(t) not in seen:
                out.append((0, t))
        return out

    def tag_descendants(self, name: str) -> set[str]:
        """The set of tag names rooted at ``name`` — the tag itself plus every
        descendant. Filtering by a parent tag uses this to also match tasks
        carrying any child tag."""
        children: dict = {}
        for t in self.tags or []:
            if t.parent:
                children.setdefault(t.parent.lower(), []).append(t.name)
        acc: set[str] = set()

        def collect(n: str):
            for child in children.get(n.lower(), []):
                if child not in acc:
                    acc.add(child)
                    collect(child)

        if name:
            acc.add(name)
            collect(name)
        return acc

    def show_field(self, name: str, view_key: str | None = None) -> bool:
        """Whether the named field should render in task-row metadata.

        When ``view_key`` is set and there's a per-view override, that
        wins over the global ``task_row_hidden`` blocklist.
        """
        if view_key and self.task_row_hidden_by_view:
            override = self.task_row_hidden_by_view.get(view_key)
            if override is not None:
                return name not in override
        return name not in (self.task_row_hidden or [])

    def has_view_override(self, view_key: str) -> bool:
        return bool(self.task_row_hidden_by_view and
                    view_key in self.task_row_hidden_by_view)

    # ---------- level helpers ----------

    @property
    def is_global_admin(self) -> bool:
        return self.level == "global_admin"

    @property
    def is_team_admin(self) -> bool:
        return self.level in ("team_admin", "global_admin")

    @property
    def is_premium(self) -> bool:
        """Entitled to premium features. ``level`` is the recomputed source of
        truth (see ``billing.recompute_level``): anything above ``free`` — own
        subscription, comp plan, trial, or a team-covered seat — qualifies."""
        return (self.level or "free") != "free"

    @property
    def can_share_projects(self) -> bool:
        # Premium-and-up users plus anyone in a team can share within it.
        return self.team is not None or self.is_team_admin

    # ---------- billing helpers ----------

    def start_trial(self, days: int) -> None:
        """Begin the one-time signup premium trial. No-op if a trial was
        already granted or the user is a global admin (operators don't
        trial)."""
        if self.level == "global_admin":
            return
        if self.trial_ends_at is None:
            self.trial_ends_at = _utcnow() + timedelta(days=int(days))

    @property
    def is_trial_active(self) -> bool:
        return bool(self.trial_ends_at and self.trial_ends_at > _utcnow())

    @property
    def has_active_subscription(self) -> bool:
        """True only while Stripe considers the subscription paid. ``past_due``
        and ``canceled`` deliberately don't entitle (no grace period)."""
        return self.subscription_status in ("active", "trialing")

    @property
    def is_premium_via_team(self) -> bool:
        """Premium granted by team membership (bundled in the team's
        subscription), not the user's own. While true, trial messaging is
        irrelevant — it only matters again once they leave the team."""
        return self.team is not None

    @property
    def trial_days_left(self) -> int:
        if not self.is_trial_active:
            return 0
        delta = self.trial_ends_at - _utcnow()
        # Round up so the last partial day still reads as "1 day left".
        return max(0, -(-delta.days) if delta.seconds == 0 else delta.days + 1)

    # Tier-based retention for completed/dropped tasks: free keeps 60 days,
    # premium (and up) 6 months. A user may opt into a SHORTER window via
    # ``completed_task_retention_days`` but can't exceed their plan's limit.
    FREE_RETENTION_DAYS = 60
    PREMIUM_RETENTION_DAYS = 180

    def effective_retention_days(self) -> int:
        """Days a finished task is kept, or ``0`` for "never delete".

        ``0`` is the same answer the purge already treats as "leave everything
        alone", so switching the sweep off needs nothing further downstream."""
        if self.is_premium and self.keep_completed_forever:
            return 0
        tier = self.FREE_RETENTION_DAYS if (self.level or "free") == "free" \
            else self.PREMIUM_RETENTION_DAYS
        user_set = int(self.completed_task_retention_days or 0)
        return min(user_set, tier) if user_set > 0 else tier

    def ensure_tags(self, names) -> int:
        """Add any tag names from ``names`` that the user doesn't already
        have. Returns how many new tags were created. Used by the
        assignment-inherits-tags path."""
        from application.models.tag import Tag as _Tag
        added = 0
        for name in names or []:
            if not name:
                continue
            if not self.tag_by_name(name):
                self.tags.append(_Tag(name=name, color="#6c757d", is_context=False))
                added += 1
        return added

    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

    def issue_password_reset(self, hours_valid: int = 2) -> str:
        """Generate a fresh reset token, store it with an expiry, return it.

        The token is short-lived (default 2 hours) and single-use — the
        reset view clears it after a successful password change.
        """
        from datetime import timedelta
        self.password_reset_token = secrets.token_urlsafe(32)
        self.password_reset_expires_at = _utcnow() + timedelta(hours=hours_valid)
        self.save()
        return self.password_reset_token

    def consume_password_reset(self) -> None:
        self.password_reset_token = ""
        self.password_reset_expires_at = None
        self.save()

    def password_reset_valid(self, token: str) -> bool:
        if not token or not self.password_reset_token:
            return False
        if not secrets.compare_digest(token, self.password_reset_token):
            return False
        if not self.password_reset_expires_at:
            return False
        return self.password_reset_expires_at > _utcnow()

    @property
    def is_active(self) -> bool:  # Flask-Login hook
        return bool(self.is_active_user)

    def get_id(self) -> str:
        return str(self.pk)

    # ---------- calendar helpers ----------

    def find_calendar(self, pk_str: str):
        """Lookup an embedded calendar by its ``pk_ref`` (string form)."""
        if not pk_str:
            return None
        for cal in self.calendars or []:
            if str(cal.pk_ref) == str(pk_str):
                return cal
        return None

    def add_calendar(self, **kwargs):
        from application.models.calendar import ExternalCalendar
        cal = ExternalCalendar(**kwargs)
        self.calendars.append(cal)
        self.save()
        return cal

    def remove_calendar(self, pk_str: str) -> bool:
        keep = [c for c in (self.calendars or []) if str(c.pk_ref) != str(pk_str)]
        if len(keep) == len(self.calendars or []):
            return False
        self.calendars = keep
        self.save()
        return True

    def issue_api_token(self, name: str = "default",
                        read_only: bool = False) -> "tuple[ApiToken, str]":
        """Mint and persist a new API token; return ``(token, raw)``.

        Only the SHA-256 hash is persisted on the user — the raw string
        is the caller's only chance to see it. ``read_only`` tokens are
        restricted to safe HTTP methods (see token_required).
        """
        raw = secrets.token_urlsafe(32)
        tok = ApiToken(name=name, token_hash=_hash_token(raw), read_only=read_only)
        self.api_tokens.append(tok)
        self.save()
        return tok, raw

    def find_api_token(self, raw: str) -> "ApiToken | None":
        if not raw:
            return None
        expected = _hash_token(raw)
        for t in self.api_tokens or []:
            if t.revoked:
                continue
            if secrets.compare_digest(t.token_hash, expected):
                return t
        return None

    @staticmethod
    def by_api_token(raw: str) -> "tuple[User, ApiToken] | None":
        """Lookup the owning user + active token for a raw string.

        Hashes the raw input and queries the ``api_tokens.token_hash``
        index — still a single keyed Mongo query, but the stored
        material is unusable on its own. Returns ``None`` when there's
        no match (revoked tokens count as no match).
        """
        if not raw:
            return None
        h = _hash_token(raw)
        user = User.objects(api_tokens__token_hash=h).first()
        if user is None:
            return None
        tok = user.find_api_token(raw)
        return (user, tok) if tok else None

    def __str__(self) -> str:
        return self.display_name or self.email or self.username
