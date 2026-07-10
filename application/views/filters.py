"""Jinja filters and globals."""
import re
from datetime import datetime, timezone

# Raster image data-URI prefixes we accept inline in note/comment markdown.
# SVG is deliberately excluded — ``data:image/svg+xml`` can carry script.
_ALLOWED_DATA_IMG = (
    "data:image/png",
    "data:image/jpeg",
    "data:image/gif",
    "data:image/webp",
)
# A URL carries an explicit scheme when it matches this; schemeless values are
# relative/internal links (e.g. ``/static/…``) and are always safe.
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _img_attr_ok(tag, name, value):
    if name in ("alt", "title"):
        return True
    if name != "src":
        return False
    if value.startswith(_ALLOWED_DATA_IMG):
        return True  # inline (pasted, compressed) image stored in the item
    if not _SCHEME_RE.match(value):
        return True  # relative URL (legacy /static/uploads/… references)
    return value.startswith(("http://", "https://"))


def _link_target(attrs, new=False):
    """linkify callback: open links in a new tab, EXCEPT OS-handoff schemes
    (``mailto:`` / ``message:`` / ``tel:``). Those must navigate in place —
    opening them in a new tab just hands off to Mail/Phone and leaves an empty
    tab behind to close. External links also get ``rel`` hardening."""
    href = attrs.get((None, "href"), "")
    if href.startswith(("mailto:", "message:", "tel:")):
        attrs.pop((None, "target"), None)
        attrs.pop((None, "rel"), None)
        return attrs
    attrs[(None, "target")] = "_blank"
    attrs[(None, "rel")] = "nofollow noopener noreferrer"
    return attrs


def _a_attr_ok(tag, name, value):
    if name == "title":
        return True
    if name != "href":
        return False
    if not _SCHEME_RE.match(value):
        return True  # relative/internal link
    # ``message:`` lets an Apple Mail message dragged into a note stay a
    # clickable link that reopens the mail (no script risk — it just hands
    # off to the Mail app). ``tel:`` is the same harmless hand-off shape.
    return value.startswith(
        ("http://", "https://", "mailto:", "message:", "tel:")
    )


def register_filters(app):
    # Bust browser caches whenever the process boots (prod / new deploys).
    import os as _os
    import time as _time
    boot_version = str(int(_time.time()))

    # Per-file cache-buster: `asset('css/app.css')` -> the static URL with a
    # `?v=<mtime>` derived from THAT file. So editing one asset busts only it,
    # on the next normal page load — no hard reload, no process restart, in dev
    # and prod alike. Prod caches the mtime (static is baked, so it's constant
    # until the next deploy); debug re-stats every call so live edits show up.
    from flask import url_for as _url_for
    _asset_versions = {}

    def _asset_version(filename):
        path = _os.path.join(app.static_folder or "", filename)
        try:
            return str(int(_os.path.getmtime(path)))
        except OSError:
            return boot_version

    def _asset(filename):
        if app.debug:
            v = _asset_version(filename)
        else:
            v = _asset_versions.get(filename)
            if v is None:
                v = _asset_versions[filename] = _asset_version(filename)
        return f"{_url_for('static', filename=filename)}?v={v}"

    app.jinja_env.globals["asset"] = _asset

    import re as _re
    from markupsafe import Markup as _Markup
    _HEX = _re.compile(r"^#[0-9A-Fa-f]{3,8}$")

    def _item_icon(item, cls=""):
        """Unified project / folder / section icon, used everywhere so the
        colours stay consistent across views. A custom ``item.color`` tints
        the icon itself (inline style overrides the default class colour) —
        no separate coloured dot replacing the icon.
        """
        if getattr(item, "is_folder", False):
            icon, klass = "bi-folder2-open", "text-warning"
        elif getattr(item, "is_section", False):
            icon, klass = "bi-bookmark", "text-info"
        elif getattr(item, "is_note", False):
            icon, klass = "bi-journal-text", "text-success"
        else:
            icon, klass = "bi-collection", "text-info"
        color = getattr(item, "color", "") or ""
        if _HEX.match(color):
            # Drop the default text-* colour class — Bootstrap's utilities set
            # `color: ... !important`, which would otherwise beat the inline
            # tint and the custom colour would never show.
            return _Markup(f'<i class="bi {icon} {cls}" style="color: {color};"></i>')
        return _Markup(f'<i class="bi {icon} {klass} {cls}"></i>')

    @app.context_processor
    def inject_helpers():
        from flask_login import current_user
        from mongoengine import Q

        from application.models.item import Item as Item

        # Container choices for the quick-add and bulk "Move to…" dropdowns.
        # Only *projects* are offered: folders are organisational (you can't file
        # a task in one) and sections are an intra-project detail the inspector's
        # own picker handles — surfacing them here as bare "Folder/Proj/Section"
        # paths just confused users. Labels carry the FULL folder breadcrumb
        # (e.g. "Work / Acme / Website"), resolved in-memory in one query — a
        # bare parent name mislabels anything nested 3+ levels deep.
        user_project_choices = []
        if current_user.is_authenticated:
            user = current_user._get_current_object()
            from application.services.containers import container_choices
            user_project_choices = container_choices(
                user, selectable_kinds=("project",),
                include_shared=True, active_projects_only=True,
            )
        # Sidebar perspectives — render from the user's custom list.
        sidebar_perspectives = []
        sidebar_custom_perspectives = []
        if current_user.is_authenticated:
            from application.services.perspectives import for_user as perspectives_for_user
            user = current_user._get_current_object()
            sidebar_perspectives = perspectives_for_user(user)
            sidebar_custom_perspectives = [
                p for p in (user.custom_perspectives or []) if p.in_sidebar
            ]
        # Sidebar favorites + the set of favorited tokens (so star buttons
        # anywhere on the page render filled/empty without extra queries).
        sidebar_favorites = []
        favorite_tokens = set()
        if current_user.is_authenticated:
            user = current_user._get_current_object()
            favorite_tokens = set(user.favorites or [])
            if favorite_tokens:
                from application.services.favorites import resolve_favorites
                sidebar_favorites = resolve_favorites(user)

        # Project tree for the right-hand OmniFocus-style sidebar — only
        # populated when a template asks for it (cheap query but cuts a
        # round-trip if nothing renders it).
        def _project_tree():
            if not current_user.is_authenticated:
                return []
            from application.views.projects import project_tree_for_sidebar
            return project_tree_for_sidebar(current_user._get_current_object())

        # Notes tree for the notes navigator — same lazy pattern.
        def _notes_tree():
            if not current_user.is_authenticated:
                return []
            from application.views.notes import notes_tree_for_sidebar
            return notes_tree_for_sidebar(current_user._get_current_object())

        def _can_view_container(item):
            """True if the current user may open ``item`` — a task's parent
            project/folder/section, given either as an Item or as a pk
            string. Used to decide whether to render the project badge as a
            link or as inert context: an assignee can see a task without
            being able to view its container.

            The set of viewable container pks is loaded once per request
            and cached on ``flask.g`` so this never queries inside a Jinja
            loop (see the per-page DB-load guidance)."""
            if not item or not current_user.is_authenticated:
                return False
            pk_str = str(item.pk) if hasattr(item, "pk") else str(item)
            from application.services.permissions import viewable_container_pks
            return pk_str in viewable_container_pks(
                current_user._get_current_object()
            )

        # Inspector section order (per-user). Available to every template
        # that renders the inspector partial without threading it through
        # each view.
        inspector_section_order = []
        from application.services.inspector_layout import SECTION_LABELS
        inspector_section_labels = dict(SECTION_LABELS)
        if current_user.is_authenticated:
            from application.services.inspector_layout import visible_order
            inspector_section_order = visible_order(
                current_user._get_current_object()
            )

        # Sidebar count badges — show how many items each key perspective
        # holds. Computed once per request (cheap count/narrow queries).
        nav_counts = {}
        if current_user.is_authenticated:
            user = current_user._get_current_object()
            from application.views.tasks import sidebar_counts
            from application.views.review import review_due_count
            _now = datetime.now(timezone.utc)
            # Only compute counts for perspectives actually in the sidebar —
            # a hidden view does no extra DB work.
            _visible = {p.key for p in sidebar_perspectives}
            nav_counts = sidebar_counts(user, _now, keys=_visible)
            nav_counts["review"] = review_due_count(user, _now)
            nav_counts["trash"] = Item.all_objects(
                owner=user, trashed_at__ne=None
            ).count()

        # Active OmniFocus-style focus (folder/project/section) — drives the
        # navbar chip and is available to every template. Validates + clears
        # stale focus internally; cached on flask.g.
        current_focus = None
        if current_user.is_authenticated:
            from application.services import focus as _focus
            current_focus = _focus.current(current_user._get_current_object())

        from application import APP_VERSION
        return {
            "now_utc": lambda: datetime.now(timezone.utc),
            "user_project_choices": user_project_choices,
            "current_focus": current_focus,
            "nav_counts": nav_counts,
            "sidebar_perspectives": sidebar_perspectives,
            "sidebar_custom_perspectives": sidebar_custom_perspectives,
            "sidebar_favorites": sidebar_favorites,
            "favorite_tokens": favorite_tokens,
            "project_tree": _project_tree,
            "notes_tree": _notes_tree,
            "item_icon": _item_icon,
            "item_storage": lambda item: item.storage_info(),
            "task_note_counts_for": lambda items: Item.note_counts_for(items),
            "can_view_container": _can_view_container,
            "inspector_section_order": inspector_section_order,
            "inspector_section_labels": inspector_section_labels,
            "app_version": APP_VERSION,
        }

    @app.template_filter("localdt")
    def localdt(value):
        if not value:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        # Localise to the browser/user timezone (see services.tzutil) so
        # stored-as-UTC values and calendar events read in *their* wall-clock,
        # not UTC. The filter is named "localdt" but never actually localised.
        try:
            from flask_login import current_user
            from application.services.tzutil import resolve_tz
            user = current_user if getattr(current_user, "is_authenticated", False) else None
            value = value.astimezone(resolve_tz(user))
        except Exception:
            pass
        return value.strftime("%Y-%m-%d %H:%M")

    @app.template_filter("dtlocalinput")
    def dtlocalinput(value):
        """Format an aware datetime as a user-tz ``datetime-local`` value
        (``YYYY-MM-DDTHH:MM``) for pre-filling ``<input type="datetime-local">``."""
        if not value:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        try:
            from flask_login import current_user
            from application.services.tzutil import resolve_tz
            user = current_user if getattr(current_user, "is_authenticated", False) else None
            value = value.astimezone(resolve_tz(user))
        except Exception:
            pass
        return value.strftime("%Y-%m-%dT%H:%M")

    @app.template_filter("date")
    def date(value):
        if not value:
            return ""
        return value.strftime("%Y-%m-%d")

    _STATUS_LABELS = {
        "open": "Open", "next": "Next", "waiting": "Waiting",
        "waiting_reply": "Waiting on reply",
        "scheduled": "Scheduled", "someday": "Someday", "done": "Done",
        "dropped": "Dropped",
    }

    @app.template_filter("status_label")
    def status_label(value):
        return _STATUS_LABELS.get(value, (value or "").replace("_", " "))

    @app.template_filter("duration")
    def duration(value):
        """Minutes → human duration: ``2h 30m`` / ``45m`` / ``3h`` / ``0m``."""
        total = int(value or 0)
        h, m = divmod(total, 60)
        if h and m:
            return f"{h}h {m}m"
        if h:
            return f"{h}h"
        return f"{m}m"

    def tt_task_time(task):
        """Inspector helper: billing project + this task's entries, or None.

        A Jinja global (not a context processor) so it's reachable inside the
        inspector macros. Returns None for non-team users or tasks whose subtree
        isn't linked to a billing project — the time section then renders nothing.
        """
        from flask_login import current_user
        from application.services.timetracking import task_time_context
        if not current_user.is_authenticated:
            return None
        return task_time_context(current_user._get_current_object(), task)

    app.jinja_env.globals["tt_task_time"] = tt_task_time

    @app.template_filter("eur")
    def eur(cents):
        """Integer cents → ``1.234,50 €`` (German formatting). ``None`` → ``—``."""
        if cents is None:
            return "—"
        whole, frac = divmod(int(cents), 100)
        s = f"{whole:,}".replace(",", ".")
        return f"{s},{frac:02d} €"

    @app.template_filter("markdown")
    def markdown_filter(value):
        """Render Markdown to safe HTML for note bodies + comments.

        Notes are stored as Markdown (importers convert HTML → Markdown
        before persisting), so the allowlist stays small: standard
        formatting tags only.
        """
        from markupsafe import Markup
        import bleach
        import markdown as _md

        if not value:
            return ""
        html = _md.markdown(
            str(value),
            extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
        )
        cleaned = bleach.clean(
            html,
            tags=[
                "p", "br", "strong", "em", "del", "ul", "ol", "li",
                "blockquote", "code", "pre", "h1", "h2", "h3", "h4",
                "h5", "h6", "a", "hr", "table", "thead", "tbody", "tr",
                "th", "td", "img",
            ],
            attributes={
                "a": _a_attr_ok,
                "img": _img_attr_ok,
            },
            # ``data`` is needed for the inline (pasted, compressed) images we
            # store in the note itself. It's only honoured on ``img src`` and
            # only for raster types — ``_img_attr_ok`` / ``_a_attr_ok`` gate
            # everything else so ``a href="data:…"`` (an XSS vector) can't slip
            # through.
            protocols=["http", "https", "mailto", "data", "message", "tel"],
            strip=True,
        )
        # Auto-link bare URLs / emails that weren't written with Markdown
        # link syntax (e.g. a pasted https://… or an app link). Existing
        # <a> tags are left alone by linkify; code/pre are skipped so URLs
        # in snippets stay literal. Open in a new tab, no referrer leak.
        cleaned = bleach.linkify(
            cleaned, callbacks=[_link_target], skip_tags=["pre", "code"],
        )
        return Markup(cleaned)

    @app.template_filter("timeago")
    def timeago(value):
        """Compact 'time since' for notification timestamps: now, 5m, 3h, 2d,
        then an absolute date once it's a week old."""
        if not value:
            return ""
        now = datetime.now(timezone.utc)
        v = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        secs = (now - v).total_seconds()
        if secs < 60:
            return "now"
        if secs < 3600:
            return f"{int(secs // 60)}m"
        if secs < 86400:
            return f"{int(secs // 3600)}h"
        if secs < 7 * 86400:
            return f"{int(secs // 86400)}d"
        return v.strftime("%b %-d")

    @app.template_filter("relativedate")
    def relativedate(value):
        if not value:
            return ""
        now = datetime.now(timezone.utc)
        v = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        delta = (v.date() - now.date()).days
        if delta == 0:
            return "today"
        if delta == 1:
            return "tomorrow"
        if delta == -1:
            return "yesterday"
        if delta < 0:
            return f"{-delta}d overdue"
        return f"in {delta}d"
