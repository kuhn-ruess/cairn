"""External calendars — embedded on the owning user.

Each user can connect Google + subscribe to any number of iCal URLs.
Living embedded on ``User`` means a single document fetch covers the
user plus all their calendars; auth + OAuth tokens never need a
separate round trip.
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


CALENDAR_PROVIDERS = (
    "google",  # OAuth, read/write capable
    "ical",    # public/secret iCal URL, read-only
)


class ExternalCalendar(EmbeddedDocument):
    """One connected calendar belonging to a user.

    Lookups go through the parent ``User``; the ``pk_ref`` is what URLs
    and the user-level ``calendar_write_target`` reference."""

    pk_ref = ObjectIdField(default=lambda: ObjectId())

    provider = StringField(choices=CALENDAR_PROVIDERS, default="google")

    # Display
    name = StringField(required=True, max_length=128)
    color = StringField(default="#458588", max_length=16)
    show_in_today = BooleanField(default=True)
    can_write = BooleanField(default=False)

    # OAuth account binding (google)
    account_email = StringField(max_length=255)
    calendar_id = StringField()  # provider's calendar identifier
    oauth_refresh_token = StringField()
    oauth_access_token = StringField()
    oauth_token_expires_at = DateTimeField()
    oauth_scope = StringField()

    # iCal subscription (ical) — secret/public ICS URL
    ical_url = StringField()

    last_synced_at = DateTimeField()
    last_error = StringField()
    created_at = DateTimeField(default=_utcnow)

    @property
    def pk(self):
        """Mongoengine convenience — embedded docs don't get a real
        ``pk``, so views that called ``cal.pk`` keep working."""
        return self.pk_ref

    def __str__(self) -> str:
        return self.name
