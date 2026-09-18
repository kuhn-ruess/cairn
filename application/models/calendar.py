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
    "caldav",  # CalDAV collection (Nextcloud, …), read/write via Basic auth
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

    # CalDAV (caldav) — the calendar COLLECTION URL (e.g. Nextcloud
    # ``…/remote.php/dav/calendars/<user>/<cal>/``) plus Basic-auth creds. The
    # password is stored encrypted per-user (``encrypt_for_user``), exactly like
    # the OAuth tokens above — never in plaintext.
    caldav_url = StringField()
    caldav_username = StringField(max_length=255)
    caldav_password = StringField()
    # Where this calendar was discovered from (the calendar home, or the
    # collection itself when that is what was pasted). Asking it again is what
    # "Refresh from CalDAV" does — a collection URL alone can't be walked back
    # up to its home.
    caldav_home = StringField()

    last_synced_at = DateTimeField()
    last_error = StringField()
    created_at = DateTimeField(default=_utcnow)

    @property
    def caldav_host(self) -> str:
        """``cloud.example.com`` out of the collection URL. With two CalDAV
        accounts connected, the username alone stops telling them apart."""
        if not self.caldav_url:
            return ""
        from urllib.parse import urlsplit
        return urlsplit(self.caldav_url).hostname or ""

    @property
    def account_label(self) -> str:
        """Who this calendar comes from, as one short string."""
        if self.provider == "caldav":
            host = self.caldav_host
            user = self.caldav_username or ""
            return f"{user}@{host}" if user and host else (user or host or "connected")
        return self.account_email or "connected"

    @property
    def pk(self):
        """Mongoengine convenience — embedded docs don't get a real
        ``pk``, so views that called ``cal.pk`` keep working."""
        return self.pk_ref

    def __str__(self) -> str:
        return self.name
