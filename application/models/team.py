"""Teams — a team_admin owns a team; premium members belong to one team.

Invites live embedded on the team so a single fetch covers the team and
its pending invites.
"""
import secrets
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from mongoengine import (
    BooleanField,
    DateTimeField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentListField,
    ListField,
    ObjectIdField,
    ReferenceField,
    StringField,
)

from application.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_expiry() -> datetime:
    return _utcnow() + timedelta(days=14)


class TeamInvite(EmbeddedDocument):
    """An outstanding (or consumed) invite, embedded on its Team.

    The ``token`` is the opaque value the recipient clicks. Lookups
    happen via the parent ``Team`` — see ``Team.find_by_invite_token``."""

    pk_ref = ObjectIdField(default=lambda: ObjectId())
    email = StringField(required=True, max_length=255)
    token = StringField(required=True,
                        default=lambda: secrets.token_urlsafe(24))
    created_at = DateTimeField(default=_utcnow)
    expires_at = DateTimeField(default=_default_expiry)
    used_at = DateTimeField()
    used_by = ReferenceField(User)

    @property
    def pk(self):
        return self.pk_ref

    @property
    def is_valid(self) -> bool:
        if self.used_at:
            return False
        if not self.expires_at:
            return True
        return self.expires_at > _utcnow()


class ZammadAgent(EmbeddedDocument):
    """One person an import has seen booking time, and who they are in Cairn.

    (The field it lives in is still called ``zammad_agents``: it started with
    Zammad, and renaming a field under a live database costs more than the
    tidier name is worth. ``source`` says which system a row came from.)

    The roster lives on the *team*, not on the syncing user: a booking made by
    a colleague must be attributed the same way no matter whose sync picks it
    up, and the team is what gets billed. Matching is attempted automatically
    (email, then login/username, then display name); anything the automatic
    pass can't resolve is kept here **unmatched** so the UI can say so and
    offer the assignment — an agent silently booked under a bare name is
    exactly the thing that goes unnoticed until an invoice is wrong.

    ``user`` is the Cairn account the agent's time belongs to (``None`` while
    unmatched). ``matched_by`` records how we got there, so a manual
    assignment is never overwritten by the automatic matcher on the next sync.
    """

    # The id in the system this person comes from, and which system that is.
    # Together they form ``key`` — the value stamped on imported entries.
    zammad_id = StringField(required=True, max_length=32)
    source = StringField(default="zammad", max_length=16)
    login = StringField(max_length=255)
    email = StringField(max_length=255)
    name = StringField(max_length=255)
    user = ReferenceField(User)
    matched_by = StringField(choices=("email", "login", "name", "manual"))
    last_seen_at = DateTimeField(default=_utcnow)

    @property
    def key(self) -> str:
        """The value stamped on imported entries — ``<source>:user:<id>``."""
        return f"{self.source or 'zammad'}:user:{self.zammad_id}"

    @property
    def label(self) -> str:
        return self.name or self.login or self.email or f"#{self.zammad_id}"


class Team(Document):
    meta = {"collection": "teams", "indexes": ["admin", "invites.token"]}

    name = StringField(required=True, max_length=120)
    # ``admin`` is the **paying** admin — the one whose subscription carries the
    # team's seats. ``admins`` is every user with management rights (the payer
    # plus co-admins). The payer is always one of ``admins``. Querying
    # ``Team.objects(admin=user)`` therefore means "teams this user pays for".
    admin = ReferenceField(User, required=True)
    admins = ListField(ReferenceField(User))
    # Members allowed to *bill* (lock) time entries. A biller can settle time
    # across every TimeTrackingProject reachable from a Cairn item they're a
    # member of — scoped by the GTD subtree they have rights to, not account-wide
    # (this is the deliberate edge over mite, which only has account-wide admins).
    billers = ListField(ReferenceField(User))
    created_at = DateTimeField(default=_utcnow)
    invites = EmbeddedDocumentListField(TeamInvite)
    # Family (non-commercial) team → flat base price covering FAMILY_INCLUDED_SEATS
    # member seats, extra seats billed cheaper than organisation seats. When
    # False the team is an organisation (every member seat billed). The choice
    # is made by the payer at checkout; see application.services.billing.
    family = BooleanField(default=False)
    # Zammad agents seen by the time import, and their Cairn counterpart.
    # Embedded because it is read on every sync and written rarely — see
    # ZammadAgent.
    zammad_agents = EmbeddedDocumentListField(ZammadAgent)

    def zammad_agent(self, zammad_id, source: str = "zammad"):
        zid = str(zammad_id or "")
        if not zid:
            return None
        for a in self.zammad_agents or []:
            if a.zammad_id == zid and (a.source or "zammad") == source:
                return a
        return None

    def person_by_key(self, key: str):
        """Roster entry for a stamped ``<source>:user:<id>``, if we know it."""
        for a in self.zammad_agents or []:
            if a.key == key:
                return a
        return None

    def unmatched_zammad_agents(self) -> list:
        """Agents whose time is booked under a bare Zammad name."""
        return [a for a in (self.zammad_agents or []) if not a.user]

    def members_query(self):
        from application.models.user import User as _User
        return _User.objects(team=self)

    def manager_ids(self) -> set:
        """Set of str pks for everyone allowed to manage this team — the
        payer plus any co-admins."""
        ids = {str(a.pk) for a in (self.admins or []) if a}
        if self.admin:
            ids.add(str(self.admin.pk))
        return ids

    def is_managed_by(self, user) -> bool:
        return bool(user) and str(user.pk) in self.manager_ids()

    def find_invite(self, *, pk_str: str | None = None, token: str | None = None):
        for inv in self.invites or []:
            if pk_str and str(inv.pk_ref) == str(pk_str):
                return inv
            if token and inv.token == token:
                return inv
        return None

    @classmethod
    def find_by_invite_token(cls, token: str):
        """Return ``(team, invite)`` for a raw token, or ``None``."""
        if not token:
            return None
        team = cls.objects(invites__token=token).first()
        if not team:
            return None
        inv = team.find_invite(token=token)
        return (team, inv) if inv else None

    def add_invite(self, email: str) -> TeamInvite:
        inv = TeamInvite(email=email)
        self.invites.append(inv)
        self.save()
        return inv

    def consume_invite(self, token: str, user) -> TeamInvite | None:
        inv = self.find_invite(token=token)
        if inv is None:
            return None
        inv.used_at = _utcnow()
        inv.used_by = user
        self.save()
        return inv

    def prune_invites(self) -> int:
        """Drop consumed or expired invites so the pending list stays clean.
        Returns how many were removed. ``is_valid`` is false for both used and
        past-expiry invites, so it's the only test needed."""
        before = list(self.invites or [])
        kept = [i for i in before if i.is_valid]
        if len(kept) != len(before):
            self.invites = kept
            self.save()
        return len(before) - len(kept)

    def remove_invite(self, pk_str: str) -> bool:
        keep = [i for i in (self.invites or []) if str(i.pk_ref) != str(pk_str)]
        if len(keep) == len(self.invites or []):
            return False
        self.invites = keep
        self.save()
        return True

    def __str__(self) -> str:
        return self.name
