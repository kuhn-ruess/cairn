"""Settings views: importers, account preferences."""
import secrets
import urllib.parse
from datetime import datetime, timezone

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect,
    render_template, request, send_file, session, url_for,
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import URL, DataRequired, Length, Optional

from application.models.user import User
from application.services.calendar_sync import (
    CalendarError,
    fetch_events,
    google_oauth_authorize_url,
    google_oauth_exchange_code,
)
from application.services.todoist_import import TodoistError, import_for_user
from application.services.zammad_import import (
    ZammadError, default_time_from, import_for_user as zammad_import_for_user,
)

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")


class ICalForm(FlaskForm):
    ical_url = StringField("Secret iCal URL", validators=[DataRequired(), URL()])
    name = StringField("Name (optional)", validators=[Optional(), Length(max=128)])
    color = StringField("Color", default="#458588")
    show_in_today = BooleanField("Show events in Today", default=True)
    submit = SubmitField("Subscribe")


class CalDavForm(FlaskForm):
    caldav_url = StringField(
        "Server or calendar URL", validators=[DataRequired(), URL()],
        description="Your server's address (every calendar on it is connected) "
        "or one calendar's collection URL, e.g. "
        "https://cloud.example.com/remote.php/dav/calendars/you/personal/",
    )
    username = StringField("Username", validators=[DataRequired(), Length(max=255)])
    password = PasswordField("Password / app token", validators=[DataRequired()])
    name = StringField("Name (optional)", validators=[Optional(), Length(max=128)])
    color = StringField("Color", default="#458588")
    # One calendar out of a whole account starts visible; the others are a
    # click away in the list, which is why this doesn't say "events".
    show_in_today = BooleanField("Show the main calendar in Today", default=True)
    submit = SubmitField("Connect")




class TodoistImportForm(FlaskForm):
    token = PasswordField("Todoist API token", validators=[Optional()])
    save_token = BooleanField("Remember this token for future syncs")
    show_sync_button = BooleanField("Show a one-click Sync button in the toolbar")
    include_completed = BooleanField("Include completed tasks")
    include_comments = BooleanField("Include comments", default=True)
    dry_run = BooleanField("Dry run (don't write changes)")
    submit = SubmitField("Import")


class ZammadImportForm(FlaskForm):
    enabled = BooleanField("Sync Zammad for my own account", default=True)
    url = StringField(
        "Zammad URL",
        validators=[Optional(), URL()],
        description="Base URL of your Zammad instance, e.g. https://zammad.example.com",
    )
    token = PasswordField("Zammad API token", validators=[Optional()])
    save_credentials = BooleanField("Remember URL + token for future syncs")
    show_sync_button = BooleanField("Show a one-click Sync button in the toolbar")
    auto_sync = BooleanField("Sync automatically in the background (every few minutes)")
    project = SelectField("Import into project", choices=[], validators=[Optional()])
    assignee_only = BooleanField("Only tickets assigned to me", default=True)
    include_closed = BooleanField("Include closed tickets (closed/merged/pending close)")
    import_time = BooleanField(
        "Import booked ticket time into time tracking", default=True)
    auto_customer = BooleanField(
        "Create customers from Zammad organisations when the ticket's project "
        "isn't linked to a billing project")
    time_from = StringField(
        "Import time booked from", validators=[Optional(), Length(max=10)],
        description="YYYY-MM-DD — the day you went live here. Bookings before "
                    "it are left where they are. Empty imports everything.")
    full_sync = BooleanField(
        "Check every ticket, not only the ones changed since the last sync")
    dry_run = BooleanField("Dry run (don't write changes)")
    submit = SubmitField("Import")
    save_only = SubmitField("Save settings")


class NotesMirrorForm(FlaskForm):
    """Where the notes are mirrored as Markdown files, and how often."""

    enabled = BooleanField("Mirror my notes to this share")
    url = StringField(
        "WebDAV folder", validators=[Optional(), URL()],
        description="The collection the notes live in, e.g. "
                    "https://nas.example.com:5006/home/Cairn/Notes",
    )
    username = StringField("Username", validators=[Optional(), Length(max=255)])
    password = PasswordField("Password", validators=[Optional()])
    tasks = BooleanField("Write my tasks there too")
    interval = SelectField(
        "Run in the background", choices=[
            ("0", "Only when I press Sync now"),
            ("5", "Every 5 minutes"),
            ("15", "Every 15 minutes"),
            ("60", "Hourly"),
            ("360", "Every 6 hours"),
            ("1440", "Daily"),
        ], default="15",
    )
    submit = SubmitField("Save")
    test = SubmitField("Test connection")


def _minutes_of_day(raw) -> int:
    """``"12:30"`` → 750, minutes since midnight. Anything unreadable — an
    empty field above all, which is how the lunch break is switched off —
    reads as 0. A bare ``"12"`` is accepted too; a time input always posts
    ``HH:MM``, but a hand-written form post shouldn't silently mean 00:12."""
    parts = (raw or "").strip().split(":")
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return 0
    return max(0, min(23 * 60 + 59, hours * 60 + minutes))


@settings_bp.route("/")
@login_required
def index():
    return render_template("settings/index.html")


TASK_ROW_FIELDS = (
    # (key, label, hint)
    ("flag",         "Flag",          "Yellow flag chip in front of the title"),
    ("priority",     "Priority chip", "P1/P2/P3/P4 marker on the title line"),
    ("next_chip",    "NEXT chip",     "Highlighted marker for next-action tasks"),
    ("project",      "Project badge", "Folder name with link to the project"),
    ("tags",         "Tag chips",     "Coloured tag / context chips"),
    ("due_date",     "Due date",      "Hard deadline with relative date"),
    ("defer_chip",   "Defer chip",    "Hourglass marker while deferred"),
    ("plan_chip",    "Plan chip",     "When you intend to work on it"),
    ("assignee",     "Assignee",      "Who the task is assigned to / from"),
    ("status_badge", "Status badge",  "Waiting / scheduled / someday badge"),
    ("comments",     "Comment count", "Chat icon + number of comments"),
    ("related",      "Related count", "Link icon + number of related items"),
    ("note",         "Task note",     "Inline excerpt of the task's note text"),
    ("external_id",  "External ID",   "Imported ticket / row id (csv:…, zammad:…)"),
    ("source",       "Source chip",   "Where the task came from (jira / zammad / csv …)"),
)


@settings_bp.route("/display/<view_key>", methods=["POST"])
@login_required
def display_override(view_key):
    """Save a per-view task-row override (or reset to global)."""
    user = current_user._get_current_object()
    if request.form.get("reset"):
        if user.task_row_hidden_by_view and view_key in user.task_row_hidden_by_view:
            del user.task_row_hidden_by_view[view_key]
            user.save()
        flash("Reverted to your global setting.", "info")
    else:
        visible = set(request.form.getlist("task_row_visible"))
        hidden = [key for key, _, _ in TASK_ROW_FIELDS if key not in visible]
        user.task_row_hidden_by_view[view_key] = hidden
        user.save()
        flash("Display options saved for this view.", "success")
    return redirect(request.form.get("next") or request.referrer or url_for("dashboard.home"))


@settings_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    """Per-user account + preferences.

    Account fields (display name, email) and preference fields
    (timezone, retention, task-row display) live on one page so the user
    has a single Settings landing area. Password change has its own
    endpoint below.
    """
    try:
        from zoneinfo import available_timezones
        all_tzs = sorted(available_timezones())
    except ImportError:
        all_tzs = ["UTC"]
    user = current_user._get_current_object()
    if request.method == "POST":
        # Account: display name + email. Username intentionally not
        # exposed — email is the identifier.
        display_name = (request.form.get("display_name") or "").strip()[:128]
        email = (request.form.get("email") or "").strip().lower()
        if not email:
            flash("Email is required.", "danger")
            return redirect(url_for("settings.profile"))
        if email != user.email:
            existing = User.objects(email=email, pk__ne=user.pk).first()
            if existing:
                flash("That email is already in use.", "danger")
                return redirect(url_for("settings.profile"))
            user.email = email
        user.display_name = display_name or None
        # Other names this account answers to when something is imported — one
        # per line, matched case-insensitively. Normalised here so the matcher
        # (services.permissions.assignee_index) can stay a plain dict lookup.
        seen = set()
        aliases = []
        for line in (request.form.get("import_aliases") or "").splitlines():
            alias = line.strip().lower()[:200]
            if alias and alias not in seen:
                seen.add(alias)
                aliases.append(alias)
        user.import_aliases = aliases[:20]

        tz = (request.form.get("timezone") or "").strip()
        if tz and tz not in all_tzs:
            flash("Unknown timezone.", "danger")
            return redirect(url_for("settings.profile"))
        user.timezone = tz
        # Working-day window for the Today "Plan" timeline / auto-plan.
        # Clamp to 0..24 and keep start < end (fall back to 9–18).
        try:
            pds = max(0, min(23, int(request.form.get("plan_day_start") or 9)))
        except ValueError:
            pds = 9
        try:
            pde = max(1, min(24, int(request.form.get("plan_day_end") or 18)))
        except ValueError:
            pde = 18
        if pde <= pds:
            pds, pde = 9, 18
        user.plan_day_start = pds
        user.plan_day_end = pde
        try:
            user.plan_buffer_min = max(0, min(120, int(request.form.get("plan_buffer_min") or 0)))
        except ValueError:
            user.plan_buffer_min = 0
        ls = _minutes_of_day(request.form.get("plan_lunch_start"))
        le = _minutes_of_day(request.form.get("plan_lunch_end"))
        # start == end (or end < start, or either one empty) → no lunch break.
        user.plan_lunch_start_min, user.plan_lunch_end_min = (ls, le) if le > ls else (0, 0)
        user.focus_on_login = bool(request.form.get("focus_on_login"))
        user.editor_vim_mode = bool(request.form.get("editor_vim_mode"))
        user.caldav_calendar_enabled = bool(request.form.get("caldav_calendar_enabled"))
        cal_src = (request.form.get("caldav_calendar_source") or "due").strip()
        user.caldav_calendar_source = cal_src if cal_src in ("due", "plan") else "due"
        # Form posts the visible fields. Anything missing from that set
        # is added to the hidden blocklist.
        visible = set(request.form.getlist("task_row_visible"))
        user.task_row_hidden = [
            key for key, _, _ in TASK_ROW_FIELDS if key not in visible
        ]
        # Premium/team only: free accounts keep their plan window, which is
        # part of what the plan is rather than a preference. Checked against the
        # live level, so a lapsed subscription starts sweeping again.
        user.keep_completed_forever = bool(
            user.is_premium and request.form.get("keep_completed_forever") == "1")
        try:
            user.completed_task_retention_days = max(
                int(request.form.get("retention_days") or 0), 0
            )
        except ValueError:
            user.completed_task_retention_days = 0
        try:
            user.trash_retention_days = max(
                int(request.form.get("trash_retention_days") or 0), 0
            )
        except ValueError:
            user.trash_retention_days = 30
        user.save()
        flash("Profile saved.", "success")
        return redirect(url_for("settings.profile"))
    # Pull and CONSUME the just-minted plaintext token (set when the
    # user POSTed /settings/api-tokens/new) so we render it exactly
    # once. After that we only have the hash equivalents.
    from flask import session as flask_session
    fresh_token = flask_session.pop("new_api_token", None)
    fresh_token_name = flask_session.pop("new_api_token_name", None)
    return render_template(
        "settings/profile.html",
        timezones=all_tzs,
        server_default=current_app.config.get("DEFAULT_TZ") or "UTC",
        task_row_fields=TASK_ROW_FIELDS,
        fresh_token=fresh_token,
        fresh_token_name=fresh_token_name,
    )


@settings_bp.route("/export")
@login_required
def export_data():
    """Download a JSON backup of all your own data.

    Credential-free (no password hash, integration tokens or OAuth blobs) —
    a portability/backup export, not a secrets dump. Re-importable by an
    operator via ``cairn import-user``.
    """
    import io
    import json
    from application.services.backup import build_export

    user = current_user._get_current_object()
    bundle = build_export(user, include_trashed=True, redact_secrets=True)
    buf = io.BytesIO(json.dumps(bundle, indent=2).encode("utf-8"))
    buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return send_file(
        buf,
        mimetype="application/json",
        as_attachment=True,
        download_name=f"cairn-export-{user.username}-{stamp}.json",
    )


@settings_bp.route("/api-tokens/new", methods=["POST"])
@login_required
def api_token_new():
    """Mint a fresh API token. The plaintext value is shown ONCE on
    the next profile render — after that only the metadata is kept."""
    from flask import session as flask_session
    user = current_user._get_current_object()
    name = (request.form.get("name") or "default").strip()[:128] or "default"
    read_only = bool(request.form.get("read_only"))
    tok, raw = user.issue_api_token(name=name, read_only=read_only)
    # Stash the plaintext so the profile page can reveal it once;
    # nothing else on the server retains it.
    flask_session["new_api_token"] = raw
    flask_session["new_api_token_name"] = tok.name
    flash(f"Token '{tok.name}' minted. Copy it now — it won't be shown again.", "success")
    return redirect(url_for("settings.profile") + "#api-tokens")


@settings_bp.route("/api-tokens/<token_id>/revoke", methods=["POST"])
@login_required
def api_token_revoke(token_id):
    user = current_user._get_current_object()
    user.api_tokens = [t for t in (user.api_tokens or [])
                       if str(t.pk_ref) != token_id]
    user.save()
    flash("Token revoked.", "info")
    return redirect(url_for("settings.profile") + "#api-tokens")


def _cli_loopback_port(raw) -> int | None:
    """Validate a loopback-redirect port from the CLI authorize flow.

    Only unprivileged ports are accepted; the redirect target is always
    forced to 127.0.0.1 so this can never be turned into an open redirect.
    """
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return None
    return port if 1024 <= port <= 65535 else None


# App URL schemes allowed as a cli-authorize redirect target. Kept to a
# fixed allow-list so this can never be turned into a token-exfiltration
# / open-redirect primitive (an attacker can't aim the token at an
# arbitrary scheme of their choosing).
ALLOWED_APP_SCHEMES = {"cairn"}


@settings_bp.route("/cli-authorize", methods=["GET", "POST"])
@login_required
def cli_authorize():
    """Browser leg of the CLI / mobile-app login flow (gh-style).

    The caller opens this URL with ``state`` (a nonce it will verify),
    an optional token ``name``, and exactly one redirect target:

    * ``port=<n>``      — desktop CLI loopback → ``http://127.0.0.1:<n>/``
    * ``scheme=cairn``  — iOS/iPadOS app → ``cairn://auth`` (intercepted by
      ``ASWebAuthenticationSession``)

    The user is already authenticated via the web session; on approval we
    mint an API token and 302 back to the target carrying the raw token +
    state. The token is never rendered on a Cairn page — it only lives in
    the redirect.
    """
    port = _cli_loopback_port(request.values.get("port"))
    scheme = (request.values.get("scheme") or "").strip().lower()
    use_scheme = scheme in ALLOWED_APP_SCHEMES
    state = (request.values.get("state") or "").strip()[:128]
    name = (request.values.get("name") or "CLI").strip()[:128] or "CLI"
    if not port and not use_scheme:
        abort(400, "missing or invalid redirect target")

    if request.method == "POST":
        user = current_user._get_current_object()
        _tok, raw = user.issue_api_token(name=name)
        query = urllib.parse.urlencode({"token": raw, "state": state})
        if use_scheme:
            return redirect(f"{scheme}://auth?{query}")
        return redirect(f"http://127.0.0.1:{port}/?{query}")

    return render_template(
        "settings/cli_authorize.html",
        port=port, state=state, name=name,
        scheme=scheme if use_scheme else "",
    )


@settings_bp.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    """Let a logged-in user change their password.

    Requires the current password to confirm. Different code path from
    the public ``/auth/reset-password`` flow (that one needs a mailed
    token; this one trusts the live session)."""
    user = current_user._get_current_object()
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not user.check_password(current):
            flash("Current password is wrong.", "danger")
        elif len(new) < 8:
            flash("New password must be at least 8 characters.", "danger")
        elif new != confirm:
            flash("New passwords don't match.", "danger")
        else:
            user.set_password(new)
            user.save()
            flash("Password updated.", "success")
            return redirect(url_for("settings.change_password"))
    return render_template("settings/password.html")


@settings_bp.route("/subscription")
@login_required
def subscription():
    """Show the user's account level + what each level unlocks.

    Read-only: upgrading is a global-admin action via the CLI
    (``./cairn set-level``). The page tells the user how to request it.

    Gone entirely where no plans are sold — there is one tier there, and a
    page listing what the other ones would cost only raises the question."""
    from application.services.billing import subscriptions_enabled
    from application.services.permissions import FREE_PROJECT_LIMIT
    if not subscriptions_enabled():
        abort(404)
    user = current_user._get_current_object()
    tiers = [
        {
            "key": "free",
            "label": "Free",
            "blurb": "Solo use, capped at a small project count.",
            "limits": [
                f"Up to {FREE_PROJECT_LIMIT} projects",
                "No sharing or team features",
                "Full access to perspectives, tags, calendar read",
            ],
        },
        {
            "key": "premium",
            "label": "Premium",
            "blurb": "Solo, no project cap. Can join a team.",
            "limits": [
                "Unlimited projects",
                "Can be invited into a team",
                "Calendar write-back",
            ],
        },
        {
            "key": "team_admin",
            "label": "Team admin",
            "blurb": "Owns one team and can invite members.",
            "limits": [
                "Unlimited projects",
                "Owns a team — invite members, share projects",
                "Team-wide assignment + visibility",
            ],
        },
        {
            "key": "global_admin",
            "label": "Global admin",
            "blurb": "Operator role. Grants levels to other users.",
            "limits": [
                "Everything in team_admin",
                "User admin + level management",
            ],
        },
    ]
    return render_template(
        "settings/subscription.html",
        tiers=tiers,
        current_level=user.level,
    )


@settings_bp.route("/perspectives", methods=["GET", "POST"])
@login_required
def perspectives():
    """Pick which sidebar perspectives are visible and in what order.

    POST = reorder (SortableJS posts the new order of the visible list).
    Toggling visibility on individual entries goes to a dedicated endpoint
    so it's a single click without juggling the whole list client-side.
    """
    from application.services.perspectives import ALL_PERSPECTIVES, for_user as _for_user

    user = current_user._get_current_object()

    if request.method == "POST":
        order = request.form.getlist("visible[]") or request.form.getlist("visible")
        valid_keys = {p.key for p in ALL_PERSPECTIVES}
        ordered, seen = [], set()
        for k in order:
            if k in valid_keys and k not in seen:
                ordered.append(k); seen.add(k)
        user.sidebar_perspectives = ordered
        user.save()
        flash("Sidebar order saved.", "success")
        return redirect(url_for("settings.perspectives"))

    visible = _for_user(user)
    visible_keys_set = {p.key for p in visible}
    hidden = [p for p in ALL_PERSPECTIVES if p.key not in visible_keys_set]
    return render_template(
        "settings/perspectives.html",
        visible=visible, hidden=hidden,
    )


@settings_bp.route("/perspectives/toggle/<key>", methods=["POST"])
@login_required
def perspective_toggle(key):
    """One-click show/hide for a perspective."""
    from application.services.perspectives import ALL_PERSPECTIVES, KEYS

    user = current_user._get_current_object()
    if key not in KEYS:
        abort(404)
    current = list(user.sidebar_perspectives) if user.sidebar_perspectives else list(KEYS)
    if key in current:
        current = [k for k in current if k != key]
    else:
        current.append(key)
    user.sidebar_perspectives = current
    user.save()
    return redirect(url_for("settings.perspectives"))


@settings_bp.route("/perspectives/reset", methods=["POST"])
@login_required
def perspective_reset():
    """Drop all customisation — sidebar reverts to the canonical default."""
    user = current_user._get_current_object()
    user.sidebar_perspectives = []
    user.save()
    flash("Sidebar reset to default.", "info")
    return redirect(url_for("settings.perspectives"))


@settings_bp.route("/inspector-layout", methods=["GET", "POST"])
@login_required
def inspector_layout():
    """Order + show/hide the task inspector's sections (global, per-user).

    POST: SortableJS posts the visible sections in their new order via
    ``visible[]``; anything not listed is hidden. The pinned ``title``
    section is never part of this.
    """
    from application.services.inspector_layout import (
        SECTION_KEYS, SECTION_LABELS, resolved_layout,
    )
    user = current_user._get_current_object()

    if request.method == "POST":
        valid = set(SECTION_KEYS)
        # Full order of all sections (visible or not) + the hidden blocklist.
        order, seen = [], set()
        for k in (request.form.getlist("order[]") or request.form.getlist("order")):
            if k in valid and k not in seen:
                order.append(k)
                seen.add(k)
        hidden = [k for k in request.form.getlist("hidden[]") if k in valid]
        user.inspector_layout = order
        user.inspector_hidden = hidden
        user.save()
        flash("Inspector layout saved.", "success")
        return redirect(url_for("settings.inspector_layout"))

    layout = [
        {"key": k, "label": SECTION_LABELS[k], "visible": vis}
        for k, vis in resolved_layout(user)
    ]
    return render_template("settings/inspector_layout.html", layout=layout)


@settings_bp.route("/inspector-layout/order", methods=["POST"])
@login_required
def inspector_layout_order():
    """Save a new section order dragged in the inspector itself.

    The same setting the layout page writes, from the place the sections
    actually are — so arranging them doesn't mean going somewhere else and
    imagining the result. Only the order moves here; showing and hiding stays
    on the settings page, where there is room to explain it."""
    from application.services.inspector_layout import reordered, resolved_layout

    user = current_user._get_current_object()
    shown = request.form.getlist("order[]") or request.form.getlist("order")
    order = reordered(user, shown)
    if order != [k for k, _ in resolved_layout(user)]:
        user.inspector_layout = order
        user.save()
    return ("", 204)


@settings_bp.route("/inspector-layout/reset", methods=["POST"])
@login_required
def inspector_layout_reset():
    """Revert the inspector to the default section order, all visible."""
    user = current_user._get_current_object()
    user.inspector_layout = []
    user.inspector_hidden = []
    user.save()
    flash("Inspector layout reset to default.", "info")
    return redirect(url_for("settings.inspector_layout"))


@settings_bp.route("/import/todoist", methods=["GET", "POST"])
@login_required
def todoist():
    user = current_user._get_current_object()
    form = TodoistImportForm()
    stats = None

    # Pre-fill the preference toggles from the saved user profile so the
    # form always shows the current state.
    if request.method == "GET":
        form.save_token.data = bool(user.todoist_token)
        form.show_sync_button.data = bool(user.show_todoist_sync)

    if form.validate_on_submit():
        # Token comes from the form, or — if the field is empty and the
        # user has one stored — from the saved (encrypted) profile.
        from application.services.crypto import decrypt_for_user, encrypt_for_user
        saved = decrypt_for_user(user, user.todoist_token)
        token = (form.token.data or "").strip() or saved
        if not token:
            flash("Please paste a Todoist token (or save one first).", "danger")
            return render_template("settings/todoist.html", form=form, stats=None)
        try:
            stats = import_for_user(
                user,
                token,
                include_completed=form.include_completed.data,
                include_comments=form.include_comments.data,
                dry_run=form.dry_run.data,
            )
        except TodoistError as exc:
            flash(str(exc), "danger")
        else:
            if form.dry_run.data:
                flash("Dry-run complete — no changes were written.", "info")
            else:
                flash("Import finished.", "success")
                user.last_todoist_sync_at = datetime.now(timezone.utc)
            # Persist token / preference changes (encrypted at rest).
            if form.save_token.data:
                user.todoist_token = encrypt_for_user(user, token)
            elif not form.save_token.data and user.todoist_token and form.token.data:
                # User typed a token but unchecked save → drop saved one.
                user.todoist_token = ""
            user.show_todoist_sync = bool(form.show_sync_button.data)
            user.save()
    return render_template("settings/todoist.html", form=form, stats=stats)


@settings_bp.route("/import/zammad", methods=["GET", "POST"])
@login_required
def zammad():
    from application.models.item import Item
    from mongoengine import Q
    from application.services.crypto import decrypt_for_user, encrypt_for_user

    user = current_user._get_current_object()
    form = ZammadImportForm()
    # Project picker holds active projects the user owns.
    projects = list(
        Item.objects(
            (Q(owner=user) | Q(members=user)),
            kind="project", status="active",
        ).order_by("+name")
    )
    form.project.choices = [("", "— Inbox (no project) —")] + [
        (str(p.pk), p.name) for p in projects
    ]

    stats = None
    if request.method == "GET":
        form.enabled.data = bool(user.zammad_enabled)
        form.url.data = user.zammad_url or ""
        form.save_credentials.data = bool(user.zammad_url and user.zammad_token)
        form.show_sync_button.data = bool(user.show_zammad_sync)
        form.auto_sync.data = bool(user.zammad_auto_sync)
        form.project.data = user.zammad_sync_project or ""
        form.assignee_only.data = bool(user.zammad_assignee_only)
        form.include_closed.data = bool(user.zammad_include_closed)
        form.import_time.data = bool(user.zammad_import_time)
        form.auto_customer.data = bool(user.zammad_time_auto_customer)
        form.time_from.data = user.zammad_time_from or ""

    def _persist_options():
        """The sync options, saved wherever this form was submitted from."""
        user.zammad_enabled = bool(form.enabled.data)
        user.show_zammad_sync = bool(form.show_sync_button.data)
        # Persist the import options so the background auto-sync replays
        # the same choices (project, scope) the user picked here.
        user.zammad_auto_sync = bool(form.auto_sync.data)
        user.zammad_sync_project = form.project.data or ""
        user.zammad_assignee_only = bool(form.assignee_only.data)
        user.zammad_include_closed = bool(form.include_closed.data)
        user.zammad_import_time = bool(form.import_time.data)
        user.zammad_time_auto_customer = bool(form.auto_customer.data)
        # Empty is not "import everything" — see zammad_import.default_time_from.
        user.zammad_time_from = (_valid_day(form.time_from.data)
                                 or default_time_from(user))
        user.save()

    if form.validate_on_submit():
        url = (form.url.data or "").strip() or user.zammad_url
        saved_token = decrypt_for_user(user, user.zammad_token) if user.zammad_token else ""
        token = (form.token.data or "").strip() or saved_token
        # "Save settings" is the way to turn the time import (or anything else
        # here) on and off without pulling tickets — the switches shouldn't
        # need a sync to take effect.
        if form.save_only.data:
            if url and token and form.save_credentials.data:
                user.zammad_url = url
                user.zammad_token = encrypt_for_user(user, token)
            _persist_options()
            flash("Settings saved.", "success")
            return redirect(url_for("settings.zammad"))
        if not form.enabled.data:
            # Saying "don't sync for me" and then pressing Import is a
            # contradiction; the switch wins, and says so.
            _persist_options()
            flash("Your own Zammad sync is switched off — nothing was fetched. "
                  "Turn it back on above if you want to import yourself.",
                  "warning")
            return redirect(url_for("settings.zammad"))
        if not url or not token:
            flash("Please supply both a Zammad URL and an API token.", "danger")
            return render_template("settings/zammad.html", form=form, stats=None,
                                   **_zammad_agent_context(user))
        target_project = None
        if form.project.data:
            target_project = Item.objects(pk=form.project.data, owner=user).first()
        # Apply the time-import choices to this very run (they are persisted
        # below once the sync succeeded).
        user.zammad_import_time = bool(form.import_time.data)
        user.zammad_time_auto_customer = bool(form.auto_customer.data)
        user.zammad_time_from = (_valid_day(form.time_from.data)
                                 or default_time_from(user))
        try:
            stats = zammad_import_for_user(
                user, url, token,
                project=target_project,
                assignee_only=form.assignee_only.data,
                include_closed=form.include_closed.data,
                dry_run=form.dry_run.data,
                full=form.full_sync.data,
                debug=True,  # populate the per-ticket trace for the debug console
            )
        except ZammadError as exc:
            flash(str(exc), "danger")
        else:
            if form.dry_run.data:
                flash("Dry-run complete — no changes were written.", "info")
            else:
                flash("Zammad import finished.", "success")
            if form.save_credentials.data:
                user.zammad_url = url
                user.zammad_token = encrypt_for_user(user, token)
            _persist_options()
            if form.auto_sync.data and not (user.zammad_url and user.zammad_token):
                flash("Auto-sync needs saved credentials — tick "
                      "“Remember URL + token” so the background worker can "
                      "authenticate.", "warning")
            if stats.time_users_unmatched:
                flash("These Zammad agents have no Cairn account yet — their "
                      "time is booked under their Zammad name until you assign "
                      "them below: " + ", ".join(stats.time_users_unmatched),
                      "warning")
    return render_template("settings/zammad.html", form=form, stats=stats,
                           **_zammad_agent_context(user))


def _valid_day(raw) -> str:
    """``"2026-08-20"`` or ``""`` — anything unparseable means "no cutoff".

    A silently wrong date here would quietly drop hours, so only a real ISO day
    is accepted."""
    from datetime import date as _date
    value = (raw or "").strip()
    if not value:
        return ""
    try:
        return _date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def _zammad_agent_context(user) -> dict:
    """The agent roster shown under the form: who Zammad booked time as, and
    who that is in Cairn. Empty for a solo user — there is no team to bill."""
    from application.services.permissions import team_members
    from application.services.timetracking import default_team
    from application.services.zammad_billing import agent_rows

    team = default_team(user)
    if team is None:
        return {"team": None, "agents": [], "members": [], "can_map": False,
                "team_syncs": False}
    return {
        "team": team,
        # Whether this team fetches Zammad centrally — if it does, a personal
        # sync fetches the same tickets a second time, and the page should say
        # so rather than let two runs quietly race.
        "team_syncs": bool(team.zammad_url and team.zammad_token),
        "agents": agent_rows(team),
        "members": sorted(team_members(team),
                          key=lambda m: (m.display_name or m.username or "").lower()),
        "can_map": team.is_managed_by(user),
    }


@settings_bp.route("/import/zammad/agents", methods=["POST"])
@login_required
def zammad_agent_map():
    """Point one Zammad agent at a Cairn account — and re-file the time already
    booked under their bare Zammad name."""
    from application.services.permissions import team_members
    from application.services.timetracking import default_team
    from application.services.zammad_billing import assign_agent

    user = current_user._get_current_object()
    team = default_team(user)
    if team is None or not team.is_managed_by(user):
        abort(403)
    zammad_id = (request.form.get("zammad_id") or "").strip()
    target_id = (request.form.get("user_id") or "").strip()
    target = None
    if target_id:
        target = User.objects(pk=target_id).first()
        if target is None or not any(str(m.pk) == target_id
                                     for m in team_members(team)):
            flash("That account isn't a member of this team.", "danger")
            return redirect(url_for("settings.zammad"))
    agent, fixed = assign_agent(team, zammad_id, target)
    if agent is None:
        flash("Unknown Zammad agent.", "warning")
    elif target is None:
        flash(f"“{agent.label}” is no longer mapped to a Cairn account"
              + (f" — {fixed} entr{'y' if fixed == 1 else 'ies'} re-filed under "
                 f"their Zammad name." if fixed else "."), "info")
    else:
        flash(f"“{agent.label}” → {target.display_name or target.username}"
              + (f" — {fixed} existing entr{'y' if fixed == 1 else 'ies'} "
                 f"re-attributed." if fixed else "."), "success")
    return redirect(url_for("settings.zammad"))


@settings_bp.route("/sync/zammad", methods=["POST"])
@login_required
def sync_zammad():
    """One-shot Zammad sync triggered by the navbar Sync icon."""
    from application.services.crypto import decrypt_for_user
    user = current_user._get_current_object()
    if not user.zammad_enabled:
        return (
            jsonify({"error": "Your own Zammad sync is switched off "
                              "(Settings → Import from Zammad)."}),
            400,
        )
    if not user.zammad_url or not user.zammad_token:
        return (
            jsonify({"error": "Zammad isn't configured yet — set URL + token in Settings."}),
            400,
        )
    token = decrypt_for_user(user, user.zammad_token)
    if not token:
        return (
            jsonify({"error": "Saved Zammad token couldn't be decrypted. Re-enter it under Settings."}),
            400,
        )
    try:
        stats = zammad_import_for_user(user, user.zammad_url, token)
    except ZammadError as exc:
        return jsonify({"error": str(exc)}), 502
    user.last_zammad_sync_at = datetime.now(timezone.utc)
    user.save()
    return jsonify({
        "ok": True,
        "stats": stats.as_dict(),
        "last_sync": user.last_zammad_sync_at.isoformat(),
    })


@settings_bp.route("/notes-mirror", methods=["GET", "POST"])
@login_required
def notes_mirror():
    """Configure the notes ⇄ WebDAV mirror, test it, and start a round."""
    from application.services import notes_sync, sync_log
    from application.services.crypto import encrypt_for_user
    from application.services.webdav import DavError

    user = current_user._get_current_object()
    form = NotesMirrorForm()
    probe = ""

    if form.validate_on_submit():
        user.notes_mirror_url = (form.url.data or "").strip().rstrip("/")
        user.notes_mirror_user = (form.username.data or "").strip()
        if form.password.data:
            user.notes_mirror_password = encrypt_for_user(user, form.password.data)
        user.notes_mirror_interval = int(form.interval.data or 0)
        user.notes_mirror_enabled = bool(form.enabled.data)
        user.notes_mirror_tasks = bool(form.tasks.data)
        user.save()
        if form.test.data:
            try:
                probe = notes_sync.client_for(user).check()
            except DavError as exc:
                flash(f"Couldn't reach the share: {exc}", "danger")
            else:
                flash(probe, "success")
        else:
            flash("Saved.", "success")
        return redirect(url_for("settings.notes_mirror"))

    if request.method == "GET":
        form.enabled.data = user.notes_mirror_enabled
        form.url.data = user.notes_mirror_url
        form.username.data = user.notes_mirror_user
        form.interval.data = str(user.notes_mirror_interval or 0)
        form.tasks.data = user.notes_mirror_tasks

    return render_template(
        "settings/notes_mirror.html", form=form, user=user,
        has_password=bool(user.notes_mirror_password),
        runs=sync_log.recent(users=[user], limit=10),
    )


def _mirror_state(user) -> dict:
    """What the last round left behind. Two buttons start a round and then
    wait for this: a round ends by stamping ``last_notes_mirror_at``, or by
    writing a failure into the status, so either one moving is the answer
    "it has landed" — and there is no second store to keep in step with it."""
    at = user.last_notes_mirror_at
    return {
        "at": at.isoformat() if at else "",
        "when": current_app.jinja_env.filters["timeago"](at) if at else "",
        "status": user.notes_mirror_status or "",
    }


@settings_bp.route("/notes-mirror/state")
@login_required
def notes_mirror_state():
    """Polled while a round runs in the worker (see :func:`_mirror_state`)."""
    return jsonify(_mirror_state(current_user._get_current_object()))


@settings_bp.route("/notes-mirror/sync", methods=["POST"])
@login_required
def notes_mirror_sync():
    """Start a round now. A first round can write hundreds of files, which
    outlives any gateway, so it goes to the worker when there is one and runs
    here when there isn't (same fallback as the Zammad syncs)."""
    from application.services import notes_sync
    from application.services.webdav import DavError

    user = current_user._get_current_object()
    # Asked from the notes navigator rather than from the settings form: answer
    # in place. Sending someone to a settings page and back for a sync they
    # started while reading a note is a detour, and it costs them their scroll
    # position and their browser history.
    inline = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def done(message: str, level: str = "info", ok: bool = True,
             queued: bool = False):
        """Answer the caller. ``queued`` says the round has been handed to a
        worker and has NOT run yet — everything this can report about it is
        still the previous round's, which is why the page then waits for
        ``notes_mirror_state`` rather than showing what it has."""
        if not inline:
            flash(message, level)
            return redirect(url_for("settings.notes_mirror"))
        user.reload()
        return jsonify({"ok": ok, "message": message, "level": level,
                        "queued": queued, **_mirror_state(user)})

    if not (user.notes_mirror_url or "").strip():
        return done("Set the WebDAV folder first.", "warning", ok=False)
    try:
        from application.services.sync_tasks import enqueue_notes_mirror
        enqueue_notes_mirror(str(user.pk), trigger="manual")
        return done("Sync started — it runs in the background.", "info",
                    queued=True)
    except Exception:                          # no broker, or it's down
        try:
            stats = notes_sync.sync_user(user, trigger="manual")
        except DavError as exc:
            return done(f"Sync failed: {exc}", "danger", ok=False)
        if not inline:
            for err in stats.errors:
                flash(err, "warning")
        return done(stats.summary(), "success")


@settings_bp.route("/calendars")
@login_required
def calendars():
    user = current_user._get_current_object()
    cals = sorted(user.calendars or [], key=lambda c: c.name.lower())
    google_ready = bool(
        current_app.config.get("GOOGLE_CLIENT_ID")
        and current_app.config.get("GOOGLE_CLIENT_SECRET")
    )
    writable = [c for c in cals if c.can_write]
    return render_template(
        "settings/calendars.html",
        calendars=cals,
        writable=writable,
        google_ready=google_ready,
        ical_form=ICalForm(),
        caldav_form=CalDavForm(),
    )


@settings_bp.route("/calendars/ical/new", methods=["POST"])
@login_required
def calendar_ical_new():
    """Verify the iCal URL works, auto-detect the calendar name, save."""
    from application.services.calendar_sync import probe_ical

    user = current_user._get_current_object()
    form = ICalForm()
    if not form.validate_on_submit():
        flash("Please paste a valid iCal URL.", "danger")
        return redirect(url_for("settings.calendars"))

    url = form.ical_url.data.strip()
    try:
        probe = probe_ical(url)
    except CalendarError as exc:
        flash(f"Couldn't read that calendar: {exc}", "danger")
        return redirect(url_for("settings.calendars"))

    name = form.name.data.strip() or probe.get("name") or "Calendar"
    color = form.color.data or probe.get("color") or "#458588"
    user.add_calendar(
        provider="ical",
        name=name,
        ical_url=url,
        color=color,
        show_in_today=form.show_in_today.data,
        can_write=False,
    )
    flash(f"Subscribed to “{name}” — {probe.get('event_count', 0)} events read.", "success")
    return redirect(url_for("settings.calendars"))


@settings_bp.route("/calendars/caldav/new", methods=["POST"])
@login_required
def calendar_caldav_new():
    """Connect the calendars behind a CalDAV address (read/write).

    The address may be one calendar's collection URL or just the server's —
    ``discover_caldav_calendars`` walks CalDAV's discovery chain down from
    whatever was pasted, so the common case (a server address and an app
    password) connects every calendar on the account in one go. Submitting the
    same address again is how a calendar added later gets picked up: the ones
    already here are skipped by URL.

    The password is encrypted per-user before storage, exactly like the Google
    OAuth tokens. CalDAV calendars are ``can_write=True`` so they can be picked
    as the write-back target."""
    from application.models.calendar import ExternalCalendar
    from application.services.calendar_sync import discover_caldav_calendars
    from application.services.crypto import encrypt_for_user

    user = current_user._get_current_object()
    form = CalDavForm()
    if not form.validate_on_submit():
        flash("Please fill in the CalDAV URL, username and password.", "danger")
        return redirect(url_for("settings.calendars"))

    url = form.caldav_url.data.strip()
    username = form.username.data.strip()
    password = form.password.data
    try:
        root, found = discover_caldav_calendars(url, username, password)
    except CalendarError as exc:
        flash(f"Couldn't reach that calendar: {exc}", "danger")
        return redirect(url_for("settings.calendars"))
    if not found:
        flash("No calendars found at that address.", "warning")
        return redirect(url_for("settings.calendars"))

    known = {(c.caldav_url or "").rstrip("/")
             for c in (user.calendars or []) if c.provider == "caldav"}
    secret = encrypt_for_user(user, password)
    # The colour picker always holds a value, so "the user chose one" can only
    # mean "it isn't the default any more" — otherwise the server's own colour
    # wins, which is what keeps a dozen discovered calendars apart.
    picked = (form.color.data or "").strip()
    if picked.lower() == "#458588":
        picked = ""
    added = []
    for entry in found:
        if entry["url"].rstrip("/") in known:
            continue
        # A typed name can only mean the one calendar the user was looking at.
        name = ((form.name.data or "").strip() if len(found) == 1 else "") \
            or entry["name"]
        user.calendars.append(ExternalCalendar(
            provider="caldav",
            name=name,
            color=picked or entry["color"] or "#458588",
            # A whole account's worth of calendars in Today is a wall of
            # events nobody asked for, so only the default one starts visible
            # — the rest are one click away in the list above.
            show_in_today=bool(form.show_in_today.data and entry["default"]),
            can_write=True,
            caldav_url=entry["url"],
            caldav_home=root,
            caldav_username=username,
            caldav_password=secret,
        ))
        added.append(name)
    if not added:
        flash("Those calendars are already connected.", "info")
        return redirect(url_for("settings.calendars"))
    user.save()
    if len(added) == 1:
        flash(f"Connected to \u201c{added[0]}\u201d.", "success")
    else:
        flash(f"Connected {len(added)} calendars: " + ", ".join(added)
              + ". Pick which of them show in Today above.", "success")
    return redirect(url_for("settings.calendars"))


@settings_bp.route("/booking", methods=["GET", "POST"])
@login_required
def booking():
    """Configure the public booking page (``/book/<slug>``).

    Hand-parsed rather than a WTForm: the bulk of it is seven weekday rows
    and two checkbox lists over the user's calendars, which a declarative
    form would have to rebuild dynamically anyway. The CSRF token is checked
    the same way the other settings forms here do it.

    Everything is validated before anything is written, because half-saved
    opening hours are a page that quietly offers the wrong times."""
    from application.models.booking import (
        WEEKDAYS, Booking, BookingHour, BookingPhoto,
    )

    user = current_user._get_current_object()
    page = user.booking
    if request.method == "POST":
        errors = _read_booking_form(user, page, request.form)
        # The photo is stored separately from the page's fields, but it is
        # refused for the same reasons, so it is validated in the same pass.
        photo_error = _store_booking_photo(user, request)
        if photo_error:
            errors.append(photo_error)
        if errors:
            for err in errors:
                flash(err, "danger")
        else:
            user.save()
            flash("Booking page saved.", "success")
            return redirect(url_for("settings.booking"))

    cals = sorted(user.calendars or [], key=lambda c: c.name.lower())
    upcoming = Booking.objects(
        owner=user, start__gte=datetime.now(timezone.utc)).order_by("start")[:20]
    return render_template(
        "settings/booking.html",
        page=page, calendars=cals,
        writable=[c for c in cals if c.can_write],
        weekdays=list(enumerate(WEEKDAYS)),
        hours_by_day={d: page.windows_for(d) for d in range(7)},
        default_hour=BookingHour(),
        upcoming=upcoming,
        has_photo=bool(BookingPhoto.objects(owner=user).only("id").first()),
        # Shown whether or not the page is live: an unsaved slug has no link
        # yet, but a configured one always has, and that is what gets handed
        # out.
        public_url=(url_for("booking.page", slug=page.slug, _external=True)
                    if page.slug else ""),
    )


def _read_booking_form(user, page, form) -> list:
    """Fill ``page`` from the posted form. Returns a list of complaints; on a
    non-empty list nothing about the page should be saved."""
    import re

    from application.models.booking import WEEKDAYS, BookingHour
    from application.models.user import User

    errors = []
    slug = (form.get("slug") or "").strip().lower()
    enabled = bool(form.get("enabled"))
    if slug and not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,38}", slug):
        errors.append("The link name may use lowercase letters, digits and "
                      "dashes only, and must start with a letter or digit.")
    elif slug and User.objects(booking__slug=slug, pk__ne=user.pk).first():
        errors.append("Somebody else already uses that link name.")
    if enabled and not slug:
        errors.append("A public page needs a link name.")

    durations = []
    for raw in (form.get("durations") or "").replace(";", ",").split(","):
        raw = raw.strip()
        if not raw:
            continue
        if not raw.isdigit() or not (5 <= int(raw) <= 480):
            errors.append(f"“{raw}” isn't a length in minutes between 5 and 480.")
            continue
        durations.append(int(raw))
    if enabled and not durations:
        errors.append("Offer at least one appointment length.")

    hours = []
    for day in range(7):
        if not form.get(f"day-{day}"):
            continue
        start = _hhmm(form.get(f"start-{day}"))
        end = _hhmm(form.get(f"end-{day}"))
        if start is None or end is None:
            errors.append(f"{WEEKDAYS[day]}: please give both times as HH:MM.")
            continue
        if end <= start:
            errors.append(f"{WEEKDAYS[day]}: the end has to be after the start.")
            continue
        hours.append(BookingHour(weekday=day, start_minute=start, end_minute=end))
    if enabled and not hours:
        errors.append("Pick at least one day you're bookable.")

    write_id = (form.get("write_calendar_id") or "").strip()
    if write_id and not user.find_calendar(write_id):
        errors.append("The calendar for new appointments no longer exists.")
    # Deliberately NOT required for a live page. Insisting on one meant that
    # anybody without a writable calendar connected could not switch their
    # page on at all — the link just kept answering 404. Without a target the
    # appointment is still taken and still shown under "Upcoming bookings";
    # the page says so instead of blocking.
    busy_ids = [cid for cid in form.getlist("busy_calendar_ids")
                if user.find_calendar(cid)]

    if errors:
        return errors

    page.enabled = enabled
    page.listed = bool(form.get("listed"))
    page.slug = slug
    page.title = (form.get("title") or "").strip()[:120]
    page.intro = (form.get("intro") or "").strip()[:2000]
    page.durations = sorted(set(durations))
    page.days_ahead = _clamp(form.get("days_ahead"), 1, 365, 30)
    page.hours = hours
    page.busy_calendar_ids = busy_ids
    page.write_calendar_id = write_id
    page.min_notice_hours = _clamp(form.get("min_notice_hours"), 0, 720, 0)
    page.buffer_minutes = _clamp(form.get("buffer_minutes"), 0, 240, 0)
    page.max_per_day = _clamp(form.get("max_per_day"), 0, 50, 0)
    return []


# A portrait is shown at 72px on the page and 64px in the directory, so it is
# stored at 320 square: crisp on a retina screen, a few dozen KB, and the same
# shape for everyone no matter what came out of the phone.
BOOKING_PHOTO_PX = 320
BOOKING_PHOTO_MAX_BYTES = 8 * 1024 * 1024


def _store_booking_photo(user, req) -> str:
    """Save, replace or remove the booking portrait. Returns a complaint, or
    an empty string when there was nothing wrong (including nothing to do)."""
    from application.models.booking import BookingPhoto
    from application.views.tasks import _sniff_image_type

    if req.form.get("remove_photo"):
        BookingPhoto.objects(owner=user).delete()
        return ""
    fileobj = req.files.get("photo")
    if not fileobj or not fileobj.filename:
        return ""
    raw = fileobj.read(BOOKING_PHOTO_MAX_BYTES + 1)
    if len(raw) > BOOKING_PHOTO_MAX_BYTES:
        return (f"That photo is larger than "
                f"{BOOKING_PHOTO_MAX_BYTES // 1024 // 1024} MB.")
    if not _sniff_image_type(raw[:12]):
        # Sniffed, not taken from the filename — and SVG stays out, it can
        # carry script and this file is served to the public.
        return "That file isn't a PNG, JPEG, GIF or WebP image."
    try:
        data = _square_webp(raw, BOOKING_PHOTO_PX)
    except Exception as exc:  # noqa: BLE001 — a broken upload is the user's news
        return f"That image couldn't be read: {exc}"
    BookingPhoto.objects(owner=user).delete()
    BookingPhoto(owner=user, data=data, content_type="image/webp",
                 updated_at=datetime.now(timezone.utc)).save()
    return ""


def _square_webp(raw: bytes, size: int) -> bytes:
    """Centre-crop to a square and re-encode as WebP."""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    img.load()
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    side = min(img.width, img.height)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize(
        (size, size), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="WEBP", quality=82, method=4)
    return out.getvalue()


def _hhmm(value):
    """``"09:30"`` → 570. ``None`` when it isn't a time of day."""
    parts = (value or "").strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 24 or minutes > 59 or hours * 60 + minutes > 24 * 60:
        return None
    return hours * 60 + minutes


def _clamp(value, low, high, fallback):
    try:
        return max(low, min(high, int(str(value).strip())))
    except (TypeError, ValueError):
        return fallback


@settings_bp.route("/calendars/caldav/refresh", methods=["POST"])
@login_required
def calendar_caldav_refresh():
    """Re-ask each connected CalDAV account for its calendars.

    The Google equivalent of this button reuses the OAuth grant; here the
    stored credentials do the same job, so a calendar created on the server
    since arrives without anyone typing a URL again. Names and colours of the
    rows already here are refreshed, new calendars arrive hidden (appearing in
    Today unasked is the one thing a refresh must not do), and a calendar that
    is gone from the server loses its row — the list is the server's list.

    Removal is deliberately narrow, because "not in the answer" is also what a
    half-broken connection looks like: only calendars under a home that just
    answered with a listing are dropped, only from that connection, and a
    connection whose refresh failed leaves every one of its rows alone.

    One connection is one ``(username, home)`` pair: several accounts, or
    several servers, are refreshed each on their own."""
    from application.models.calendar import ExternalCalendar
    from application.services.calendar_sync import discover_caldav_calendars
    from application.services.crypto import decrypt_for_user

    user = current_user._get_current_object()
    cals = [c for c in (user.calendars or []) if c.provider == "caldav"]
    if not cals:
        flash("Connect a CalDAV calendar first.", "warning")
        return redirect(url_for("settings.calendars"))

    groups: dict = {}
    for cal in cals:
        # Calendars connected before the home was recorded: the collection's
        # parent is the home on every server that lays them out that way, and
        # the discovery walk corrects it from there if it isn't.
        home = cal.caldav_home or urllib.parse.urljoin(cal.caldav_url or "", "..")
        groups.setdefault((cal.caldav_username or "", home), []).append(cal)

    by_url = {(c.caldav_url or "").rstrip("/"): c for c in cals}
    created = updated = 0
    dropped: list = []
    errors = []
    for (username, home), members in groups.items():
        seed = members[0]
        try:
            root, found = discover_caldav_calendars(
                home, username, decrypt_for_user(user, seed.caldav_password) or "")
        except CalendarError as exc:
            errors.append(f"{home}: {exc}")
            continue
        alive = {e["url"].rstrip("/") for e in found}
        dropped.extend(c for c in members
                       if (c.caldav_url or "").rstrip("/") not in alive)
        for entry in found:
            existing = by_url.get(entry["url"].rstrip("/"))
            if existing:
                existing.name = entry["name"] or existing.name
                existing.color = entry["color"] or existing.color
                existing.caldav_home = root
                updated += 1
                continue
            user.calendars.append(ExternalCalendar(
                provider="caldav",
                name=entry["name"],
                color=entry["color"] or "#458588",
                show_in_today=False,
                can_write=True,
                caldav_url=entry["url"],
                caldav_home=root,
                caldav_username=username,
                caldav_password=seed.caldav_password,
            ))
            created += 1
    if dropped:
        gone = {str(c.pk_ref) for c in dropped}
        user.calendars = [c for c in user.calendars if str(c.pk_ref) not in gone]
        # The write target may have been one of them — a target that no longer
        # exists silently stops writing, so it is cleared rather than kept.
        if user.calendar_write_target and not user.find_calendar(
                user.calendar_write_target):
            user.calendar_write_target = ""
    user.save()
    for err in errors:
        flash(f"Refresh failed: {err}", "danger")
    if created or updated or dropped:
        flash(f"Refreshed: {created} added, {updated} updated, "
              f"{len(dropped)} removed.", "success")
    return redirect(url_for("settings.calendars"))


@settings_bp.route("/calendars/google/disconnect", methods=["POST"])
@login_required
def calendar_google_disconnect():
    """Remove all Google calendars + tokens for this user.

    Doesn't revoke the OAuth grant on Google's side (user can do that at
    https://myaccount.google.com/permissions). On reconnect, Cairn just
    re-runs the consent flow.
    """
    user = current_user._get_current_object()
    before = len(user.calendars or [])
    user.calendars = [c for c in (user.calendars or []) if c.provider != "google"]
    count = before - len(user.calendars)
    # Clear write-target if it pointed to a Google calendar that's now gone.
    if user.calendar_write_target and not user.find_calendar(user.calendar_write_target):
        user.calendar_write_target = ""
    user.save()
    flash(f"Disconnected. {count} Google calendar entry(ies) removed.", "info")
    return redirect(url_for("settings.calendars"))


@settings_bp.route("/calendars/google/refresh", methods=["POST"])
@login_required
def calendar_google_refresh_list():
    """Re-pull the Google calendar list using the existing OAuth grant.

    Materialises new ExternalCalendar rows for calendars added on the
    Google side since the user connected. Existing rows get their name,
    colour and accessRole refreshed.
    """
    from application.services.calendar_sync import google_list_calendars

    user = current_user._get_current_object()
    seed = next(
        (c for c in (user.calendars or []) if c.provider == "google"),
        None,
    )
    if not seed:
        flash("Connect Google first.", "warning")
        return redirect(url_for("settings.calendars"))
    try:
        items = google_list_calendars(seed, user)
    except CalendarError as exc:
        flash(f"Refresh failed: {exc}", "danger")
        return redirect(url_for("settings.calendars"))

    by_calid = {c.calendar_id: c for c in (user.calendars or []) if c.provider == "google"}
    created = updated = 0
    seen_ids = set()
    from application.models.calendar import ExternalCalendar
    for it in items:
        gcal_id = it.get("id")
        if not gcal_id:
            continue
        seen_ids.add(gcal_id)
        access_role = it.get("accessRole", "")
        can_write = access_role in ("owner", "writer")
        name = it.get("summaryOverride") or it.get("summary") or "Calendar"
        color = it.get("backgroundColor") or "#458588"
        existing = by_calid.get(gcal_id)
        if existing:
            existing.name = name
            existing.color = color
            existing.can_write = can_write
            if "@" in gcal_id:
                existing.account_email = gcal_id
            updated += 1
        else:
            user.calendars.append(ExternalCalendar(
                provider="google", name=name, color=color,
                calendar_id=gcal_id,
                show_in_today=bool(it.get("primary")),
                can_write=can_write,
                account_email=gcal_id if "@" in gcal_id else "",
                oauth_refresh_token=seed.oauth_refresh_token,
                oauth_access_token=seed.oauth_access_token,
                oauth_token_expires_at=seed.oauth_token_expires_at,
                oauth_scope=seed.oauth_scope,
            ))
            created += 1

    # Drop the placeholder "primary" row if it doesn't match anything real.
    if "primary" not in seen_ids:
        user.calendars = [
            c for c in user.calendars
            if not (c.provider == "google" and c.calendar_id == "primary")
        ]

    user.save()
    flash(f"Refreshed: {created} added, {updated} updated.", "success")
    return redirect(url_for("settings.calendars"))


@settings_bp.route("/calendars/write-settings", methods=["POST"])
@login_required
def calendar_write_settings():
    """Persist which calendar Cairn writes back to and which date triggers it."""
    user = current_user._get_current_object()
    target = (request.form.get("write_target") or "").strip()
    if target:
        cal = user.find_calendar(target)
        if not cal or not cal.can_write:
            flash("Invalid write target.", "danger")
            return redirect(url_for("settings.calendars"))
    user.calendar_write_target = target
    user.calendar_write_due = bool(request.form.get("write_due"))
    user.calendar_write_plan = bool(request.form.get("write_plan"))
    user.save()
    flash("Write-back preferences saved.", "success")
    return redirect(url_for("settings.calendars"))


@settings_bp.route("/calendars/<cal_id>/toggle-today", methods=["POST"])
@login_required
def calendar_toggle_today(cal_id):
    user = current_user._get_current_object()
    cal = user.find_calendar(cal_id)
    if not cal:
        abort(404)
    cal.show_in_today = not cal.show_in_today
    user.save()
    return redirect(url_for("settings.calendars"))


@settings_bp.route("/calendars/<cal_id>/test", methods=["POST"])
@login_required
def calendar_test(cal_id):
    """Manually fetch today's events to verify the connection works."""
    from datetime import timedelta
    user = current_user._get_current_object()
    cal = user.find_calendar(cal_id)
    if not cal:
        abort(404)
    now = datetime.now(timezone.utc)
    try:
        evs = fetch_events(cal, now, now + timedelta(days=7), user=user)
        flash(f"Fetched {len(evs)} event(s) from “{cal.name}”.", "success")
    except CalendarError as exc:
        flash(f"Failed: {exc}", "danger")
    return redirect(url_for("settings.calendars"))


@settings_bp.route("/calendars/<cal_id>/delete", methods=["POST"])
@login_required
def calendar_delete(cal_id):
    user = current_user._get_current_object()
    if not user.remove_calendar(cal_id):
        abort(404)
    flash("Calendar removed.", "info")
    return redirect(url_for("settings.calendars"))


# -------- Google OAuth flow --------------------------------------------------


@settings_bp.route("/calendars/google/connect")
@login_required
def calendar_google_connect():
    user = current_user._get_current_object()
    redirect_uri = url_for("settings.calendar_google_callback", _external=True)
    state = secrets.token_urlsafe(24)
    session["google_oauth_state"] = state
    try:
        url = google_oauth_authorize_url(redirect_uri, state, user=user)
    except CalendarError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("settings.calendars"))
    # Print to server log so the user can diagnose Google's "unknownerror"
    # by comparing the actual params we send (especially redirect_uri).
    current_app.logger.info("Google OAuth redirect_uri=%s, full URL=%s", redirect_uri, url)
    # ?debug=1 short-circuits to a page showing the URL so you can inspect it.
    if request.args.get("debug") == "1":
        return (
            f"<pre>redirect_uri = {redirect_uri}\n\nauth URL =\n{url}</pre>"
            f"<p><a href='{url}'>Continue to Google</a></p>"
        )
    return redirect(url)


@settings_bp.route("/calendars/google/callback")
@login_required
def calendar_google_callback():
    user = current_user._get_current_object()
    expected = session.pop("google_oauth_state", None)
    if not expected or request.args.get("state") != expected:
        flash("OAuth state mismatch — please retry.", "danger")
        return redirect(url_for("settings.calendars"))
    if request.args.get("error"):
        # Google sends ``error`` (short code) + ``error_description`` (human
        # readable). Show both so the user knows what to fix.
        err = request.args.get("error")
        desc = request.args.get("error_description") or ""
        flash(f"Google declined: {err}{' — ' + desc if desc else ''}", "danger")
        return redirect(url_for("settings.calendars"))
    code = request.args.get("code")
    if not code:
        flash("No authorisation code received.", "danger")
        return redirect(url_for("settings.calendars"))

    redirect_uri = url_for("settings.calendar_google_callback", _external=True)
    try:
        token_data = google_oauth_exchange_code(code, redirect_uri, user=user)
    except CalendarError as exc:
        flash(f"Token exchange failed: {exc}", "danger")
        return redirect(url_for("settings.calendars"))

    refresh = token_data.get("refresh_token")
    access = token_data.get("access_token")
    expires = token_data.get("expires_in", 3600)
    scope = token_data.get("scope", "")

    from application.services.crypto import encrypt_for_user
    from application.services.calendar_sync import google_list_calendars
    from datetime import timedelta

    enc_refresh = encrypt_for_user(user, refresh) if refresh else None
    enc_access = encrypt_for_user(user, access)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires) - 30)

    # Pick any existing google calendar for this user just to ride the
    # newly-fetched tokens for the calendarList call.
    from application.models.calendar import ExternalCalendar
    google_cals = [c for c in (user.calendars or []) if c.provider == "google"]
    seed = google_cals[0] if google_cals else None
    if seed is None:
        seed = ExternalCalendar(
            provider="google", name="Google Calendar",
            calendar_id="primary", color="#458588", show_in_today=True,
            can_write=True,
            oauth_refresh_token=enc_refresh,
            oauth_access_token=enc_access,
            oauth_token_expires_at=expires_at,
            oauth_scope=scope,
        )
        user.calendars.append(seed)
    else:
        if enc_refresh:
            seed.oauth_refresh_token = enc_refresh
        seed.oauth_access_token = enc_access
        seed.oauth_token_expires_at = expires_at
        seed.oauth_scope = scope
        seed.last_error = ""
    user.save()

    # Materialise one embedded calendar per real Google calendar (or
    # update names/colour/can_write if it already exists). Tokens are
    # shared (same OAuth grant), so they're copied to each entry.
    try:
        items = google_list_calendars(seed, user)
    except CalendarError as exc:
        flash(f"Connected, but listing calendars failed: {exc}", "warning")
        return redirect(url_for("settings.calendars"))

    by_calid = {c.calendar_id: c for c in user.calendars if c.provider == "google"}
    created = updated = 0
    for it in items:
        gcal_id = it.get("id")
        if not gcal_id:
            continue
        access_role = it.get("accessRole", "")
        can_write = access_role in ("owner", "writer")
        name = it.get("summaryOverride") or it.get("summary") or "Calendar"
        color = it.get("backgroundColor") or "#458588"
        existing = by_calid.get(gcal_id)
        if existing:
            existing.name = name
            existing.color = color
            existing.can_write = can_write
            if enc_refresh:
                existing.oauth_refresh_token = enc_refresh
            existing.oauth_access_token = enc_access
            existing.oauth_token_expires_at = expires_at
            existing.oauth_scope = scope
            if "@" in gcal_id:
                existing.account_email = gcal_id
            updated += 1
        else:
            user.calendars.append(ExternalCalendar(
                provider="google", name=name, color=color,
                calendar_id=gcal_id,
                show_in_today=bool(it.get("primary")),
                can_write=can_write,
                account_email=gcal_id if "@" in gcal_id else "",
                oauth_refresh_token=enc_refresh,
                oauth_access_token=enc_access,
                oauth_token_expires_at=expires_at,
                oauth_scope=scope,
            ))
            created += 1

    # Set a default write target on first connect.
    if not user.calendar_write_target:
        primary_id = next((it.get("id") for it in items if it.get("primary")), None)
        if primary_id:
            wt = next(
                (c for c in user.calendars
                 if c.provider == "google" and c.calendar_id == primary_id),
                None,
            )
            if wt:
                user.calendar_write_target = str(wt.pk_ref)
                user.calendar_write_due = True

    # Drop the seed row if it's a leftover from the placeholder
    # "primary" that doesn't actually appear in the user's list.
    if seed.calendar_id == "primary" and not any(
        i.get("id") == "primary" for i in items
    ):
        user.calendars = [
            c for c in user.calendars
            if not (c is seed or
                    (c.provider == "google" and c.calendar_id == "primary"))
        ]
    user.save()

    flash(
        f"Google Calendar connected. {created} new calendar(s), "
        f"{updated} refreshed. Pick read + write below.",
        "success",
    )
    return redirect(url_for("settings.calendars"))


@settings_bp.route("/sync/todoist", methods=["POST"])
@login_required
def sync_todoist():
    """One-shot sync triggered by the navbar Sync icon."""
    from application.services.crypto import decrypt_for_user
    user = current_user._get_current_object()
    if not user.todoist_token:
        return (
            jsonify({"error": "No Todoist token saved. Configure it under Settings."}),
            400,
        )
    token = decrypt_for_user(user, user.todoist_token)
    if not token:
        return (
            jsonify({"error": "Saved Todoist token couldn't be decrypted. "
                              "Re-enter it under Settings."}),
            400,
        )
    try:
        stats = import_for_user(
            user,
            token,
            include_completed=False,
            include_comments=True,
        )
    except TodoistError as exc:
        return jsonify({"error": str(exc)}), 502
    user.last_todoist_sync_at = datetime.now(timezone.utc)
    user.save()
    return jsonify({
        "ok": True,
        "stats": stats.as_dict(),
        "last_sync": user.last_todoist_sync_at.isoformat(),
    })
