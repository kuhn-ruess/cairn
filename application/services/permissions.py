"""Permission helpers and decorators for the level / team system.

Levels:
* ``free``         — solo, capped at ``FREE_PROJECT_LIMIT`` projects,
                     cannot share projects
* ``premium``      — solo, no project cap, may belong to a team
* ``team_admin``   — owns one team, can invite + share within it
* ``global_admin`` — only role that can change other users' level
"""
from functools import wraps

from flask import abort
from flask_login import current_user

FREE_PROJECT_LIMIT = 5

def global_admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_global_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper

def team_admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_team_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper

def project_count(user) -> int:
    """How many projects the user currently owns (excluding folders/sections)."""
    from application.models.item import Item as Item
    return Item.objects(owner=user, kind="project").count()

def at_project_limit(user) -> bool:
    if user.level != "free":
        return False
    return project_count(user) >= FREE_PROJECT_LIMIT

def teams_of(actor) -> list:
    """Every team the actor participates in: the one team they belong to
    as a member (``user.team``) plus all teams they admin — a user may
    admin several since admin-side team creation was added."""
    from application.models.team import Team
    teams = list(Team.objects(admin=actor))
    if actor.team and not any(t.pk == actor.team.pk for t in teams):
        teams.append(actor.team)
    return teams


def team_members(team) -> list:
    """All users in ``team``: its members plus the admin (whose own
    ``user.team`` may point elsewhere, so they aren't always in the
    members query)."""
    from application.models.user import User
    users = list(User.objects(team=team))
    if team.admin and not any(u.pk == team.admin.pk for u in users):
        users.append(team.admin)
    return users


def can_share_with(actor, target_user) -> bool:
    """Can ``actor`` add ``target_user`` as a member of one of their projects?

    True when they share at least one team — across *any* of the actor's
    teams, not just the single team the actor belongs to as a member.
    """
    if actor == target_user:
        return True
    actor_team_ids = {str(t.pk) for t in teams_of(actor)}
    if not actor_team_ids:
        return False
    target_team_ids = {str(t.pk) for t in teams_of(target_user)}
    return bool(actor_team_ids & target_team_ids)

def shareable_user_ids(actor) -> list:
    """Return the pks of users the actor may share projects with.

    Always includes the actor; adds the members of every team the actor
    participates in (member team + all admin'd teams).
    """
    ids = {str(actor.pk)}
    for team in teams_of(actor):
        for u in team_members(team):
            ids.add(str(u.pk))
    return list(ids)


def resolve_visible_item(actor, item_id):
    """Look up an Item by pk and assert the actor may view it.

    Returns the Item on success, ``None`` otherwise. Use this before
    accepting any user-supplied ``project_id`` / ``parent_id`` so a
    caller can't reparent into a project they don't have access to.
    """
    if not item_id:
        return None
    from application.models.item import Item
    from bson.errors import InvalidId
    from mongoengine.errors import ValidationError
    try:
        item = Item.objects(pk=item_id).first()
    except (InvalidId, ValidationError):
        return None
    if not item or not item.can_be_viewed_by(actor):
        return None
    return item


def assignable_users(actor) -> list:
    """Users the actor may assign tasks to: themselves plus the members of
    every team they participate in (deduped, actor first). Powers the
    ``+name`` quick-add token and its autocomplete."""
    out = [actor]
    seen = {str(actor.pk)}
    for team in teams_of(actor):
        for u in team_members(team):
            if str(u.pk) not in seen:
                seen.add(str(u.pk))
                out.append(u)
    return out


def resolve_assignable_user_by_name(actor, name):
    """Resolve a ``+name`` token to an assignable user. Matches username or
    display name, case-insensitive: exact match first, then a unique prefix.
    Returns None when nothing matches or a prefix is ambiguous."""
    if not name:
        return None
    nl = name.strip().lower()
    if not nl:
        return None
    users = assignable_users(actor)

    def names(u):
        return (
            (u.username or "").lower(),
            (getattr(u, "display_name", "") or "").lower(),
        )

    for u in users:
        if nl in names(u):
            return u
    prefix = [u for u in users if any(n.startswith(nl) for n in names(u) if n)]
    return prefix[0] if len(prefix) == 1 else None


def resolve_assignable_user(actor, user_id):
    """Look up a User by pk and assert the actor may assign to them.

    Returns the User on success, ``None`` otherwise. Use this for any
    ``assigned_to_id`` / ``member_ids`` field; without this check a
    caller could grant edit rights to arbitrary accounts.
    """
    if not user_id:
        return None
    from application.models.user import User
    from bson.errors import InvalidId
    from mongoengine.errors import ValidationError
    try:
        target = User.objects(pk=user_id).first()
    except (InvalidId, ValidationError):
        return None
    if not target:
        return None
    if not can_share_with(actor, target):
        return None
    return target
