"""In-app notifications shown in the navbar bell.

Embedded on ``User`` (not a top-level collection) so the unread badge that
renders on *every* page costs nothing extra: the user document is already
loaded by Flask-Login, so the count is in hand. The list is capped to the
most recent ``NOTIFICATION_CAP`` entries on write (see ``User.add_notification``).

Notifications are fired from explicit, user-facing choke points — assignment
(``Item.assign_to``), completion (``Item.complete``) and team invites — never
from ``Item.save`` or background sync, so imports/cron never spam the bell.
"""
from datetime import datetime, timezone

from bson import ObjectId
from mongoengine import (
    BooleanField,
    DateTimeField,
    EmbeddedDocument,
    ObjectIdField,
    StringField,
)


def _utcnow():
    return datetime.now(timezone.utc)


class Notification(EmbeddedDocument):
    # Stable id so the mark-read endpoint can target a single entry.
    pk_ref = ObjectIdField(default=lambda: ObjectId())
    kind = StringField()              # assigned | unassigned | completed | mention | team_invite
    text = StringField()              # human-readable message
    link = StringField()              # where clicking the entry navigates
    icon = StringField(default="bi-bell")   # Bootstrap-icon class
    actor = StringField()             # display name of who triggered it
    created_at = DateTimeField(default=_utcnow)
    read = BooleanField(default=False)
    # The item the entry is about, when there is one (assignment, mention,
    # completion — not a team invite). ``link`` already carries the path, but
    # an API client should not have to parse a URL to know what it was handed.
    item_id = ObjectIdField()
