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


def _a_attr_ok(tag, name, value):
    if name == "title":
        return True
    if name != "href":
        return False
    if not _SCHEME_RE.match(value):
        return True  # relative/internal link
    return value.startswith(("http://", "https://", "mailto:"))


def register_filters(app):
    # Bust browser caches whenever the process boots (prod / new deploys).
    import os as _os
    import time as _time
    boot_version = str(int(_time.time()))

    def _static_version():
        # In debug, derive the cache-buster from the newest js/css mtime so
        # editing a static asset busts the browser cache on the next page
        # load — no process restart needed. (The --debug reloader only
        # restarts on .py changes, which would otherwise leave the boot-time
        # version stale and serve cached JS.) Prod keeps the boot value.
        if not app.debug:
            return boot_version
        newest = 0.0
        for sub in ("js", "css"):
            base = _os.path.join(app.static_folder or "", sub)
            for root, _dirs, files in _os.walk(base):
                for f in files:
                    try:
                        m = _os.path.getmtime(_os.path.join(root, f))
                    except OSError:
                        continue
                    if m > newest:
                        newest = m
        return str(int(newest)) if newest else boot_version

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

        projects = []
        if current_user.is_authenticated:
            user = current_user._get_current_object()
            # Folders are organisational only — you can't put tasks inside them.
            projects = list(
                Item.objects(
                    (Q(owner=user) | Q(members=user)),
                    status="active",
                    kind__ne="folder",
                ).order_by("+name")
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
        # Project tree for the right-hand OmniFocus-style sidebar — only
        # populated when a template asks for it (cheap query but cuts a
        # round-trip if nothing renders it).
        def _project_tree():
            if not current_user.is_authenticated:
                return []
            from application.views.projects import project_tree_for_sidebar
            return project_tree_for_sidebar(current_user._get_current_object())

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
            from flask import g
            cache = getattr(g, "_viewable_container_pks", None)
            if cache is None:
                user = current_user._get_current_object()
                # Containers the user owns or is a direct member of.
                pks = {
                    str(pk) for pk in Item.objects(
                        (Q(owner=user) | Q(members=user)),
                        kind__in=["project", "folder", "section"],
                    ).values_list("pk")
                }
                # Plus children of folders the user is a member of (parent
                # membership grants child visibility — mirror of
                # Item.can_be_viewed_by).
                member_folders = Item.objects(
                    members=user, kind__in=["folder", "project"],
                ).values_list("pk")
                member_folders = list(member_folders)
                if member_folders:
                    pks |= {
                        str(pk) for pk in Item.objects(
                            parent__in=member_folders,
                            kind__in=["project", "folder", "section"],
                        ).values_list("pk")
                    }
                cache = g._viewable_container_pks = pks
            return pk_str in cache

        # Inspector section order (per-user). Available to every template
        # that renders the inspector partial without threading it through
        # each view.
        inspector_section_order = []
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

        from application import APP_VERSION
        return {
            "now_utc": lambda: datetime.now(timezone.utc),
            "user_projects": projects,
            "nav_counts": nav_counts,
            "sidebar_perspectives": sidebar_perspectives,
            "sidebar_custom_perspectives": sidebar_custom_perspectives,
            "static_version": _static_version(),
            "project_tree": _project_tree,
            "item_icon": _item_icon,
            "can_view_container": _can_view_container,
            "inspector_section_order": inspector_section_order,
            "app_version": APP_VERSION,
        }

    @app.template_filter("localdt")
    def localdt(value):
        if not value:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M")

    @app.template_filter("date")
    def date(value):
        if not value:
            return ""
        return value.strftime("%Y-%m-%d")

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
            protocols=["http", "https", "mailto", "data"],
            strip=True,
        )
        # Auto-link bare URLs / emails that weren't written with Markdown
        # link syntax (e.g. a pasted https://… or an app link). Existing
        # <a> tags are left alone by linkify; code/pre are skipped so URLs
        # in snippets stay literal. Open in a new tab, no referrer leak.
        from bleach.callbacks import nofollow, target_blank
        cleaned = bleach.linkify(
            cleaned, callbacks=[nofollow, target_blank], skip_tags=["pre", "code"],
        )
        return Markup(cleaned)

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
