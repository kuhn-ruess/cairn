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

def can_share_with(actor, target_user) -> bool:
    """Can ``actor`` add ``target_user`` as a member of one of their projects?"""
    if actor == target_user:
        return True
    if not actor.team:
        return False
    return target_user.team == actor.team

def shareable_user_ids(actor) -> list:
    """Return the pks of users the actor may share projects with.

    Always includes the actor; adds team members when applicable.
    """
    ids = [str(actor.pk)]
    if actor.team:
        from application.models.user import User
        for u in User.objects(team=actor.team):
            if str(u.pk) != str(actor.pk):
                ids.append(str(u.pk))
    return ids
