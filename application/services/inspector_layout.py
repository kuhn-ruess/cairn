"""Per-user inspector section layout — which sections show and in what
order. The ``title`` section is pinned (always first, always visible);
everything else is orderable and hideable, persisted on ``User.inspector_layout``
as an ordered list of *visible* section keys.

Empty list = the default (all sections visible, canonical order). A key
absent from a non-empty saved list means the user hid that section. Newly
added sections (not present in an older saved list) default to visible and
are appended, so shipping a new section needs no migration.
"""

# (key, label) in canonical default order. ``title`` is intentionally not
# here — it's pinned at the top of the inspector and can't be moved/hidden.
INSPECTOR_SECTIONS = (
    ("note", "Note"),
    ("status_project", "Status & Project"),
    ("tags", "Tags"),
    ("dates", "Defer & Due dates"),
    ("plan", "Plan date"),
    ("priority_estimate", "Priority & Estimate"),
    ("location", "Location"),
    ("repeat", "Repeat"),
    ("review", "Review interval"),
    ("owner", "Owner"),
    ("assigned_to", "Assignee"),
    ("followup", "Follow-up"),
    ("flags", "Flags"),
    ("checklist", "Checklist"),
    ("time", "Time tracking"),
    ("subtasks", "Subtasks"),
    ("related", "Related"),
    ("dependencies", "Dependencies"),
    ("comments", "Comments"),
)

SECTION_KEYS = [k for k, _ in INSPECTOR_SECTIONS]
SECTION_LABELS = dict(INSPECTOR_SECTIONS)


def resolved_layout(user):
    """Return ``[(key, visible)]`` in the user's saved order, for both the
    settings UI and rendering. Sections the saved order doesn't mention
    (e.g. a newly shipped one) are appended in canonical order; visibility
    comes from the ``inspector_hidden`` blocklist so new sections default
    to visible."""
    known = set(SECTION_KEYS)
    saved = [k for k in (getattr(user, "inspector_layout", None) or []) if k in known]
    seen = set(saved)
    order = saved + [k for k in SECTION_KEYS if k not in seen]
    hidden = set(getattr(user, "inspector_hidden", None) or [])
    return [(k, k not in hidden) for k in order]


def visible_order(user):
    """Ordered list of visible section keys, for rendering the inspector."""
    return [k for k, vis in resolved_layout(user) if vis]
