"""Team time-tracking / billing module — a NoSQL-optimised replacement for mite.

Design constraints (decided with the user):

* **Team-only.** The whole module is gated behind team membership; solo
  free/premium users never see it.
* **Never delete.** Logged time is an append-only record. An entry is never
  removed — it carries a ``status ∈ {active, cancelled}`` and is soft-cancelled
  in place (with who/when). ``locked`` entries (billed) are frozen entirely.
* **Few documents.** Entries are *not* one-doc-each. They are bucketed: one
  :class:`TimeBucket` per ``(team, customer, day)`` holds every entry logged for
  that customer on that day — by *everyone* — appended via an atomic ``$push``.
  A document that would grow unwieldy is not allowed to: once a bucket reaches
  ``MAX_ENTRIES_PER_BUCKET`` the next write opens ``part=2`` for the same day
  (then 3, …), so a document can never approach MongoDB's 16 MB ceiling. Every
  read path (day sheet, report, share link, backlink lookup) queries by day
  range and merges the parts, so the split is invisible above the service
  layer.

Hierarchy of the billing entities (mite-shaped):

    Team → Customer → TimeTrackingProject → (entries live in the monthly bucket)

The GTD tree (``Item``) is linked *in*, not merged: a Cairn folder/project sets
``Item.time_project``; tasks inherit it by climbing parents. When time is logged
the source task/project is captured only as a soft backlink (its id as a string)
plus a name snapshot, so a later deletion of that task never breaks the entry —
see ``TimeEntry.source_item_id`` / ``source_label``.
"""
import secrets
from datetime import datetime, timezone

from mongoengine import (
    BooleanField,
    DateTimeField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentListField,
    IntField,
    ListField,
    ReferenceField,
    StringField,
)

from application.models.timer import RunningTimer  # noqa: F401  (re-export)
from application.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Customer(Document):
    """A billing customer. Belongs to exactly one team. Groups projects."""

    meta = {"collection": "tt_customers", "indexes": ["team"]}

    team = ReferenceField("Team", required=True)
    name = StringField(required=True, max_length=200)
    note = StringField(default="")
    archived = BooleanField(default=False)
    # Identity in the system this was imported from (``mite:<id>``), so a
    # repeated import updates instead of duplicating. Empty for customers
    # created in Cairn.
    external_id = StringField()

    # Billing details — what an invoice needs, kept with the customer it bills.
    # Plain strings, deliberately: this is data to read off when invoicing (or
    # to hand to whatever writes the invoice), not something Cairn validates or
    # calculates with. ``address`` is multi-line and free-form, because postal
    # formats are not ours to police.
    legal_name = StringField(max_length=200)      # if it differs from ``name``
    address = StringField(default="")
    vat_id = StringField(max_length=64)           # USt-IdNr. / VAT ID
    tax_number = StringField(max_length=64)       # Steuernummer
    invoice_email = StringField(max_length=200)
    contact_name = StringField(max_length=200)
    phone = StringField(max_length=64)
    # How this customer is billed: payment terms in days, a customer number in
    # your own accounting, and anything else the person writing the invoice
    # needs to know.
    payment_terms_days = IntField(min_value=0)
    customer_number = StringField(max_length=64)
    billing_note = StringField(default="")

    created_at = DateTimeField(default=_utcnow)

    def __str__(self) -> str:
        return self.name


class TimeProjectField(EmbeddedDocument):
    """One extra thing a project wants recorded with every booking.

    Defined per billing project by whoever manages the team, and asked for
    wherever time is captured. Two kinds, which is all the shapes real billing
    notes take: free ``text`` (with an optional default that is pre-filled), and
    a ``select`` whose choices the manager fixes — so "Ticket-Nr." stays free
    while "Abrechnungsart" can't be spelled three different ways.

    ``key`` is a slug derived from the label once and then kept: renaming a
    field must not orphan the values already booked under it."""

    key = StringField(required=True, max_length=60)
    label = StringField(required=True, max_length=100)
    field_type = StringField(choices=("text", "select"), default="text")
    default = StringField(default="", max_length=200)
    options = ListField(StringField(max_length=200))

    def __str__(self) -> str:
        return self.label


class TimeTrackingProject(Document):
    """The billing unit. Belongs to one customer; carries the hourly rate.

    The customer-facing read-only link (hours only, no money) hangs off
    ``share_token`` here — same pattern as ``Item.share_token`` / ``/p``."""

    meta = {
        "collection": "tt_projects",
        "indexes": ["team", "customer", "share_token"],
    }

    team = ReferenceField("Team", required=True)
    customer = ReferenceField("Customer", required=True)
    name = StringField(required=True, max_length=200)
    note = StringField(default="")
    # Hourly rate in cents. Frozen onto each entry at capture time, so changing
    # it here only affects future entries. ``None`` = no rate (hours only).
    hourly_rate_cents = IntField(min_value=0)

    # Optional budget (mite-shaped). ``budget_type`` encodes both the unit and
    # the recurrence: ``minutes`` / ``cents`` are a total cap; the ``*_per_month``
    # variants are recurring retainers that reset each calendar month. ``None``
    # type = no budget. ``budget_value`` is in minutes or cents per the type.
    budget_value = IntField(min_value=0)
    budget_type = StringField(choices=("minutes", "cents",
                                       "minutes_per_month", "cents_per_month"))

    archived = BooleanField(default=False)

    # Identity in the system this was imported from (``mite:<id>``) — see
    # Customer.external_id.
    external_id = StringField()

    # Extra fields asked for when booking time on this project (see
    # TimeProjectField). Empty for a project that just wants hours and a note.
    custom_fields = EmbeddedDocumentListField(TimeProjectField)

    # How time is normally entered here: a duration ("1h30m") or the clock
    # ("09:00–10:30"). Some customers want the times of day on the sheet, most
    # only want the hours — pinning it per project means nobody has to remember
    # which, the capture form just asks the right way. A *default for the form*,
    # not a constraint: the stopwatch and the one-click start still book
    # whatever they measured.
    time_mode = StringField(choices=("duration", "clock"), default="duration")

    # Show the task an entry was logged from as part of its description on the
    # customer's sheet and export. The title is already on every entry
    # (``TimeEntry.source_label``); this only decides whether it is *rendered*
    # into the description — nothing is ever written into the note, so turning
    # it off takes the reference away again and an edited note stays the user's
    # own words.
    show_source = BooleanField(default=False)

    # Incoming external time (currently: the Zammad import) for this customer
    # books here. Exactly one project per customer should carry it — the
    # importer takes the first it finds, and setting it on a project clears it
    # on the customer's other ones (services.timetracking.set_intake_project).
    # An auto-created customer gets it on the project created with it; pointing
    # it at a project you already had is how you take the training wheels off.
    intake = BooleanField(default=False)

    # Customer share link (read-only, hours only).
    share_enabled = BooleanField(default=False)
    share_token = StringField(unique=True, sparse=True)

    created_at = DateTimeField(default=_utcnow)

    def regenerate_share_token(self) -> str:
        self.share_token = secrets.token_urlsafe(24)
        return self.share_token

    def __str__(self) -> str:
        return self.name


class TimeEntryValue(EmbeddedDocument):
    """A custom field's value on one booking — key, and the label *as it read
    when the time was booked*.

    The label is snapshotted with the value for the same reason every other
    field here is: a report, a CSV or the customer's share link must stay
    readable after the project's field was renamed or removed."""

    key = StringField(required=True, max_length=60)
    label = StringField(default="", max_length=100)
    value = StringField(default="", max_length=500)


class TimeEntry(EmbeddedDocument):
    """One logged chunk of time, embedded in a customer's monthly bucket.

    Append-only: never popped. ``status`` flips to ``cancelled`` for soft
    delete; ``locked`` marks it billed and freezes it. ``entry_id`` is a stable
    opaque id used to target the entry for positional updates (so we never rely
    on a fragile list index)."""

    entry_id = StringField(default=lambda: secrets.token_urlsafe(12))
    # Project as a soft id + name snapshot (not a hard ReferenceField): keeps the
    # report deref-free (group/filter by the string id, show the snapshot name)
    # and survives a project being renamed or archived. The customer is the
    # bucket's customer; the frozen rate/amount are below.
    project_id = StringField(required=True)
    project_name = StringField(default="")

    # Who logged it — soft id + name snapshot (deref-free in reports, survives
    # the user being removed from the team).
    user_id = StringField()
    user_name = StringField(default="")

    minutes = IntField(required=True, min_value=1)
    spent_on = DateTimeField(default=_utcnow)  # date the work happened, tz-aware
    # Optionally *when* on that day the work happened, as wall-clock "HH:MM" in
    # the logger's own timezone. Booking by clock time is an alternative to
    # typing a duration (``minutes`` is then derived and stays authoritative);
    # the stopwatch fills both in by itself. Empty when only a duration was
    # given — which stays the normal case.
    start_time = StringField(max_length=5)
    end_time = StringField(max_length=5)
    note = StringField(default="")
    # mite calls this a "Leistung": what kind of work this was. Cairn has no
    # service entity — the label is carried along so imported history keeps its
    # meaning, and shown next to the note wherever entries are listed.
    service = StringField(default="")

    # Soft backlink to the Cairn item the time was logged from. Stored as a
    # plain string id (NOT a ReferenceField) on purpose: dereferencing must
    # never crash if the task/project was deleted. ``source_label`` keeps the
    # name so the entry stays readable even when the item is gone.
    source_item_id = StringField()
    source_kind = StringField()  # task / project, for the backlink url
    source_label = StringField(default="")

    # Identity of the record this entry was imported from, e.g.
    # ``zammad:<time_accounting_id>``. Set only for imported time; it makes a
    # re-sync idempotent (an entry already carrying the id is never appended a
    # second time) and marks the entry as externally owned.
    external_id = StringField()
    # How the *source* of the work is shown and reached: the ticket number the
    # user knows it by, and a deep link to it. Snapshotted like everything else
    # here, so a report never has to dereference the task (which may be gone) to
    # print "#4711" as a link. Never shown on the customer share link, which
    # only ever gets the number as text.
    external_ref = StringField()
    external_url = StringField()
    # Who booked it *in the source system*, e.g. ``zammad:user:42``. Kept next
    # to the Cairn attribution (``user_id``/``user_name``) rather than instead
    # of it: while an agent has no Cairn account the entry still carries the
    # agent's identity, so assigning that agent later can find and re-attribute
    # exactly their entries — by id, not by a name that may repeat.
    external_user = StringField()

    # Values for the project's custom fields, snapshotted with their labels
    # (see TimeEntryValue).
    custom = EmbeddedDocumentListField(TimeEntryValue)

    # Money frozen at capture time (internal only — never shown on the customer
    # share link). ``amount_cents`` is null when non-billable or no rate.
    hourly_rate_cents = IntField(min_value=0)
    amount_cents = IntField(min_value=0)
    billable = BooleanField(default=True)

    # Billing freeze: once locked, the entry cannot be edited or cancelled.
    locked = BooleanField(default=False)
    billed_at = DateTimeField()
    billed_by = ReferenceField(User)

    # Soft delete.
    status = StringField(choices=("active", "cancelled"), default="active")
    cancelled_at = DateTimeField()
    cancelled_by = ReferenceField(User)

    created_at = DateTimeField(default=_utcnow)

    @property
    def is_active(self) -> bool:
        return self.status == "active"


# How many entries one bucket document may hold before the next write starts a
# new part for the same day. A time entry is a few hundred bytes, so 500 keeps a
# document at ~200 KB — two orders of magnitude below MongoDB's 16 MB document
# limit, with room for very long notes.
MAX_ENTRIES_PER_BUCKET = 500


class TimeBucket(Document):
    """One document per ``(team, customer, day, part)`` holding many entries.

    ``period`` is the day the work happened, ``"YYYY-MM-DD"`` in the logging
    user's timezone. Everyone who books time on that customer that day appends
    into the *same* document — that is the point of the bucket.

    ``part`` exists only as an overflow valve: it is ``1`` for virtually every
    bucket, and only a day busy enough to pass ``MAX_ENTRIES_PER_BUCKET`` ever
    opens a ``2``. Readers must therefore never assume one document per day —
    always iterate whatever the day-range query returns (the services do).

    ``total_minutes`` caches the sum of *active* minutes and ``entry_count`` the
    number of stored entries (including cancelled ones — it guards document
    size, not the report)."""

    meta = {
        "collection": "tt_buckets",
        "indexes": [
            {"fields": ["team", "customer", "period", "part"], "unique": True},
            # Day-range scans: the day sheet and every report filter on these.
            ("team", "period"),
            # Backlink lookup: "all time logged on this task" (inspector).
            "entries.source_item_id",
            # Dedup scan when re-importing from an external system.
            "entries.external_id",
            # Re-attribution when a Zammad agent is mapped to a Cairn account.
            "entries.external_user",
        ],
    }

    team = ReferenceField("Team", required=True)
    customer = ReferenceField(Customer, required=True)
    period = StringField(required=True)  # "YYYY-MM-DD"
    part = IntField(default=1, min_value=1)

    total_minutes = IntField(default=0)
    entry_count = IntField(default=0)
    entries = EmbeddedDocumentListField(TimeEntry)

    created_at = DateTimeField(default=_utcnow)
    updated_at = DateTimeField(default=_utcnow)

    def __str__(self) -> str:
        suffix = f" #{self.part}" if (self.part or 1) > 1 else ""
        return f"{self.customer} {self.period}{suffix}"
