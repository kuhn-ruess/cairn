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
# Schemes that hand off to an OS app instead of navigating the browser. They
# must survive sanitising (they carry no script risk — the OS opens the app)
# AND must not get target="_blank", which would leave an empty tab behind.
_HANDOFF_SCHEMES = (
    "mailto:",
    "message:",  # Apple Mail message dragged into a note
    "tel:",
)
# Plus *any* ``x-…:`` scheme. Desktop apps register their deep links under this
# convention (x-devonthink-item:, x-things:, x-fantastical:, x-callback-url:, …)
# and enumerating them would mean a code change per app. The prefix is safe by
# construction: every scheme a browser can execute script through (javascript:,
# data:, vbscript:) is a fixed name that does NOT start with ``x-``.
_X_SCHEME_RE = re.compile(r"^x-[a-zA-Z0-9+.\-]*:")
# Same, but for pulling the schemes back out of sanitiser-bound HTML — bleach
# takes ``protocols`` as a fixed list, so we hand it the ones actually present.
_X_HREF_RE = re.compile(r"""href=["'](x-[a-zA-Z0-9+.\-]*):""", re.I)


def _is_handoff(url):
    """True for a URL that opens an OS app rather than a web page."""
    return url.startswith(_HANDOFF_SCHEMES) or bool(_X_SCHEME_RE.match(url))


# A rendered code block or code span, taken whole. Safe to read with a regex
# *here* and nowhere else: this only ever runs on bleach.clean output, whose
# tag set is a short allowlist and where an angle bracket inside the code is
# already an entity — so the first ``</pre>`` really is the end of the block.
_CODE_HTML_RE = re.compile(
    r"<pre\b[^>]*>.*?</pre>|<code\b[^>]*>.*?</code>", re.S | re.I)


def _linkify_outside_code(html, callbacks):
    """``bleach.linkify``, leaving code alone — without wrecking it.

    Its own ``skip_tags`` does not skip: it re-serializes the text it passed
    over, and escapes it a second time on the way out, so ``&lt;`` becomes
    ``&amp;lt;`` and a snippet of ``<<<placeholder>>>`` renders as entity soup.
    Dropping ``skip_tags`` keeps the entities but then links every URL inside a
    code sample, which is the thing it was there to prevent.

    So the code comes out first, the rest is linked, and the code goes back in
    exactly as it was. The marker carries a random tail: it has to be something
    linkify leaves alone (letters and digits — no dot, or it reads as a domain)
    that cannot collide with what somebody actually wrote."""
    import bleach
    from uuid import uuid4

    kept = []
    marker = "cairncodeslot" + uuid4().hex

    def stash(match):
        kept.append(match.group(0))
        return f"{marker}{len(kept) - 1}x"

    linked = bleach.linkify(_CODE_HTML_RE.sub(stash, html), callbacks=callbacks)
    for i, block in enumerate(kept):
        linked = linked.replace(f"{marker}{i}x", block, 1)
    return linked


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
    (see ``_is_handoff``). Those must navigate in place — opening them in a new
    tab just hands off to Mail/Phone/DEVONthink and leaves an empty tab behind
    to close. External links also get ``rel`` hardening."""
    href = attrs.get((None, "href"), "")
    # Internal links (a [[wiki link]] to another note, /tasks/<id>, /static/…)
    # are navigation inside Cairn, not an outbound jump — they stay in the tab.
    if _is_handoff(href) or not _SCHEME_RE.match(href):
        attrs.pop((None, "target"), None)
        attrs.pop((None, "rel"), None)
        return attrs
    attrs[(None, "target")] = "_blank"
    attrs[(None, "rel")] = "nofollow noopener noreferrer"
    return attrs


# ``- [ ] todo`` / ``- [x] done`` inside a note body. Python-Markdown has no
# task-list extension bundled, so the brackets survive into the rendered <li>
# and we swap them for a real checkbox here. ``data-md-task`` is the checkbox's
# ordinal in the SOURCE (same order markdown emits them), which is how the JS
# toggles the right line back in the Markdown without parsing it.
_MD_TASK_RE = re.compile(r"(<li[^>]*>)\s*(<p>\s*)?\[([ xX])\]\s*")


def _checkboxes(html: str, interactive: bool = False) -> str:
    """Turn the literal ``[ ]`` / ``[x]`` markers markdown left in list items
    into checkboxes. No-op for text that has none.

    Only the note body wires up a click handler (notes/detail.html), so
    everywhere else they render disabled — a checkbox you can tick that
    silently forgets is worse than one that plainly can't be ticked.
    """
    if "[" not in html:
        return html
    counter = [0]

    def repl(m):
        idx = counter[0]
        counter[0] += 1
        checked = " checked" if m.group(3).lower() == "x" else ""
        state = "" if interactive else " disabled"
        return (f'<li class="md-task">{m.group(2) or ""}'
                f'<input type="checkbox" data-md-task="{idx}"{checked}{state}> ')

    return _MD_TASK_RE.sub(repl, html)


def _a_attr_ok(tag, name, value):
    if name == "title":
        return True
    if name != "href":
        return False
    if not _SCHEME_RE.match(value):
        return True  # relative/internal link
    # Hand-off schemes let an Apple Mail message or a DEVONthink document
    # dragged into a note stay a clickable link that reopens it (no script risk
    # — it just hands off to the app).
    return value.startswith(("http://", "https://")) or _is_handoff(value)


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

    def _subscriptions_enabled():
        """Does this deployment sell plans? Everything that mentions money,
        levels or signing up asks here before it renders."""
        from application.services.billing import subscriptions_enabled
        return subscriptions_enabled()

    app.jinja_env.globals["subscriptions_enabled"] = _subscriptions_enabled

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
            glyph = f'<i class="bi {icon} {cls}" style="color: {color};"></i>'
        else:
            glyph = f'<i class="bi {icon} {klass} {cls}"></i>'
        # A shared folder/project says so on its icon: which of the rows in a
        # list somebody else can see is worth knowing before you click one.
        if getattr(item, "is_shared", False):
            glyph += ('<i class="bi bi-people-fill item-icon-share"'
                      ' title="Shared with others"></i>')
        return _Markup(glyph)

    @app.context_processor
    def inject_helpers():
        from flask_login import current_user
        from mongoengine import Q

        from application.models.item import Item as Item

        # Container choices for the quick-add and bulk "Move to…" dropdowns.
        # Projects and folders: a folder may hold tasks (see the Item
        # docstring), and both backends behind these menus have always accepted
        # one — only the menus didn't offer it, so the same folder was reachable
        # by drag-and-drop and not by "Move to…". Sections stay out: they are an
        # intra-project detail the inspector's own picker handles, and bare
        # "Folder/Proj/Section" paths here just confused people. Labels carry
        # the FULL folder breadcrumb (e.g. "Work / Acme / Website"), resolved
        # in-memory in one query — a bare parent name mislabels anything nested
        # 3+ levels deep.
        user_project_choices = []
        if current_user.is_authenticated:
            user = current_user._get_current_object()
            from application.services.containers import container_choices
            user_project_choices = container_choices(
                user, selectable_kinds=("project", "folder"),
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

        def _view_url(view):
            """Current URL with the ``view`` query param set (or cleared),
            preserving every other filter + route arg. Powers the List/Gantt
            toggle on perspective + project pages."""
            from flask import request, url_for
            args = request.args.to_dict(flat=True)
            if view:
                args["view"] = view
            else:
                args.pop("view", None)
            return url_for(request.endpoint, **(request.view_args or {}), **args)

        def _gantt_context(items):
            from application.services.gantt import build_gantt
            from application.services.tzutil import resolve_tz
            tz = (resolve_tz(current_user._get_current_object())
                  if current_user.is_authenticated else timezone.utc)
            return build_gantt(items, tz)

        from application import APP_VERSION
        return {
            "now_utc": lambda: datetime.now(timezone.utc),
            "gantt_context": _gantt_context,
            "view_url": _view_url,
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

    @app.template_filter("localdate")
    def localdate(value):
        """Calendar day of a stored datetime in the user's wall-clock — the
        template half of ``tzutil.local_date``. Use it on BOTH sides of a
        day comparison (``task.plan_date|localdate == now_utc()|localdate``):
        an all-day date is stored as midnight in the user's zone, so a bare
        ``.date()`` reads it as the previous day and the chip lights up a day
        early."""
        from application.services.tzutil import local_date, resolve_tz
        try:
            from flask_login import current_user
            user = current_user if getattr(current_user, "is_authenticated", False) else None
        except Exception:
            user = None
        return local_date(value, resolve_tz(user))

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
        "waiting_reply": "Waiting on reply", "review": "Review",
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

    def linked_notes_folder(item):
        """The notes folder an item belongs to (own link or inherited from an
        ancestor), or None — powers the "Notes" jump button on task/project
        pages. A Jinja global for the same reason as ``tt_task_time``: the
        detail templates need it outside any view's context. Never call it
        inside a list loop; it walks ancestors."""
        from application.services.containers import resolve_notes_folder
        try:
            return resolve_notes_folder(item)
        except Exception:
            return None

    app.jinja_env.globals["linked_notes_folder"] = linked_notes_folder

    @app.template_filter("ticketref")
    def ticketref(value):
        """A ticket reference as it is normally written: ``#4711`` (Zammad),
        ``CHMK-417`` (Jira). Keeps every view from re-deciding."""
        from application.services.timetracking import ref_label
        return ref_label(value)

    @app.template_filter("eur")
    def eur(cents):
        """Integer cents → ``1.234,50 €`` (German formatting). ``None`` → ``—``."""
        if cents is None:
            return "—"
        whole, frac = divmod(int(cents), 100)
        s = f"{whole:,}".replace(",", ".")
        return f"{s},{frac:02d} €"

    @app.template_filter("markdown")
    def markdown_filter(value, interactive_tasks=False):
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
        html = _checkboxes(html, interactive_tasks)
        cleaned = bleach.clean(
            html,
            tags=[
                "p", "br", "strong", "em", "del", "ul", "ol", "li",
                "blockquote", "code", "pre", "h1", "h2", "h3", "h4",
                "h5", "h6", "a", "hr", "table", "thead", "tbody", "tr",
                "th", "td", "img", "input",
            ],
            attributes={
                "a": _a_attr_ok,
                "img": _img_attr_ok,
                "input": ["type", "checked", "disabled", "data-md-task"],
                "li": ["class"],
                "ul": ["class"],
            },
            # ``data`` is needed for the inline (pasted, compressed) images we
            # store in the note itself. It's only honoured on ``img src`` and
            # only for raster types — ``_img_attr_ok`` / ``_a_attr_ok`` gate
            # everything else so ``a href="data:…"`` (an XSS vector) can't slip
            # through.
            # bleach gates href protocols separately from the _a_attr_ok
            # callback, so both must allow a scheme. ``protocols`` only takes a
            # fixed list — no wildcard — so scan the HTML for the ``x-…``
            # schemes it actually contains and allow exactly those.
            protocols=["http", "https", "data"]
            + [s.rstrip(":") for s in _HANDOFF_SCHEMES]
            + sorted({m.lower() for m in _X_HREF_RE.findall(html)}),
            strip=True,
        )
        # Auto-link bare URLs / emails that weren't written with Markdown
        # link syntax (e.g. a pasted https://… or an app link). Existing
        # <a> tags are left alone by linkify; code and pre stay literal, which
        # is what _linkify_outside_code is for (bleach's own skip_tags escapes
        # the code a second time). Open in a new tab, no referrer leak.
        cleaned = _linkify_outside_code(cleaned, [_link_target])
        return Markup(cleaned)

    @app.template_filter("note_md")
    def note_md_filter(value, wiki_index=None, block_index=None):
        """``markdown``, plus the two link syntaxes a NOTE body may use:
        ``[[Note title]]`` against ``wiki_index`` (services/note_links) and
        ``((block-id))`` against ``block_index`` (services/block_refs).

        Both are rewritten into ordinary Markdown links BEFORE rendering, so
        the sanitiser below never has to learn a custom syntax. Comments and
        task notes keep the plain ``markdown`` filter, so a stray "[[" or "((
        " there stays literal."""
        from flask import url_for
        from application.services import block_refs, note_links
        if value and wiki_index is not None:
            value = note_links.expand(
                value, wiki_index,
                new_note_url=lambda t: url_for("notes.new", kind="note", title=t),
            )
        if value and block_index:
            value = block_refs.expand(value, block_index)
        return markdown_filter(value, interactive_tasks=True)

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
        # Both days in the user's wall-clock: an all-day date sits at local
        # midnight, which is the day before in UTC — comparing raw UTC dates
        # would call tomorrow "today".
        delta = (localdate(value) - localdate(datetime.now(timezone.utc))).days
        if delta == 0:
            return "today"
        if delta == 1:
            return "tomorrow"
        if delta == -1:
            return "yesterday"
        if delta < 0:
            return f"{-delta}d overdue"
        return f"in {delta}d"
