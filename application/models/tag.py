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

    created_at = DateTimeField(default=_utcnow)
    external_id = StringField()  # opaque id from the importing source

    def __str__(self) -> str:
        return f"@{self.name}" if self.is_context else self.name
