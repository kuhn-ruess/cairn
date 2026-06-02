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
    ZammadError, import_for_user as zammad_import_for_user,
)

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")


class ICalForm(FlaskForm):
    ical_url = StringField("Secret iCal URL", validators=[DataRequired(), URL()])
    name = StringField("Name (optional)", validators=[Optional(), Length(max=128)])
    color = StringField("Color", default="#458588")
    show_in_today = BooleanField("Show events in Today", default=True)
    submit = SubmitField("Subscribe")




class TodoistImportForm(FlaskForm):
    token = PasswordField("Todoist API token", validators=[Optional()])
    save_token = BooleanField("Remember this token for future syncs")
    show_sync_button = BooleanField("Show a one-click Sync button in the toolbar")
    include_completed = BooleanField("Include completed tasks")
    include_comments = BooleanField("Include comments", default=True)
    dry_run = BooleanField("Dry run (don't write changes)")
    submit = SubmitField("Import")


class ZammadImportForm(FlaskForm):
    url = StringField(
        "Zammad URL",
        validators=[Optional(), URL()],
        description="Base URL of your Zammad instance, e.g. https://zammad.example.com",
    )
    token = PasswordField("Zammad API token", validators=[Optional()])
    save_credentials = BooleanField("Remember URL + token for future syncs")
    show_sync_button = BooleanField("Show a one-click Sync button in the toolbar")
    project = SelectField("Import into project", choices=[], validators=[Optional()])
    assignee_only = BooleanField("Only tickets assigned to me", default=True)
    include_closed = BooleanField("Include closed tickets")
    dry_run = BooleanField("Dry run (don't write changes)")
    submit = SubmitField("Import")


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

        tz = (request.form.get("timezone") or "").strip()
        if tz and tz not in all_tzs:
            flash("Unknown timezone.", "danger")
            return redirect(url_for("settings.profile"))
        user.timezone = tz
        # Form posts the visible fields. Anything missing from that set
        # is added to the hidden blocklist.
        visible = set(request.form.getlist("task_row_visible"))
        user.task_row_hidden = [
            key for key, _, _ in TASK_ROW_FIELDS if key not in visible
        ]
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
    tok, raw = user.issue_api_token(name=name)
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
    (``./cairn set-level``). The page tells the user how to request it."""
    from application.services.permissions import FREE_PROJECT_LIMIT
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
        form.url.data = user.zammad_url or ""
        form.save_credentials.data = bool(user.zammad_url and user.zammad_token)
        form.show_sync_button.data = bool(user.show_zammad_sync)

    if form.validate_on_submit():
        url = (form.url.data or "").strip() or user.zammad_url
        saved_token = decrypt_for_user(user, user.zammad_token) if user.zammad_token else ""
        token = (form.token.data or "").strip() or saved_token
        if not url or not token:
            flash("Please supply both a Zammad URL and an API token.", "danger")
            return render_template("settings/zammad.html", form=form, stats=None)
        target_project = None
        if form.project.data:
            target_project = Item.objects(pk=form.project.data, owner=user).first()
        try:
            stats = zammad_import_for_user(
                user, url, token,
                project=target_project,
                assignee_only=form.assignee_only.data,
                include_closed=form.include_closed.data,
                dry_run=form.dry_run.data,
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
            user.show_zammad_sync = bool(form.show_sync_button.data)
            user.save()
    return render_template("settings/zammad.html", form=form, stats=stats)


@settings_bp.route("/sync/zammad", methods=["POST"])
@login_required
def sync_zammad():
    """One-shot Zammad sync triggered by the navbar Sync icon."""
    from application.services.crypto import decrypt_for_user
    user = current_user._get_current_object()
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
