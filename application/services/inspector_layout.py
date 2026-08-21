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
    ("status_project", "Status, Project, Tags & Flags"),
    ("dates", "Defer, Due & Plan"),
    ("priority_estimate", "Priority & Estimate"),
    ("location", "Location"),
    ("repeat", "Repeat"),
    ("review", "Review & Follow-up"),
    ("assigned_to", "Owner & Assignee"),
    ("checklist", "Checklist"),
    ("time", "Time tracking"),
    ("subtasks", "Subtasks"),
    ("related", "Related & Dependencies"),
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


def hidden_sections(user) -> set:
    """Section keys the user hid — i.e. the ones the inspector does NOT render,
    so their fields never reach the POST. ``_apply_form`` skips these instead
    of writing the form's empty defaults over the task (an absent ``project``
    would otherwise clear the parent, an absent ``assigned_to`` would unassign,
    an absent checkbox would unflag)."""
    return set(SECTION_KEYS) - set(visible_order(user))


def reordered(user, shown: list) -> list:
    """The saved order with ``shown`` rearranged into the order given.

    Dragging happens in the inspector, which renders only what is visible —
    and on the detail page not even all of that (comments and subtasks live in
    their own column there). So the drop reports a *subset*, and the sections
    it doesn't mention must keep their place rather than be shuffled to the end
    where they'd resurface in a random order the next time they're shown.

    Walk the saved order; every slot that holds one of the reported sections
    takes the next one from the reported sequence, every other slot keeps what
    it had. The subset is rearranged exactly where it already lived."""
    known = set(SECTION_KEYS)
    order = [k for k, _ in resolved_layout(user)]
    moving = [k for k in dict.fromkeys(shown) if k in known and k in order]
    if not moving:
        return order
    queue = iter(moving)
    in_play = set(moving)
    return [next(queue) if k in in_play else k for k in order]
