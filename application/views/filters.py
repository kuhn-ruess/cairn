"""Jinja filters and globals."""
from datetime import datetime, timezone

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

        from application import APP_VERSION
        return {
            "now_utc": lambda: datetime.now(timezone.utc),
            "user_projects": projects,
            "sidebar_perspectives": sidebar_perspectives,
            "sidebar_custom_perspectives": sidebar_custom_perspectives,
            "static_version": static_version,
            "project_tree": _project_tree,
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
                "a": ["href", "title"],
                "img": ["src", "alt", "title"],
            },
            protocols=["http", "https", "mailto"],
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
