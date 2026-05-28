"""Team admin pages — list members, send invites, accept invites."""
from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from application.models.team import Team
from application.models.user import User
from application.services.permissions import team_admin_required

teams_bp = Blueprint("teams", __name__, url_prefix="/team")


def _user_teams(user) -> list[Team]:
    """All teams the user is admin of (sorted by name) — a user may
    legitimately admin several teams since we added admin-side team
    creation."""
    return list(Team.objects(admin=user).order_by("+name"))


def _select_team(user) -> Team | None:
    """Pick which team to show on the page.

    ``?team=<pk>`` lets the admin switch when they own more than one;
    otherwise fall back to the first admin'd team, then to the team
    the user belongs to as a member.
    """
    raw = request.args.get("team")
    if raw:
        cand = Team.objects(pk=raw).first()
        if cand and (cand.admin == user or user.team == cand):
            return cand
    owned = _user_teams(user)
    if owned:
        return owned[0]
    return user.team


def _team_from_form(user) -> Team | None:
    """Look up a team via ``team_id`` form field and verify the
    user can manage it. Returns None on auth failure."""
    raw = (request.form.get("team_id") or "").strip()
    if not raw:
        return None
    team = Team.objects(pk=raw).first()
    if not team or team.admin != user:
        return None
    return team


@teams_bp.route("/")
@login_required
def index():
    user = current_user._get_current_object()
    team = _select_team(user)
    if not team:
        return render_template("teams/none.html")
    members = User.objects(team=team).order_by("+username")
    invites = sorted(
        (i for i in (team.invites or []) if not i.used_at),
        key=lambda i: i.email,
    )
    can_manage = user.is_team_admin and team.admin == user
    return render_template(
        "teams/index.html",
        team=team, members=members, invites=invites,
        can_manage=can_manage,
        all_teams=_user_teams(user),
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
    if request.method == "POST":
        user.team = team
        if user.level == "free":
            user.level = "premium"
        user.save()
        team.consume_invite(token, user)
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
        flash("Can't remove the team admin.", "danger")
        return redirect(url_for("teams.index", team=str(team.pk)))
    member.team = None
    member.save()
    flash(f"{member.username} removed from {team.name}.", "info")
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
