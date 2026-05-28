"""Canonical list of sidebar perspectives + per-user customisation.

Each perspective is identified by a stable string key (the same key the
user stores in ``User.sidebar_perspectives``). The display attributes
(label, icon, URL endpoint) live here so the sidebar template and the
settings UI render from the same source of truth.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Perspective:
    key: str
    label: str
    icon: str
    endpoint: str


# Every perspective we expose — the picker in Settings lists this set.
# Forecast lives here so old bookmarks + the picker can find it, but
# isn't in the default sidebar (Today's ?days=N covers the same need).
ALL_PERSPECTIVES: tuple[Perspective, ...] = (
    Perspective("home",      "Home",            "bi-house",              "dashboard.home"),
    Perspective("inbox",     "Inbox",           "bi-inbox",              "tasks.inbox"),
    Perspective("today",     "Today",           "bi-calendar-day",       "tasks.today"),
    Perspective("next",      "Next",            "bi-arrow-right-circle", "tasks.next_actions"),
    Perspective("scheduled", "Scheduled",       "bi-hourglass-split",    "tasks.scheduled"),
    Perspective("flagged",   "Flagged",         "bi-flag",               "tasks.flagged"),
    Perspective("waiting",   "Waiting",         "bi-hourglass-split",    "tasks.waiting"),
    Perspective("assigned",  "Assigned to me",  "bi-person-check",       "tasks.assigned_to_me"),
    Perspective("someday",   "Someday",         "bi-cloud",              "tasks.someday"),
    Perspective("completed", "Completed",       "bi-check2-all",         "tasks.completed"),
    Perspective("forecast",  "Forecast (14d)",  "bi-calendar-week",      "tasks.forecast"),
)

# Default sidebar order — what new users see, and what
# ``./cairn perspectives-reset`` falls back to. Excludes Forecast (see
# the comment above ALL_PERSPECTIVES).
DEFAULT_SIDEBAR_KEYS: tuple[str, ...] = (
    "home", "inbox", "today", "next", "scheduled",
    "flagged", "waiting", "assigned", "someday", "completed",
)

KEYS = tuple(p.key for p in ALL_PERSPECTIVES)
_BY_KEY = {p.key: p for p in ALL_PERSPECTIVES}


def for_user(user) -> list[Perspective]:
    """Return the perspectives the user wants in the sidebar, in order.

    Empty ``sidebar_perspectives`` falls back to :data:`DEFAULT_SIDEBAR_KEYS`
    (everything except Forecast — see the constant's docstring).
    Unknown keys are skipped silently.
    """
    custom = list(user.sidebar_perspectives or [])
    if not custom:
        return [_BY_KEY[k] for k in DEFAULT_SIDEBAR_KEYS if k in _BY_KEY]
    return [_BY_KEY[k] for k in custom if k in _BY_KEY]


def visible_keys(user) -> list[str]:
    return [p.key for p in for_user(user)]
