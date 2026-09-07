"""Tags — embedded in User, keyed by name within that user.

Stored as ``EmbeddedDocumentListField`` on the User document so a user's
whole tag set is loaded together (no extra query). Tasks store tag
**names** (strings) rather than references — names are the canonical
identifier and survive sharing across users (an assignee inherits the
tag names on the assigned task; their own user.tags get a styled entry
auto-added if it's not there yet).

Tag groups: ``parent`` references another tag of the same user by name.
``exclusive_children`` marks a parent whose children are mutually
exclusive — only one child tag from the group can be applied to a task
at a time.
"""
from datetime import datetime, timezone

from mongoengine import (
    BooleanField,
    DateTimeField,
    EmbeddedDocument,
    StringField,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tag(EmbeddedDocument):
    name = StringField(required=True, max_length=64)
    color = StringField(default="#6c757d", max_length=16)
    is_context = BooleanField(default=True)
    on_hold = BooleanField(default=False)

    # Tag hierarchy
    parent = StringField()                # parent tag's name, or empty
    exclusive_children = BooleanField(default=False)

    # Optional auto-status mapping. When a task carries this tag, its
    # ``status`` is forced to this value on save / import. Lets the
    # user say "Warte auf Kunde → waiting" once, then any task that
    # ever gets that tag flips automatically, whether the tag was set
    # by the inspector, by a Zammad sync, or by a CSV reimport.
    maps_to_status = StringField()

    # Optional auto-assignment. When a task picks up this tag, it is handed
    # to this person — the ``User`` pk of somebody the tag's owner may assign
    # to (a member of one of their teams). "Buchhaltung → Anna" said once,
    # so tagging is enough and nobody has to remember the second step.
    # Resolved through ``permissions.resolve_assignable_user`` on every use,
    # so a person who left the team simply stops the rule from firing.
    assigns_to = StringField()

    created_at = DateTimeField(default=_utcnow)
    external_id = StringField()  # opaque id from the importing source

    def __str__(self) -> str:
        return f"@{self.name}" if self.is_context else self.name
