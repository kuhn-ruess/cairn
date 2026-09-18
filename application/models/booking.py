"""The booking page — one per user — and the appointments it produces.

Two halves that live in different places on purpose:

``BookingPage`` is settings, so it is EMBEDDED on ``User``: the public page
reads the whole configuration out of the one user document it already has to
load to resolve the slug. ``BookingHour`` rides along inside it, one row per
weekday the user is bookable (a list, so a day can hold two windows — mornings
and afternoons with a lunch break between them).

``Booking`` is a real Document. It could almost be derived from the calendar
the events are written to, but not quite, and each reason matters:

* the write calendar need not be one of the calendars checked for busy times,
  and then the event we just created would be invisible to the next visitor;
* "at most N per day" has to count something, and counting events in a
  calendar full of other things answers the wrong question;
* two visitors picking the same slot in the same second is a race the database
  settles with a unique index on ``(owner, start)``, not the calendar.
"""
from datetime import datetime, timezone

from bson import ObjectId
from mongoengine import (
    BooleanField,
    DateTimeField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentListField,
    IntField,
    ListField,
    ObjectIdField,
    ReferenceField,
    StringField,
)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday")

# What a fresh page offers before anyone touches it: office hours on weekdays.
DEFAULT_HOURS = [(i, 9 * 60, 17 * 60) for i in range(5)]
DEFAULT_DURATIONS = [30, 60]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BookingHour(EmbeddedDocument):
    """One bookable window on one weekday, in the HOST's timezone.

    Minutes since midnight rather than a string, because every consumer
    (slot generation, the form, the comparison that rejects ``end <= start``)
    wants the number and only the form wants ``09:00``."""

    weekday = IntField(min_value=0, max_value=6, required=True)  # 0 = Monday
    start_minute = IntField(min_value=0, max_value=24 * 60, default=9 * 60)
    end_minute = IntField(min_value=0, max_value=24 * 60, default=17 * 60)

    @property
    def start_hhmm(self) -> str:
        return f"{self.start_minute // 60:02d}:{self.start_minute % 60:02d}"

    @property
    def end_hhmm(self) -> str:
        return f"{self.end_minute // 60:02d}:{self.end_minute % 60:02d}"


class BookingPage(EmbeddedDocument):
    """A user's public booking page, addressed by ``/book/<slug>``."""

    enabled = BooleanField(default=False)
    slug = StringField(max_length=40, default="")
    title = StringField(max_length=120, default="")
    intro = StringField(max_length=2000, default="")

    # Which calendars are read for busy times, and which one the confirmed
    # appointment is written to. Both hold ``ExternalCalendar.pk_ref`` strings.
    busy_calendar_ids = ListField(StringField())
    write_calendar_id = StringField(default="")

    # What a visitor may pick: lengths in minutes, and how far out the page
    # shows days at all.
    durations = ListField(IntField(min_value=5, max_value=480),
                          default=lambda: list(DEFAULT_DURATIONS))
    days_ahead = IntField(min_value=1, max_value=365, default=30)

    # A fresh page already says Mon–Fri, 09:00–17:00: the form opens on
    # something sensible instead of seven unticked rows that answer nobody.
    hours = EmbeddedDocumentListField(
        BookingHour,
        default=lambda: [BookingHour(weekday=d, start_minute=s, end_minute=e)
                         for d, s, e in DEFAULT_HOURS])

    # The three rules that keep a public link from becoming a nuisance.
    min_notice_hours = IntField(min_value=0, max_value=720, default=4)
    buffer_minutes = IntField(min_value=0, max_value=240, default=0)
    max_per_day = IntField(min_value=0, max_value=50, default=0)  # 0 = no cap

    def windows_for(self, weekday: int) -> list:
        return [h for h in (self.hours or []) if h.weekday == weekday
                and h.end_minute > h.start_minute]

    def ordered_durations(self) -> list:
        return sorted({int(d) for d in (self.durations or []) if d})

    def is_live(self) -> bool:
        """Configured enough to answer a visitor at all."""
        return bool(self.enabled and self.slug and self.ordered_durations()
                    and (self.hours or []))


class Booking(Document):
    """One appointment somebody booked through the page."""

    meta = {
        "collection": "bookings",
        "indexes": [
            {"fields": ["owner", "start"], "unique": True},
            "owner",
            ("owner", "start", "end"),
        ],
    }

    owner = ReferenceField("User", required=True)
    start = DateTimeField(required=True)   # UTC
    end = DateTimeField(required=True)     # UTC
    duration_minutes = IntField(required=True)

    # Who booked it. The email is what the confirmation goes to; nothing is
    # verified, which is what the rate limit and the notice window are for.
    name = StringField(max_length=120, default="")
    email = StringField(max_length=255, default="")
    note = StringField(max_length=2000, default="")
    guest_timezone = StringField(max_length=64, default="")

    # Where the event landed, so it stays traceable from the Cairn side.
    calendar_id = StringField(default="")   # ExternalCalendar.pk_ref
    event_id = StringField(default="")
    event_error = StringField(default="")

    created_at = DateTimeField(default=_utcnow)
    pk_ref = ObjectIdField(default=lambda: ObjectId())

    def __str__(self) -> str:
        return f"{self.name} {self.start:%Y-%m-%d %H:%M}"
