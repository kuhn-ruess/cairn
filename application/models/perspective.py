"""Custom perspectives — user-defined task views with filter + grouping.

Modelled like OmniFocus:

* a perspective has a name, icon, sidebar position and is owned by a user
* its filter is a tree of clauses with AND/OR groups (nested up to a
  reasonable depth)
* grouping + sort describe how matching tasks are rendered

Stored as ``EmbeddedDocumentListField`` on ``User`` so everything lives
with the user document (no extra collection needed).
"""
import secrets

from mongoengine import (
    BooleanField,
    EmbeddedDocument,
    EmbeddedDocumentListField,
    StringField,
)


# Filter --------------------------------------------------------------

FILTER_FIELDS = (
    # (key, label, value type) — used by the UI to render the right input.
    ("status",        "Status",        "status"),
    ("flagged",       "Flagged",       "bool"),
    ("priority",      "Priority",      "priority"),
    ("tag",           "Tag",           "tag"),
    ("project",       "Project",       "project"),
    ("kind",          "Item kind",     "kind"),
    ("has_due",       "Has due date",  "bool"),
    ("due",           "Due date",      "date"),
    ("has_plan",      "Has plan date", "bool"),
    ("plan",          "Plan date",     "date"),
    ("has_defer",     "Has defer date","bool"),
    ("defer",         "Defer date",    "date"),
    ("available",     "Available",     "bool"),
    ("blocked",       "Blocked",       "bool"),
    ("repeating",     "Repeats",       "bool"),
    ("assigned",      "Assigned to me","bool"),
    ("title",         "Title",         "text"),
    ("note",          "Note text",     "text"),
    ("created",       "Created date",  "date"),
    ("updated",       "Updated date",  "date"),
)

OPS_BY_TYPE = {
    "status":   ["is", "is_not", "in"],
    "bool":     ["is_true", "is_false"],
    "priority": ["is", "lte", "gte"],
    "tag":      ["has", "not_has"],
    "project":  ["is", "is_descendant_of"],
    "kind":     ["is", "is_not"],
    "date":     ["before", "after", "on", "within_days", "overdue", "in_past"],
    "text":     ["contains", "equals"],
}


class FilterClause(EmbeddedDocument):
    """One node of the filter tree.

    ``kind == "leaf"`` → ``field`` + ``op`` + ``value`` describe a condition.
    ``kind == "group"`` → ``group_op`` (and/or) over ``children``.
    """
    kind = StringField(choices=["leaf", "group"], default="leaf")
    # leaf
    field = StringField()
    op = StringField()
    value = StringField()       # JSON-encoded primitive or list
    # group
    group_op = StringField(choices=["and", "or"], default="and")
    children = EmbeddedDocumentListField("FilterClause")


# Perspective ---------------------------------------------------------

GROUPING_MODES = (
    "none", "status", "priority", "project", "subproject", "tag", "tag_specific",
    "due", "plan", "flagged",
)

SORT_FIELDS = (
    "default", "title", "due_asc", "due_desc", "priority", "created", "updated",
)


class CustomPerspective(EmbeddedDocument):
    pid = StringField(default=lambda: secrets.token_urlsafe(8))  # stable user-scoped id
    name = StringField(required=True, max_length=80)
    icon = StringField(default="bi-star")
    color = StringField(default="#fabd2f", max_length=16)
    in_sidebar = BooleanField(default=True)

    # Top-level filter: an AND/OR of clauses. Use a single root group with
    # nested groups for AND/OR mixing.
    root_op = StringField(choices=["and", "or"], default="and")
    clauses = EmbeddedDocumentListField(FilterClause)

    grouping = StringField(choices=GROUPING_MODES, default="none")
    grouping_tag = StringField()        # tag pk for "tag_specific" grouping
    sort = StringField(choices=SORT_FIELDS, default="default")
    include_completed = BooleanField(default=False)
    show_blocked = BooleanField(default=True)
