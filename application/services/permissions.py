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
    """Coarse gate: the user must manage at least one team — either as the
    paying admin (``level == team_admin``) or as a co-admin (listed in some
    ``Team.admins``). Per-team authorization is still enforced inside each
    view via ``Team.is_managed_by``."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(403)
        if not current_user.is_team_admin:
            from application.models.team import Team
            if not Team.objects(admins=current_user._get_current_object()).first():
                abort(403)
        return view(*args, **kwargs)
    return wrapper

def team_required(view):
    """Gate for the team time-tracking module: the user must belong to a team
    (as a member or as an admin). Solo free/premium users get 403."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(403)
        from application.services.timetracking import has_team
        if not has_team(current_user):
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

def participating_teams(actor) -> list:
    """Canonical "all teams this user belongs to" — broader than ``teams_of``:
    their member team (``user.team``) plus every team where they're the payer
    (``admin``), a co-admin (``admins``) or a biller (``billers``). This is the
    set the team page + time-tracking module use for the team switcher, so a
    co-admin or member never loses sight of a team they're in."""
    from mongoengine import Q
    from application.models.team import Team
    actor = actor._get_current_object() if hasattr(actor, "_get_current_object") else actor
    if actor is None:
        return []
    teams = list(Team.objects(Q(admins=actor) | Q(admin=actor) | Q(billers=actor)))
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


def visible_items_q(actor):
    """Base visibility query for items the actor can see DIRECTLY: owned,
    assigned to them, shared with them by name (members), or shared with one of
    their teams (shared_teams). Shared building block for the per-view ``_scope``
    helpers — note this is the flat/direct query for LISTS; inherited (parent →
    child) visibility with override is resolved per-item in
    ``Item.can_be_viewed_by``."""
    from mongoengine import Q
    return (Q(owner=actor) | Q(assigned_to=actor) | Q(members=actor)
            | Q(shared_teams__in=teams_of(actor)))


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


def managed_teams(actor) -> list:
    """Teams the actor manages (is payer/admin or co-admin of)."""
    from mongoengine import Q
    from application.models.team import Team
    return list(Team.objects(Q(admin=actor) | Q(admins=actor)))


def managed_team_ids(actor) -> set:
    return {str(t.pk) for t in managed_teams(actor)}


def admin_subtree_pks(actor) -> set:
    """Container pks a team admin may open in full: everything under any subtree
    shared with a team they manage — descendant overrides DON'T lock the admin
    out (unlike regular members). Cached per request."""
    try:
        from flask import g, has_request_context
        in_request = has_request_context()
    except Exception:
        g, in_request = None, False
    if in_request:
        cached = getattr(g, "_admin_subtree_pks", None)
        if cached is not None:
            return cached
    from application.models.item import Item
    teams = managed_teams(actor)
    pks = set()
    if teams:
        roots = list(Item.objects(
            shared_teams__in=teams, kind__in=["project", "folder", "section"],
        ).only("pk"))
        pks = {str(r.pk) for r in roots}
        frontier = [r.pk for r in roots]
        while frontier:
            kids = Item.objects(
                parent__in=frontier, kind__in=["project", "folder", "section"],
            ).only("pk")
            nxt = []
            for k in kids:
                kp = str(k.pk)
                if kp in pks:
                    continue
                pks.add(kp)
                nxt.append(k.pk)
            frontier = nxt
    if in_request:
        g._admin_subtree_pks = pks
    return pks


def container_visibility_q(actor):
    """Query for containers (project/folder/section) the actor sees DIRECTLY:
    owned, shared with them by name (members), or shared with one of their teams
    (shared_teams). The flat/direct building block — inherited descendants are
    expanded by ``viewable_container_pks``."""
    from mongoengine import Q
    return (Q(owner=actor) | Q(members=actor)
            | Q(shared_teams__in=teams_of(actor)))


def viewable_container_pks(user) -> set:
    """Set of str pks for every project/folder/section ``user`` may open —
    owned, directly shared (members or one of their teams), or inherited as a
    descendant of a shared container. Inheritance stops at any descendant that
    sets its OWN share (members/shared_teams): that override is only visible if
    it shares directly with the user (then it's already in the direct set). This
    mirrors ``Item.can_be_viewed_by``'s nearest-share-wins rule.

    Cached on ``flask.g`` per request so the Inbox query and the
    ``can_view_container`` template helper share ONE computation."""
    from application.models.item import Item
    try:
        from flask import g, has_request_context
        in_request = has_request_context()
    except Exception:
        g, in_request = None, False
    if in_request:
        cached = getattr(g, "_viewable_container_pks", None)
        if cached is not None:
            return cached
    direct = list(Item.objects(
        container_visibility_q(user),
        kind__in=["project", "folder", "section"],
    ).only("pk"))
    pks = {str(c.pk) for c in direct}
    # Walk down: a shared container's children inherit access unless they
    # override with their own share. One query per tree level until nothing new.
    frontier = [c.pk for c in direct]
    while frontier:
        children = Item.objects(
            parent__in=frontier,
            kind__in=["project", "folder", "section"],
        ).only("pk", "members", "shared_teams")
        nxt = []
        for c in children:
            cpk = str(c.pk)
            if cpk in pks:
                continue
            if c.members or c.shared_teams:
                continue  # override: not inherited (direct set already has it if shared with us)
            pks.add(cpk)
            nxt.append(c.pk)
        frontier = nxt
    # Team admins additionally see every container under a subtree shared with a
    # team they manage — overrides don't lock them out.
    pks |= admin_subtree_pks(user)
    if in_request:
        g._viewable_container_pks = pks
    return pks


def locked_container_pks(user) -> set:
    """Containers the user can see EXIST — a direct child of something they can
    view — but may NOT open, because that child overrides the inherited share
    with its own (which excludes the user). The navigator shows these as locked
    instead of hiding them, so it's clear access was deliberately restricted."""
    from bson import ObjectId
    from application.models.item import Item
    try:
        from flask import g, has_request_context
        in_request = has_request_context()
    except Exception:
        g, in_request = None, False
    if in_request:
        cached = getattr(g, "_locked_container_pks", None)
        if cached is not None:
            return cached
    viewable = viewable_container_pks(user)
    locked = set()
    if viewable:
        children = Item.objects(
            parent__in=[ObjectId(p) for p in viewable],
            kind__in=["project", "folder"],
        ).only("pk")
        locked = {str(c.pk) for c in children if str(c.pk) not in viewable}
    if in_request:
        g._locked_container_pks = locked
    return locked


def inbox_q(user):
    """Mongo filter (a ``Q``) for the user's Inbox.

    Inbox = truly unfiled tasks (``parent=None``) PLUS tasks delegated to the
    user that they have NOT filed (``assignee_parent=None``) and whose project
    they cannot see — so an untriaged delegated task lands in the assignee's
    Inbox by default instead of hiding under the owner's invisible project.
    Pair with the usual ``_scope(user)`` + ``kind='task'`` + status filters."""
    from bson import ObjectId
    from mongoengine import Q
    vis = [ObjectId(p) for p in viewable_container_pks(user)]
    delegated = (Q(assigned_to=user) & Q(owner__ne=user)
                 & Q(assignee_parent=None) & Q(parent__ne=None)
                 & Q(parent__nin=vis))
    return Q(parent=None) | delegated


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
