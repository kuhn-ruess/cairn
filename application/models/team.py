"""Teams — a team_admin owns a team; premium members belong to one team.

Invites live embedded on the team so a single fetch covers the team and
its pending invites.
"""
import secrets
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from mongoengine import (
    DateTimeField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentListField,
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


class Team(Document):
    meta = {"collection": "teams", "indexes": ["admin", "invites.token"]}

    name = StringField(required=True, max_length=120)
    admin = ReferenceField(User, required=True)
    created_at = DateTimeField(default=_utcnow)
    invites = EmbeddedDocumentListField(TeamInvite)

    def members_query(self):
        from application.models.user import User as _User
        return _User.objects(team=self)

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

    def remove_invite(self, pk_str: str) -> bool:
        keep = [i for i in (self.invites or []) if str(i.pk_ref) != str(pk_str)]
        if len(keep) == len(self.invites or []):
            return False
        self.invites = keep
        self.save()
        return True

    def __str__(self) -> str:
        return self.name
