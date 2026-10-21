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
    BinaryField,
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
    # Listed on the public directory at ``/book``. Separate from ``enabled``
    # because "anyone with my link may book" and "put my face on a page that
    # lists everybody" are two different decisions.
    listed = BooleanField(default=True)
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

    # How the appointment is met. ``none`` says nothing, ``link`` hands out
    # the same room every time (``meeting_link`` — a Jitsi, a Zoom, a standing
    # Teams room), ``teams`` asks Microsoft for a fresh Teams meeting per
    # booking (services/msgraph.py). The mode is on the PAGE and the resulting
    # URL on the BOOKING: switching the mode later must not rewrite where
    # somebody was already told to turn up.
    meeting_mode = StringField(choices=("none", "link", "teams"), default="none")
    meeting_link = StringField(max_length=500, default="")

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


class BookingPhoto(Document):
    """The portrait shown on a booking page and in the ``/book`` directory.

    Its own collection, NOT a field on ``User``: Flask-Login loads the user
    document on every single request, and a photo living there would be read
    on every page of the app to be used on two. Stored already resized (see
    ``views.settings._store_booking_photo``), so it is small and uniform."""

    meta = {"collection": "booking_photos", "indexes": [
        {"fields": ["owner"], "unique": True}]}

    owner = ReferenceField("User", required=True)
    data = BinaryField(required=True)
    content_type = StringField(default="image/webp")
    updated_at = DateTimeField(default=_utcnow)


class Booking(Document):
    """One appointment somebody booked through the page."""

    meta = {
        "collection": "bookings",
        "indexes": [
            {"fields": ["owner", "start"], "unique": True},
            "owner",
            ("owner", "start", "end"),
            # What the flood ceiling counts (services/booking_guard.py):
            # how much this page has taken in the last hour and day.
            ("owner", "created_at"),
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

    # Where the two of you meet. Written once, when the booking is taken, and
    # then repeated in the confirmation mails, in the .ics and on the host's
    # calendar entry. ``meeting_error`` is the host's news, like
    # ``event_error``: the visitor has the slot either way.
    meeting_url = StringField(max_length=500, default="")
    meeting_provider = StringField(default="")   # "link" | "teams"
    meeting_error = StringField(default="")

    # Whose appointment this is. Filled when the booking is taken, from the
    # mail DOMAIN the guest booked with (services/customers.py) — a call from
    # ``anna@acme.de`` is Acme's without anybody saying so. Left empty when no
    # customer claims the domain, when two do, or when it is a free-mail
    # address; the list is where it is then picked by hand.
    #
    # Deliberately NOT a billing project: a meeting that was never held is
    # never billed, so nothing is booked until somebody says so from the list.
    customer = ReferenceField("Customer")

    # What the meeting actually took, when that differed from what was booked.
    # A half-hour call that ran fifty minutes is billed as fifty, and this is
    # where the correction is kept, so the list shows it afterwards instead of
    # only the invoice knowing. ``None`` = it took as long as it was booked for.
    actual_minutes = IntField(min_value=1)

    # Where the event landed, so it stays traceable from the Cairn side.
    calendar_id = StringField(default="")   # ExternalCalendar.pk_ref
    event_id = StringField(default="")
    event_error = StringField(default="")
    # The host's address as the write calendar knows it, which is what the
    # event and every mail about it name as ORGANIZER: Apple Calendar applies
    # a guest's acceptance only to a meeting organised by one of the
    # account's own addresses, and that need not be the Cairn login address.
    organizer_email = StringField(max_length=255, default="")

    created_at = DateTimeField(default=_utcnow)
    pk_ref = ObjectIdField(default=lambda: ObjectId())

    @property
    def billed_minutes(self) -> int:
        """How long this meeting is to be billed for — corrected if corrected."""
        return int(self.actual_minutes or self.duration_minutes or 0)

    def __str__(self) -> str:
        return f"{self.name} {self.start:%Y-%m-%d %H:%M}"
