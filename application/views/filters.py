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
    # Bust browser caches whenever the process boots (dev) or new deploys (prod).
    import time as _time
    static_version = str(int(_time.time()))

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

        from application import APP_VERSION
        return {
            "now_utc": lambda: datetime.now(timezone.utc),
            "user_projects": projects,
            "sidebar_perspectives": sidebar_perspectives,
            "sidebar_custom_perspectives": sidebar_custom_perspectives,
            "static_version": static_version,
            "project_tree": _project_tree,
            "can_view_container": _can_view_container,
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
