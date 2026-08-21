"""Team admin pages — list members, send invites, accept invites."""
from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from application.models.item import Item
from application.models.team import Team
from application.models.user import User
from application.services.permissions import team_admin_required

teams_bp = Blueprint("teams", __name__, url_prefix="/team")


def _user_teams(user) -> list[Team]:
    """Every team the user participates in (member, payer, co-admin or biller),
    sorted by name — the set offered in the team switcher."""
    from application.services.permissions import participating_teams
    return sorted(participating_teams(user), key=lambda t: (t.name or "").lower())


def _select_team(user) -> Team | None:
    """Pick which team to show — and, on a POST, which team to *write to*.

    ``team=<pk>`` switches to any team the user is in (member or manager);
    otherwise default to a team they manage, then any team they're in.

    It is read from ``request.values``, so a form's hidden ``team`` field counts
    as much as the query string: a page reached with ``?team=`` used to post
    back without it and silently save to the default team instead of the one on
    screen — the settings landed in a team the user was not looking at.
    """
    teams = _user_teams(user)
    raw = request.values.get("team")
    if raw:
        for t in teams:
            if str(t.pk) == raw:
                return t
        # Fall through to the default if they asked for a team they're not in.
    managed = [t for t in teams if t.is_managed_by(user)]
    if managed:
        return managed[0]
    if teams:
        return teams[0]
    return user.team


def _team_from_form(user) -> Team | None:
    """Look up a team via ``team_id`` form field and verify the user may
    manage it (paying admin or co-admin). Returns None on auth failure."""
    raw = (request.form.get("team_id") or "").strip()
    if not raw:
        return None
    team = Team.objects(pk=raw).first()
    if not team or not team.is_managed_by(user):
        return None
    return team


@teams_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()
    team = _select_team(user)
    if not team:
        return render_template("teams/none.html")
    # Use the canonical team-membership helper so the team owner is always
    # counted/listed even when their own ``user.team`` points elsewhere — the
    # plain ``User.objects(team=team)`` query missed the admin (wrong count).
    from application.services.permissions import team_members
    from application.services import billing
    # Clear out consumed/expired invites whenever a manager opens the page, so
    # stale invitations don't pile up in the pending list.
    if team.is_managed_by(user):
        team.prune_invites()
    members = sorted(team_members(team), key=lambda u: (u.username or "").lower())
    invites = sorted(
        (i for i in (team.invites or []) if i.is_valid),
        key=lambda i: i.email,
    )
    manager_ids = team.manager_ids()
    return render_template(
        "teams/index.html",
        team=team, members=members, invites=invites,
        can_manage=team.is_managed_by(user),
        is_payer=bool(team.admin and team.admin.pk == user.pk),
        payer=team.admin,
        manager_ids=manager_ids,
        biller_ids={str(b.pk) for b in (team.billers or []) if b},
        billing_active=billing.team_billing_active(team),
        billable=billing.billable_member_count(team),
        cost_label=billing.team_cost_label(team),
        all_teams=_user_teams(user),
    )


@teams_bp.route("/members/<user_id>")
@login_required
def member(user_id):
    """A teammate's open tasks assigned to them + the projects shared with
    them — scoped to what the *viewer* may see, so a member's private items
    never leak. Reachable by anyone who shares a team with the member."""
    from application.services.permissions import can_share_with
    from application.services.prefetch import prepare_list
    user = current_user._get_current_object()
    m = User.objects(pk=user_id).first()
    if not m or not can_share_with(user, m):
        abort(404)
    cand = (Item.objects(assigned_to=m, kind="task",
                         status__nin=["done", "dropped"])
            .order_by("+due_date", "+sort_order"))
    tasks = prepare_list([t for t in cand if t.can_be_viewed_by(user)])
    projects = [
        p for p in Item.objects(members=m, kind__in=["project", "folder"])
        .order_by("+title")
        if p.can_be_viewed_by(user)
    ]
    return render_template(
        "teams/member.html", member=m, tasks=tasks, projects=projects,
    )


@teams_bp.route("/invite", methods=["POST"])
@login_required
@team_admin_required
def invite():
    user = current_user._get_current_object()
    team = _team_from_form(user)
    if not team:
        abort(403)
    email = (request.form.get("email") or "").strip().lower()
    if not email:
        flash("Email is required.", "danger")
        return redirect(url_for("teams.index", team=str(team.pk)))
    existing = User.objects(email=email).first()
    if existing and existing.team == team:
        flash(f"{email} is already in this team.", "info")
        return redirect(url_for("teams.index", team=str(team.pk)))
    inv = team.add_invite(email)
    link = url_for("teams.accept", token=inv.token, _external=True)
    # An existing account gets an in-app notification so they can accept from
    # the bell instead of hunting for the emailed link.
    if existing:
        from application.services.notifications import notify_team_invite
        notify_team_invite(
            existing, team=team, inviter=user,
            link=url_for("teams.accept", token=inv.token),
        )
    flash(
        f"Invite created for {email} on {team.name}. Share this link: {link}",
        "success",
    )
    return redirect(url_for("teams.index", team=str(team.pk)))


@teams_bp.route("/invite/<token>", methods=["GET", "POST"])
def accept(token):
    """Accept a team invite. Requires login; pre-fills email on register."""
    hit = Team.find_by_invite_token(token)
    if not hit:
        return render_template("teams/invite_invalid.html"), 404
    team, inv = hit
    if not inv.is_valid:
        return render_template("teams/invite_invalid.html"), 404

    if not current_user.is_authenticated:
        from flask import session
        session["pending_team_invite"] = token
        flash(f"Sign in or register to join {team.name}.", "info")
        return redirect(url_for("auth.login", next=request.path))

    user = current_user._get_current_object()
    # Bind the invite to the email it was issued for — without this
    # anyone who acquires the link (shoulder surfing, referrer leak,
    # forwarded email) could claim it for themselves.
    if (inv.email or "").lower() != (user.email or "").lower():
        flash(
            "This invite was issued for a different email address — "
            "sign in with that account to accept it.",
            "warning",
        )
        return render_template("teams/invite_invalid.html"), 403
    if request.method == "POST":
        user.team = team
        # Member's premium seat is paid by the team's payer — recompute, then
        # push the new seat count to the payer's subscription.
        from application.services import billing
        billing.recompute_level(user, save=False)
        user.save()
        team.consume_invite(token, user)
        billing.sync_team_seats(team)
        flash(f"You're now part of {team.name}.", "success")
        return redirect(url_for("teams.index"))
    return render_template("teams/invite_accept.html", invite=inv, team=team)


@teams_bp.route("/members/<user_id>/remove", methods=["POST"])
@login_required
@team_admin_required
def remove_member(user_id):
    user = current_user._get_current_object()
    team = _team_from_form(user)
    if not team:
        abort(403)
    member = User.objects(pk=user_id, team=team).first()
    if not member:
        abort(404)
    if member == team.admin:
        flash("Can't remove the paying admin — hand billing to another admin first.",
              "danger")
        return redirect(url_for("teams.index", team=str(team.pk)))
    member.team = None
    # Removing a member also drops any co-admin role they held.
    team.admins = [a for a in (team.admins or []) if a and a.pk != member.pk]
    team.save()
    # Their premium seat goes away (unless they pay their own way) — recompute,
    # then update the payer's seat count.
    from application.services import billing
    billing.recompute_level(member, save=False)
    member.save()
    billing.sync_team_seats(team)
    flash(f"{member.username} removed from {team.name}.", "info")
    return redirect(url_for("teams.index", team=str(team.pk)))


@teams_bp.route("/admins/<user_id>/add", methods=["POST"])
@login_required
@team_admin_required
def add_admin(user_id):
    """Grant a team member co-admin (management) rights."""
    user = current_user._get_current_object()
    team = _team_from_form(user)
    if not team:
        abort(403)
    member = User.objects(pk=user_id, team=team).first()
    if not member:
        abort(404)
    if member.pk not in [a.pk for a in (team.admins or []) if a]:
        team.admins = list(team.admins or []) + [member]
        team.save()
        flash(f"{member.username} is now a team admin.", "success")
    return redirect(url_for("teams.index", team=str(team.pk)))


@teams_bp.route("/admins/<user_id>/remove", methods=["POST"])
@login_required
@team_admin_required
def remove_admin(user_id):
    """Revoke a co-admin's management rights. The paying admin can't be
    demoted this way — hand billing over first."""
    user = current_user._get_current_object()
    team = _team_from_form(user)
    if not team:
        abort(403)
    if team.admin and str(team.admin.pk) == str(user_id):
        flash("The paying admin stays an admin — hand billing over first.",
              "danger")
        return redirect(url_for("teams.index", team=str(team.pk)))
    team.admins = [a for a in (team.admins or []) if a and str(a.pk) != str(user_id)]
    team.save()
    flash("Admin rights revoked.", "info")
    return redirect(url_for("teams.index", team=str(team.pk)))


@teams_bp.route("/billers/<user_id>/add", methods=["POST"])
@login_required
@team_admin_required
def add_biller(user_id):
    """Grant a member time-tracking billing rights (settle/lock entries within
    the projects they have access to)."""
    user = current_user._get_current_object()
    team = _team_from_form(user)
    if not team:
        abort(403)
    member = User.objects(pk=user_id, team=team).first()
    if not member:
        abort(404)
    if member.pk not in [b.pk for b in (team.billers or []) if b]:
        team.billers = list(team.billers or []) + [member]
        team.save()
        flash(f"{member.username} can now bill time.", "success")
    return redirect(url_for("teams.index", team=str(team.pk)))


@teams_bp.route("/billers/<user_id>/remove", methods=["POST"])
@login_required
@team_admin_required
def remove_biller(user_id):
    """Revoke a member's billing rights."""
    user = current_user._get_current_object()
    team = _team_from_form(user)
    if not team:
        abort(403)
    team.billers = [b for b in (team.billers or []) if b and str(b.pk) != str(user_id)]
    team.save()
    flash("Billing rights revoked.", "info")
    return redirect(url_for("teams.index", team=str(team.pk)))


@teams_bp.route("/payer/<user_id>", methods=["POST"])
@login_required
@team_admin_required
def set_payer(user_id):
    """Designate which admin pays for the team. Only the current payer may
    hand this over."""
    user = current_user._get_current_object()
    team = _team_from_form(user)
    if not team:
        abort(403)
    if not (team.admin and team.admin.pk == user.pk):
        flash("Only the current paying admin can hand over billing.", "danger")
        return redirect(url_for("teams.index", team=str(team.pk)))
    new_payer = User.objects(pk=user_id).first()
    if not new_payer:
        abort(404)
    from application.services import billing
    try:
        billing.set_team_payer(team, new_payer)
    except billing.BillingError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("teams.index", team=str(team.pk)))
    flash(f"{new_payer.username} is now the paying admin — they need to start a "
          f"team subscription to keep billing active.", "info")
    return redirect(url_for("teams.index", team=str(team.pk)))


@teams_bp.route("/invites/<invite_id>/delete", methods=["POST"])
@login_required
@team_admin_required
def revoke_invite(invite_id):
    user = current_user._get_current_object()
    team = _team_from_form(user)
    if not team:
        abort(403)
    if not team.remove_invite(invite_id):
        abort(404)
    flash("Invite revoked.", "info")
    return redirect(url_for("teams.index", team=str(team.pk)))


@teams_bp.route("/zammad", methods=["GET", "POST"])
@login_required
def zammad():
    """The team's Zammad connection — one sync for everyone in it.

    A team setting, not a time-tracking one: it holds a service token that
    reads the whole instance and decides where every member's tickets land.
    That the hours it finds end up in the billing module is a consequence, not
    the reason it lives somewhere."""
    from application.services.crypto import decrypt_for, encrypt_for  # noqa: F401
    from application.services.permissions import team_members
    from application.services.zammad_import import ZammadError, default_time_from
    from application.services.zammad_team import (
        move_team_connection, purge_team_import, refile_team_owners, sync_team,
    )

    user = current_user._get_current_object()
    team = _select_team(user)
    if not team:
        return render_template("teams/none.html")
    if not team.is_managed_by(user):
        abort(403)

    stats = purge = refiled = None
    if request.method == "POST":
        # Every form here says which team it means. Refuse the ones that don't
        # rather than fall back to the default team — writing a connection into
        # a team the user wasn't looking at is exactly the bug this prevents.
        if (request.form.get("team") or "") != str(team.pk):
            abort(400)
        action = request.form.get("action")
        if action == "move":
            # Matched against the teams this user manages rather than looked up
            # by id — an id from elsewhere is then simply not a target.
            wanted = (request.form.get("target") or "").strip()
            target = next((t for t in _user_teams(user) if str(t.pk) == wanted
                           and t != team and t.is_managed_by(user)), None)
            if not target:
                abort(403)
            moved = move_team_connection(team, target)
            flash(f"Zammad connection moved to {target.name} "
                  f"(including {moved['agents']} agent mapping"
                  f"{'' if moved['agents'] == 1 else 's'}). What was already "
                  f"imported stayed with {team.name}.", "success")
            return redirect(url_for("teams.zammad", team=str(target.pk)))
        def _save_settings():
            """Take the connection form's fields — and *only* when they were on
            the form that was sent. The purge and re-file forms don't carry the
            checkboxes; reading them anyway meant every preview click silently
            switched auto-sync off, which is how a team quietly stops syncing."""
            url = (request.form.get("url") or "").strip()
            if url or request.form.get("token"):
                team.zammad_url = url
            token_in = (request.form.get("token") or "").strip()
            if token_in:
                team.zammad_token = encrypt_for(team, token_in)
            team.zammad_auto_sync = request.form.get("auto_sync") == "1"
            team.zammad_import_time = request.form.get("import_time") == "1"
            team.zammad_time_auto_customer = request.form.get("auto_customer") == "1"
            team.zammad_include_closed = request.form.get("include_closed") == "1"
            # Empty is not "import everything" — see default_time_from.
            team.zammad_time_from = (_valid_day(request.form.get("time_from"))
                                     or default_time_from())
            team.save()

        if action in ("preview-purge", "purge"):
            # The way back, in two steps: see what would go, then type the
            # team's name to mean it.
            what = {"tasks": request.form.get("purge_tasks") == "1",
                    "time": request.form.get("purge_time") == "1"}
            if not any(what.values()):
                flash("Pick what to remove.", "warning")
            elif action == "preview-purge":
                purge = purge_team_import(team, dry_run=True, **what)
            elif (request.form.get("confirm") or "").strip() != team.name:
                flash(f"Type the team name exactly (“{team.name}”) to remove "
                      f"the imported data — nothing was deleted.", "warning")
            else:
                purge = purge_team_import(team, dry_run=False, **what)
                flash(f"Removed {purge['tasks']} imported task(s), "
                      f"{purge['entries']} time entr"
                      f"{'y' if purge['entries'] == 1 else 'ies'}, "
                      f"{purge['projects']} project(s) and "
                      f"{purge['customers']} customer(s). Import again when "
                      f"you're ready.", "success")
                purge = None
        elif action in ("preview-refile", "refile"):
            # After matching the agents: hand the first import's tickets to the
            # people who own them in Zammad. Preview first, like the purge.
            try:
                refiled = refile_team_owners(
                    team, dry_run=action == "preview-refile")
            except ZammadError as exc:
                flash(str(exc), "danger")
            else:
                if action == "refile":
                    flash(f"Moved {refiled['moved']} task(s) to their Zammad "
                          f"owner.", "success")
        elif action == "sync":
            _save_settings()
            try:
                stats = sync_team(team, full=request.form.get("full") == "1",
                                  dry_run=request.form.get("dry_run") == "1")
            except ZammadError as exc:
                flash(str(exc), "danger")
            else:
                flash("Team sync finished.", "success")
        else:
            _save_settings()
            flash("Connection saved.", "success")

    members = sorted(team_members(team),
                     key=lambda m: (m.display_name or m.username or "").lower())
    # Where each member's tickets land, and whether they are matched to a
    # Zammad agent at all — the two things that decide whether a central sync
    # can file a ticket in the right person's area.
    mapped = {str(a.user.pk) for a in (team.zammad_agents or [])
              if a.user and (a.source or "zammad") == "zammad"}
    rows = []
    for m in members:
        target = Item.objects(pk=m.zammad_sync_project, owner=m).first() \
            if getattr(m, "zammad_sync_project", "") else None
        rows.append({"user": m, "matched": str(m.pk) in mapped,
                     "target": target.title if target else ""})
    from application.services import sync_log
    teams = _user_teams(user)
    return render_template(
        "teams/zammad.html", team=team, all_teams=teams,
        # Where the connection could be moved to: the other teams this user
        # manages — moving it into a team you can't administer is not a fix.
        move_targets=[t for t in teams if t != team and t.is_managed_by(user)],
        stats=stats, purge=purge, refiled=refiled, members=rows,
        # What the syncs have been doing — the team's own runs and its members'
        # personal ones, because "why is there no time from Tuesday" is the same
        # question whichever of them ran.
        runs=sync_log.recent(team=team, users=members, limit=40),
        has_token=bool(team.zammad_token), can_manage=True)


def _valid_day(raw) -> str:
    """``"2026-08-20"`` or ``""`` — anything unparseable means "no cutoff",
    because a silently wrong date here quietly drops hours."""
    from datetime import date as _date
    value = (raw or "").strip()
    if not value:
        return ""
    try:
        return _date.fromisoformat(value).isoformat()
    except ValueError:
        return ""
