"""Global admin pages — user management."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from application.models.team import Team
from application.models.user import User
from application.services.permissions import global_admin_required

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

LEVELS = ("free", "premium", "team_admin", "global_admin")


@admin_bp.route("/users")
@login_required
@global_admin_required
def users():
    return render_template("admin/users.html",
                           users=User.objects().order_by("+username"),
                           levels=LEVELS)


# Which part of the product a model module belongs to, in the order the tabs
# should read. A module that isn't named here still shows up — under "Other" —
# because the model set is discovered, not listed, and a new model appearing
# in the wrong tab is a smaller problem than one that doesn't appear at all.
MODEL_GROUPS = (
    ("Tasks & notes", ("item",)),
    ("Time tracking", ("timetracking", "timer")),
    ("People & teams", ("user", "team")),
    ("Organising", ("tag", "perspective", "habit")),
    ("Integrations", ("calendar", "sync_run", "notification")),
)


@admin_bp.route("/data-model")
@login_required
@global_admin_required
def data_model():
    """Introspect mongoengine documents and render their field map.

    Useful for reviews and onboarding — every model with its fields,
    types, indexes, and collection name pulled live from the class
    metadata (so it can't drift from the code). The model *set* is
    auto-discovered from the mongoengine registry, so newly added models
    (and embedded docs) appear here automatically — no hand-kept list.

    Grouped into tabs by module (``MODEL_GROUPS``) because 28 documents in one
    scroll is a list, not a map."""
    import importlib
    import pkgutil

    import application.models as models_pkg
    from mongoengine import Document as _Doc
    from mongoengine.base.common import _document_registry

    # Import every model module so all Document/EmbeddedDocument classes have
    # registered themselves before we read the registry.
    for mod in pkgutil.iter_modules(models_pkg.__path__):
        importlib.import_module(f"application.models.{mod.name}")

    docs = [
        cls for cls in dict.fromkeys(_document_registry.values())
        if cls.__module__.startswith("application.models")
        and not (getattr(cls, "_meta", {}) or {}).get("abstract")
    ]
    # Top-level collections first, then embedded docs; alphabetical within each.
    docs.sort(key=lambda c: (not issubclass(c, _Doc), c.__name__))

    def describe(doc):
        fields = []
        for name, fld in doc._fields.items():
            ftype = type(fld).__name__
            extra_parts = []
            if getattr(fld, "required", False):
                extra_parts.append("required")
            if getattr(fld, "unique", False):
                extra_parts.append("unique")
            choices = getattr(fld, "choices", None)
            if choices:
                shown = ", ".join(
                    c[0] if isinstance(c, (tuple, list)) else str(c)
                    for c in choices
                )
                extra_parts.append(f"choices={{{shown}}}")
            inner = getattr(fld, "field", None)
            if inner is not None:
                extra_parts.append(f"of {type(inner).__name__}")
            ref = getattr(fld, "document_type_obj", None) or getattr(fld, "document_type", None)
            if ref is not None and ftype in ("ReferenceField", "EmbeddedDocumentField",
                                              "EmbeddedDocumentListField"):
                ref_name = ref.__name__ if hasattr(ref, "__name__") else str(ref)
                extra_parts.append(f"→ {ref_name}")
            fields.append((name, ftype, ", ".join(extra_parts)))
        meta = getattr(doc, "_meta", {}) or {}
        indexes = meta.get("indexes") or []
        from mongoengine import Document as _D
        is_embedded = not issubclass(doc, _D)
        return {
            "name": doc.__name__,
            "field_count": len(fields),
            "collection": meta.get("collection") or ("(embedded)" if is_embedded else "—"),
            "module": doc.__module__,
            "doc": (doc.__doc__ or "").strip(),
            "fields": fields,
            "indexes": indexes,
            "embedded": is_embedded,
        }

    described = [describe(d) for d in docs]

    # Group for the tabs, keeping the order above; anything from a module the
    # map doesn't know lands in "Other" rather than disappearing.
    where = {mod: title for title, mods in MODEL_GROUPS for mod in mods}
    groups = {title: [] for title, _ in MODEL_GROUPS}
    for m in described:
        title = where.get(m["module"].rsplit(".", 1)[-1], "Other")
        groups.setdefault(title, []).append(m)
    def slug(title):
        out = "".join(c if c.isalnum() else "-" for c in title.lower())
        while "--" in out:
            out = out.replace("--", "-")
        return out.strip("-")

    tabs = [{"title": t, "models": ms, "slug": slug(t)}
            for t, ms in groups.items() if ms]

    return render_template(
        "admin/data_model.html",
        models=described, tabs=tabs,
        collections=sum(1 for m in described if not m["embedded"]),
        embedded=sum(1 for m in described if m["embedded"]),
    )


@admin_bp.route("/users/<user_id>/delete", methods=["POST"])
@login_required
@global_admin_required
def delete_user(user_id):
    """Bulk cascade-delete a user and everything they exclusively own."""
    from application.services.cleanup import delete_user_cascade
    target = User.objects(pk=user_id).first()
    if not target:
        abort(404)
    if target == current_user._get_current_object():
        flash("Refusing to delete your own account from the admin UI.", "danger")
        return redirect(url_for("admin.users"))
    counts = delete_user_cascade(target)
    flash(
        f"Deleted user — {counts.get('projects_owned', 0)} project(s), "
        f"{counts.get('tasks_in_owned_projects', 0) + counts.get('loose_tasks', 0)} task(s), "
        f"{counts.get('api_tokens', 0)} token(s).",
        "info",
    )
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<user_id>/level", methods=["POST"])
@login_required
@global_admin_required
def set_level(user_id):
    target = User.objects(pk=user_id).first()
    if not target:
        abort(404)
    level = request.form.get("level")
    if level not in LEVELS:
        flash("Invalid level.", "danger")
        return redirect(url_for("admin.users"))

    # Setting a level from the admin UI grants an operator *comp* plan (free
    # of charge) so it survives the nightly billing reconcile — this is how a
    # global admin marks someone (or a team admin) as free. ``free`` clears it.
    target.comp_plan = {"premium": "premium", "team_admin": "team"}.get(level, "")
    target.level = level
    # Promoting someone to team_admin auto-creates a team if they don't
    # have one. Demoting from team_admin leaves the team untouched so
    # historical memberships aren't lost.
    if level == "team_admin" and not target.team:
        team = Team(name=f"{target.display_name or target.username}'s team",
                    admin=target, admins=[target]).save()
        target.team = team
    target.save()
    flash(f"{target.username} is now {level}"
          + (" (free / comp)." if target.comp_plan else "."), "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/teams/<team_id>/family", methods=["POST"])
@login_required
@global_admin_required
def team_family(team_id):
    """Toggle a team's non-commercial / family discount (€2 instead of €5).

    Only affects the price the admin pays at their NEXT checkout / plan change
    — it doesn't retro-bill an existing subscription (switch that in the
    Stripe portal)."""
    team = Team.objects(pk=team_id).first()
    if not team:
        abort(404)
    team.family = not team.family
    team.save()
    flash(f"'{team.name}' is now {'a family / non-commercial' if team.family else 'a commercial'} team.",
          "info")
    return redirect(url_for("admin.teams"))


@admin_bp.route("/teams/<team_id>/comp", methods=["POST"])
@login_required
@global_admin_required
def team_comp(team_id):
    """Toggle a team as free of charge — grants/revokes the admin's comp team
    plan so the team needs no paid subscription and survives the reconcile."""
    from application.services.billing import recompute_level
    team = Team.objects(pk=team_id).first()
    if not team or not team.admin:
        abort(404)
    admin_user = team.admin
    if admin_user.comp_plan == "team":
        admin_user.comp_plan = ""
        msg = f"'{team.name}' is no longer free — admin reverts to paid/free billing."
    else:
        admin_user.comp_plan = "team"
        msg = f"'{team.name}' is now free of charge."
    recompute_level(admin_user, save=False)
    admin_user.save()
    flash(msg, "info")
    return redirect(url_for("admin.teams"))


@admin_bp.route("/teams")
@login_required
@global_admin_required
def teams():
    """List every team in the instance + an inline create form."""
    teams_qs = Team.objects().order_by("+name")
    return render_template(
        "admin/teams.html",
        teams=list(teams_qs),
        candidate_admins=list(User.objects().order_by("+username")),
    )


@admin_bp.route("/teams/create", methods=["POST"])
@login_required
@global_admin_required
def teams_create():
    name = (request.form.get("name") or "").strip()
    admin_id = (request.form.get("admin_id") or "").strip()
    if not name:
        flash("Team name is required.", "danger")
        return redirect(url_for("admin.teams"))
    admin_user = User.objects(pk=admin_id).first() if admin_id else None
    if not admin_user:
        flash("Pick a valid team admin.", "danger")
        return redirect(url_for("admin.teams"))
    team = Team(name=name, admin=admin_user, admins=[admin_user]).save()
    # Make sure the chosen admin actually has team_admin level and the
    # team pointer — otherwise they can't manage it from the team page.
    if admin_user.level not in ("team_admin", "global_admin"):
        admin_user.level = "team_admin"
    if not admin_user.team:
        admin_user.team = team
    admin_user.save()
    flash(f"Team '{team.name}' created with {admin_user.username} as admin.", "success")
    return redirect(url_for("admin.teams"))


@admin_bp.route("/teams/<team_id>/delete", methods=["POST"])
@login_required
@global_admin_required
def teams_delete(team_id):
    from application.services.cleanup import delete_team_cascade
    team = Team.objects(pk=team_id).first()
    if not team:
        abort(404)
    r = delete_team_cascade(team)
    flash(f"Team deleted — {r.get('members_unassigned', 0)} member(s) detached.",
          "info")
    return redirect(url_for("admin.teams"))
