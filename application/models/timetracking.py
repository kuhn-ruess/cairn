"""Team time-tracking / billing module — a NoSQL-optimised replacement for mite.

Design constraints (decided with the user):

* **Team-only.** The whole module is gated behind team membership; solo
  free/premium users never see it.
* **Never delete.** Logged time is an append-only record. An entry is never
  removed — it carries a ``status ∈ {active, cancelled}`` and is soft-cancelled
  in place (with who/when). ``locked`` entries (billed) are frozen entirely.
* **Few documents.** Entries are *not* one-doc-each. They are bucketed: one
  :class:`TimeBucket` per ``(team, customer, month)`` holds every entry for that
  customer in that month, appended via an atomic ``$push``. A customer with a
  year of activity is ~12 documents, not thousands.

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
    ReferenceField,
    StringField,
)

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
    created_at = DateTimeField(default=_utcnow)

    def __str__(self) -> str:
        return self.name


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

    # Customer share link (read-only, hours only).
    share_enabled = BooleanField(default=False)
    share_token = StringField(unique=True, sparse=True)

    created_at = DateTimeField(default=_utcnow)

    def regenerate_share_token(self) -> str:
        self.share_token = secrets.token_urlsafe(24)
        return self.share_token

    def __str__(self) -> str:
        return self.name


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
    note = StringField(default="")

    # Soft backlink to the Cairn item the time was logged from. Stored as a
    # plain string id (NOT a ReferenceField) on purpose: dereferencing must
    # never crash if the task/project was deleted. ``source_label`` keeps the
    # name so the entry stays readable even when the item is gone.
    source_item_id = StringField()
    source_kind = StringField()  # task / project, for the backlink url
    source_label = StringField(default="")

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


class TimeBucket(Document):
    """One document per ``(team, customer, period)`` holding many entries.

    ``period`` is ``"YYYY-MM"`` (derived from each entry's ``spent_on``).
    ``total_minutes`` caches the sum of *active* minutes so listing a customer's
    months doesn't have to unpack the entries."""

    meta = {
        "collection": "tt_buckets",
        "indexes": [
            {"fields": ["team", "customer", "period"], "unique": True},
        ],
    }

    team = ReferenceField("Team", required=True)
    customer = ReferenceField(Customer, required=True)
    period = StringField(required=True)  # "YYYY-MM"

    total_minutes = IntField(default=0)
    entries = EmbeddedDocumentListField(TimeEntry)

    created_at = DateTimeField(default=_utcnow)
    updated_at = DateTimeField(default=_utcnow)

    def __str__(self) -> str:
        return f"{self.customer} {self.period}"
