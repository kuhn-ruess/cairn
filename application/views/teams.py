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
    """Pick which team to show on the page.

    ``?team=<pk>`` switches to any team the user is in (member or manager);
    otherwise default to a team they manage, then any team they're in.
    """
    teams = _user_teams(user)
    raw = request.args.get("team")
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
