"""The left sidebar, sorted into groups the user can fold away.

The sidebar had grown into one long column of thirty-odd links. It is now a
handful of named groups, each with a header button that collapses it; the
choice is stored per user (``User.sidebar_groups``) so it is the same in every
browser. Which entry sits in which group is decided here, in one place, so the
template only loops and the grouping can be tested without a browser.

Nothing was removed: every link the old sidebar had is in exactly one group.
Built-in perspectives keep the user's own selection and order (Settings >
Sidebar perspectives); only the group they appear under is fixed by key.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from flask import url_for


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    collapsed_by_default: bool = False


# Display order. "plan" and "more" start folded: they hold what is visited
# weekly or rarely, and that is most of what made the column long.
GROUPS: tuple[Group, ...] = (
    Group("favorites", "Favorites"),
    Group("focus", "Focus"),
    Group("followup", "Follow-up"),
    Group("custom", "My perspectives"),
    Group("organize", "Organize"),
    Group("work", "Time & customers"),
    Group("plan", "Plan & review", collapsed_by_default=True),
    Group("more", "More", collapsed_by_default=True),
)
GROUP_KEYS = frozenset(g.key for g in GROUPS)

# Built-in perspective key -> group. A key not listed (a perspective added
# later) lands in Focus rather than disappearing.
PERSPECTIVE_GROUP = {
    "home": "focus", "inbox": "focus", "today": "focus", "next": "focus",
    "scheduled": "focus", "flagged": "focus", "forecast": "focus",
    "waiting": "followup", "waiting_reply": "followup", "followup": "followup",
    "review": "followup", "assigned": "followup", "delegated": "followup",
    "someday": "more", "completed": "more",
}


@dataclass
class Entry:
    label: str
    icon: str
    url: str
    fav: str | None = None        # favourite token, None = no star
    count: int = 0
    color: str | None = None
    active: bool = False


@dataclass
class SidebarGroup:
    key: str
    label: str
    collapsed: bool
    entries: list[Entry] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Sum of the badges inside, shown on a folded header so a due
        review or a full inbox is not hidden by folding its group away."""
        return sum(e.count for e in self.entries)


def is_collapsed(user, key: str) -> bool:
    stored = (getattr(user, "sidebar_groups", None) or {}).get(key)
    if stored is not None:
        return bool(stored)
    group = next((g for g in GROUPS if g.key == key), None)
    return bool(group and group.collapsed_by_default)


def set_collapsed(user, key: str, collapsed: bool) -> None:
    """Store one group's state with an atomic ``$set`` on that key, so two
    tabs folding different groups don't overwrite each other."""
    if key not in GROUP_KEYS:
        raise ValueError(key)
    from application.models.user import User
    User.objects(pk=user.pk).update_one(
        **{f"set__sidebar_groups__{key}": bool(collapsed)})
    groups = dict(getattr(user, "sidebar_groups", None) or {})
    groups[key] = bool(collapsed)
    user.sidebar_groups = groups


def _org(slug: str, label: str, icon: str, endpoint: str, count: int = 0) -> Entry:
    return Entry(label=label, icon=icon, url=url_for(endpoint),
                 fav=f"org:{slug}", count=count or 0)


def build(user, *, perspectives, custom_perspectives, favorites, counts,
          path: str = "") -> list[SidebarGroup]:
    """The sidebar as groups, empty groups dropped.

    ``perspectives`` / ``custom_perspectives`` / ``favorites`` / ``counts``
    are what the context processor already loaded, so this costs no query.
    ``path`` is the request path; the entry it belongs to is marked active,
    and a folded group holding it is shown open on that page (the stored
    choice is left alone).
    """
    counts = counts or {}
    by_key: dict[str, list[Entry]] = {g.key: [] for g in GROUPS}

    for f in favorites or []:
        by_key["favorites"].append(Entry(
            label=f["label"], icon=f["icon"], url=f["url"],
            fav=f["token"], color=f.get("color")))

    for p in perspectives or []:
        by_key[PERSPECTIVE_GROUP.get(p.key, "focus")].append(Entry(
            label=p.label, icon=p.icon, url=url_for(p.endpoint),
            fav=f"persp:{p.key}", count=counts.get(p.key) or 0))

    for p in custom_perspectives or []:
        by_key["custom"].append(Entry(
            label=p.name, icon=p.icon, color=p.color,
            url=url_for("custom_perspectives.view", pid=p.pid),
            fav=f"cp:{p.pid}"))

    org = by_key["organize"]
    org.append(_org("projects", "Projects", "bi-collection", "projects.index"))
    org.append(_org("notes", "Notes", "bi-journal-text", "notes.index"))
    org.append(_org("scratchpad", "Scratchpad", "bi-stars", "scratchpad.index"))
    org.append(_org("tree", "Tree", "bi-diagram-3", "tree.index"))
    org.append(_org("tags", "Tags / Contexts", "bi-tag", "tags.index"))

    work = by_key["work"]
    in_team = bool(getattr(user, "team", None) or getattr(user, "is_team_admin", False))
    if in_team:
        work.append(_org("timetracking", "Time tracking", "bi-stopwatch", "timetracking.day"))
        # One customer list for the whole app, the same page the time module manages.
        work.append(_org("customers", "Customers", "bi-person-vcard", "timetracking.index"))
    booking = getattr(user, "booking", None)
    if booking and booking.enabled:
        work.append(_org("bookings", "Bookings", "bi-calendar-check",
                         "bookings.index", counts.get("bookings")))

    plan = by_key["plan"]
    plan.append(_org("review", "Review", "bi-arrow-clockwise", "review.index",
                     counts.get("review")))
    if getattr(user, "is_premium", False):
        plan.append(_org("twelve_week", "12 Week Year", "bi-bullseye", "twelve_week.index"))
        plan.append(_org("templates", "Templates", "bi-files", "templates.index"))
    plan.append(_org("dependency_graph", "Dependency graph", "bi-diagram-3",
                     "tasks.dependency_graph"))

    more = by_key["more"]
    more.append(_org("trash", "Trash", "bi-trash", "trash.index", counts.get("trash")))
    more.append(_org("perspectives", "Perspectives", "bi-binoculars",
                     "custom_perspectives.index"))
    more.append(Entry(label="Customise sidebar", icon="bi-sliders",
                      url=url_for("settings.perspectives")))

    groups = [SidebarGroup(key=g.key, label=g.label,
                           collapsed=is_collapsed(user, g.key),
                           entries=by_key[g.key])
              for g in GROUPS if by_key[g.key]]
    _mark_active(groups, path)
    return groups


def _mark_active(groups: list[SidebarGroup], path: str) -> None:
    """Mark the entries whose URL is the longest one the path starts with.

    Longest wins because some URLs nest (``/time/`` and ``/time/day``); an
    entry for ``/`` only matches the home page itself. A favourite and the
    group row it copies point at the same URL and light up together.
    """
    if not path:
        return
    best = ""
    for g in groups:
        for e in g.entries:
            u = e.url.split("?", 1)[0]
            if u == path or (u != "/" and path.startswith(u.rstrip("/") + "/")):
                if len(u) > len(best):
                    best = u
    if not best:
        return
    for g in groups:
        for e in g.entries:
            if e.url.split("?", 1)[0] == best:
                e.active = True
                g.collapsed = False
