"""Cairn CLI commands: user creation, API tokens, demo seeding.

Can be invoked two ways:

* ``flask --app wsgi:app cairn <cmd>`` — via Flask's CLI group, when
  developing locally with the ``flask`` entry point.
* ``./cairn <cmd>`` — via the source-checkout wrapper, which calls
  :func:`main` here. This is the form used by ``./helper``.
"""
import click
from flask.cli import AppGroup

from application.models.tag import Tag

from application.models.user import User
from application.models.item import Item

cairn_cli = AppGroup("cairn", help="Cairn administration commands.")

@cairn_cli.command("create-user")
@click.argument("username")
@click.argument("email")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
@click.option("--admin/--no-admin", default=False)
def create_user(username, email, password, admin):
    """Create a new user."""
    if User.objects(username=username).first():
        raise click.ClickException(f"username '{username}' already exists")
    if User.objects(email=email).first():
        raise click.ClickException(f"email '{email}' already exists")
    user = User(username=username, email=email, display_name=username, is_admin=admin)
    user.set_password(password)
    user.save()
    click.echo(f"Created user {user.username} ({user.pk})")

@cairn_cli.command("wipe-legacy-collections")
def wipe_legacy_collections():
    """Drop the pre-unification ``tasks`` and ``projects`` collections.

    The unified ``Item`` model lives in the ``items`` collection. The
    old collections are inert (no model points there anymore) but take
    up space; this command removes them after a successful re-import.
    """
    from mongoengine import get_db
    db = get_db()
    for name in ("tasks", "projects"):
        if name in db.list_collection_names():
            db[name].drop()
            click.echo(f"dropped collection: {name}")
        else:
            click.echo(f"skip (missing): {name}")

@cairn_cli.command("cleanup-completed-tasks")
@click.option("--dry-run", is_flag=True, help="Report only, don't delete.")
@click.option("--user", "username", help="Only sweep one user (by username).")
def cleanup_completed_tasks(dry_run, username):
    """Sweep done/dropped tasks past each user's retention window.

    Cron-friendly: run nightly. Per-user is a single indexed bulk-delete,
    so it scales to millions of tasks.
    """
    from application.services.cleanup import (
        purge_completed_tasks, purge_completed_tasks_all,
    )
    if username:
        u = User.objects(username=username).first()
        if not u:
            raise click.ClickException(f"no user '{username}'")
        n = purge_completed_tasks(u, dry_run=dry_run)
        verb = "would delete" if dry_run else "deleted"
        click.echo(f"{u.username}: {verb} {n} task(s)")
        return
    results = purge_completed_tasks_all(dry_run=dry_run)
    total = sum(results.values())
    verb = "would delete" if dry_run else "deleted"
    for name, n in results.items():
        if n:
            click.echo(f"  {name}: {verb} {n} task(s)")
    click.echo(f"total: {verb} {total} task(s) across {len(results)} user(s)")

@cairn_cli.command("cleanup-trash")
@click.option("--dry-run", is_flag=True, help="Report only, don't delete.")
@click.option("--user", "username", help="Only sweep one user (by username).")
def cleanup_trash(dry_run, username):
    """Permanently delete trashed items past each user's retention window.

    Cron-friendly: run nightly. Per-user is an indexed bulk-delete.
    """
    from application.services.trash import purge_trash, purge_trash_all
    if username:
        u = User.objects(username=username).first()
        if not u:
            raise click.ClickException(f"no user '{username}'")
        n = purge_trash(u, dry_run=dry_run)
        verb = "would delete" if dry_run else "deleted"
        click.echo(f"{u.username}: {verb} {n} item(s)")
        return
    results = purge_trash_all(dry_run=dry_run)
    total = sum(results.values())
    verb = "would delete" if dry_run else "deleted"
    for name, n in results.items():
        if n:
            click.echo(f"  {name}: {verb} {n} item(s)")
    click.echo(f"total: {verb} {total} item(s) across {len(results)} user(s)")

@cairn_cli.command("set-level")
@click.argument("email_or_username")
@click.argument("level", type=click.Choice(["free", "premium", "team_admin", "global_admin"]))
def set_level(email_or_username, level):
    """Promote / demote a user. Auto-creates a team when needed."""
    user = (
        User.objects(email=email_or_username).first()
        or User.objects(username=email_or_username).first()
    )
    if not user:
        raise click.ClickException(f"no user '{email_or_username}'")
    user.level = level
    if level == "team_admin" and not user.team:
        from application.models.team import Team
        team = Team(name=f"{user.display_name or user.username}'s team",
                    admin=user).save()
        user.team = team
        click.echo(f"created team {team.name} ({team.pk})")
    user.save()
    click.echo(f"{user.username} is now {level}")

@cairn_cli.command("reset-password")
@click.argument("username")
@click.option("--hours", default=2, type=int, help="Token validity in hours.")
def reset_password(username, hours):
    """Issue a one-time password-reset URL for a user.

    Useful when SMTP isn't configured — give the URL to the user via
    a secure channel and they can set a new password.
    """
    from flask import url_for

    user = User.objects(username=username).first()
    if not user:
        raise click.ClickException(f"user '{username}' not found")
    token = user.issue_password_reset(hours_valid=hours)
    click.echo(f"Reset URL for {user.username} (valid {hours}h):")
    # SERVER_NAME isn't always set in dev — fall back to a path.
    try:
        url = url_for("auth.reset_password", token=token, _external=True)
    except RuntimeError:
        url = f"/auth/reset-password/{token}"
    click.echo(url)

@cairn_cli.command("issue-token")
@click.argument("username")
@click.option("--name", default="default")
def issue_token(username, name):
    """Issue an API token for a user."""
    user = User.objects(username=username).first()
    if not user:
        raise click.ClickException(f"user '{username}' not found")
    _tok, raw = user.issue_api_token(name)
    click.echo(raw)

@cairn_cli.command("list-users")
def list_users():
    """List all users."""
    for u in User.objects():
        click.echo(f"{u.pk}\t{u.username}\t{u.email}\t{'admin' if u.is_admin else ''}")

@cairn_cli.command("import-todoist")
@click.argument("username")
@click.option("--token", required=True, help="Todoist personal API token.")
@click.option("--include-completed/--no-include-completed", default=False)
@click.option("--include-comments/--no-include-comments", default=True)
@click.option(
    "--project-id",
    "project_ids",
    multiple=True,
    help="Restrict to one or more Todoist project IDs (repeat the flag).",
)
@click.option("--dry-run", is_flag=True, help="Don't write to the DB.")
def import_todoist(username, token, include_completed, include_comments, project_ids, dry_run):
    """Import projects, labels and tasks from Todoist for a user."""
    from application.services.todoist_import import import_for_user, TodoistError

    user = User.objects(username=username).first()
    if not user:
        raise click.ClickException(f"user '{username}' not found")
    try:
        stats = import_for_user(
            user,
            token,
            include_completed=include_completed,
            include_comments=include_comments,
            only_project_ids=list(project_ids) or None,
            dry_run=dry_run,
        )
    except TodoistError as exc:
        raise click.ClickException(str(exc))

    click.echo(f"Projects: +{stats.projects_created} ~{stats.projects_updated}")
    click.echo(f"Tags:     +{stats.tags_created} ~{stats.tags_updated}")
    click.echo(f"Tasks:    +{stats.tasks_created} ~{stats.tasks_updated}")
    click.echo(f"Comments: +{stats.comments_imported}")
    if stats.errors:
        click.echo(f"Errors ({len(stats.errors)}):")
        for err in stats.errors:
            click.echo(f"  - {err}")
    if dry_run:
        click.echo("(dry-run: no changes written)")

@cairn_cli.command("import-zammad")
@click.argument("username")
@click.option("--url", required=True, help="Zammad instance URL.")
@click.option("--token", required=True, help="Zammad API token.")
@click.option("--project", "project_id", default=None, help="Cairn project pk to import into.")
@click.option("--assignee-only/--all-tickets", default=True,
              help="Only tickets assigned to the token holder (default).")
@click.option("--include-closed/--no-include-closed", default=False)
@click.option("--dry-run", is_flag=True, help="Don't write to the DB.")
def import_zammad(username, url, token, project_id, assignee_only, include_closed, dry_run):
    """Import tickets from a Zammad instance for a user."""
    from application.models.item import Item
    from application.services.zammad_import import import_for_user as _imp, ZammadError

    user = User.objects(username=username).first()
    if not user:
        raise click.ClickException(f"user '{username}' not found")
    target = None
    if project_id:
        target = Item.objects(pk=project_id, owner=user, kind="project").first()
        if not target:
            raise click.ClickException(f"project '{project_id}' not found or not yours")
    try:
        stats = _imp(
            user, url, token,
            project=target,
            assignee_only=assignee_only,
            include_closed=include_closed,
            dry_run=dry_run,
        )
    except ZammadError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Tickets: +{stats.tickets_created} ~{stats.tickets_updated} skipped={stats.tickets_skipped}")
    if stats.errors:
        click.echo(f"Errors ({len(stats.errors)}):")
        for err in stats.errors:
            click.echo(f"  - {err}")
    if dry_run:
        click.echo("(dry-run: no changes written)")


@cairn_cli.command("apply-label-rules")
@click.option("--user", "username", default=None,
              help="Limit to a single user; omit to run for everyone.")
@click.option("--dry-run", is_flag=True, help="Show changes without saving.")
def apply_label_rules_cmd(username, dry_run):
    """Re-evaluate Tag.maps_to_status on every existing task.

    Useful after setting a new auto-status rule — without this you'd
    have to wait for the next sync or manual save before tasks pick
    up the rule.
    """
    from application.models.item import Item
    from application.services.label_rules import apply_label_rules
    users = User.objects(username=username) if username else User.objects()
    total = 0
    for user in users:
        n = 0
        for it in Item.objects(owner=user, kind="task"):
            if apply_label_rules(it, user):
                if not dry_run:
                    it.save()
                n += 1
        total += n
        click.echo(f"{user.username}: {n} task(s) re-statused")
    click.echo(f"{'(dry-run) would update' if dry_run else 'Updated'} {total} task(s) total.")


@cairn_cli.command("convert-html-notes")
@click.option("--dry-run", is_flag=True, help="Only report, don't write.")
def convert_html_notes(dry_run):
    """Convert legacy ``\\`\\`\\`html ... \\`\\`\\`\\``-wrapped notes to Markdown.

    The Zammad importer used to box HTML article bodies in a fenced
    code block. We now convert HTML → Markdown at import time and
    store the result directly, so the fences need to be unwrapped
    AND the HTML inside translated into Markdown.
    """
    import re
    from application.models.item import Item
    from application.services.html_to_markdown import html_to_markdown
    pat = re.compile(r"```html\s*\n(.*?)\n```", re.DOTALL)
    qs = Item.objects(note__contains="```html")
    touched = 0
    for it in qs:
        old = it.note or ""
        def repl(m):
            return html_to_markdown(m.group(1))
        new_note, n = pat.subn(repl, old)
        if not n:
            continue
        click.echo(f"{it.kind} {str(it.pk)[:8]} : {(it.title or '')[:60]}  ({n}x)")
        touched += 1
        if not dry_run:
            it.note = new_note
            it.save()
    if dry_run:
        click.echo(f"(dry-run) would convert {touched} item(s)")
    else:
        click.echo(f"Converted {touched} item(s).")


@cairn_cli.command("repair-section-parents")
@click.option("--dry-run", is_flag=True, help="Only report, don't write.")
def repair_section_parents(dry_run):
    """Fix projects/folders that ended up with a section as parent.

    Such items are inconsistent — the model only allows project/folder
    parents on a sub-project. The fix is to hoist them up to the
    section's own parent (the containing project) so they nest
    correctly in the sidebar.
    """
    from application.models.item import Item
    bad = list(Item.objects(kind__in=["project", "folder"]))
    fixed = 0
    for p in bad:
        par = p.parent
        if par is None or par.kind != "section":
            continue
        hoisted = par.parent  # the section's owning project
        click.echo(f"{p.kind} {p.title[:50]} : {par.title} (section) → {hoisted.title if hoisted else 'top level'}")
        if not dry_run:
            p.parent = hoisted
            p.save()
        fixed += 1
    if dry_run:
        click.echo(f"(dry-run) would fix {fixed} item(s)")
    else:
        click.echo(f"Fixed {fixed} item(s).")


@cairn_cli.command("migrate-tags-to-embedded")
def migrate_tags_to_embedded():
    """One-shot: move the central ``tags`` collection into User.tags
    and rewrite Item.tags from ObjectId references to tag names.

    Idempotent: re-running after a successful migration finds the
    ``tags`` collection empty/dropped and does nothing.
    """
    from application.models.tag import Tag as EmbeddedTag

    db = User._get_collection().database
    if "tags" not in db.list_collection_names():
        click.echo("No legacy 'tags' collection — nothing to migrate.")
        return

    # Build {old_tag_id: name} once for the task pass.
    id_to_name = {}
    tags_by_owner = {}
    for raw in db.tags.find():
        id_to_name[raw["_id"]] = raw["name"]
        tags_by_owner.setdefault(raw["owner"], []).append(raw)

    moved = 0
    for owner_id, tag_docs in tags_by_owner.items():
        user = User.objects(pk=owner_id).first()
        if not user:
            continue
        existing_names = {t.name.lower() for t in (user.tags or [])}
        for raw in tag_docs:
            if raw["name"].lower() in existing_names:
                continue
            parent_name = None
            if raw.get("parent"):
                parent_name = id_to_name.get(raw["parent"])
            user.tags.append(EmbeddedTag(
                name=raw["name"],
                color=raw.get("color") or "#6c757d",
                is_context=raw.get("is_context", True),
                on_hold=raw.get("on_hold", False),
                parent=parent_name,
                external_id=raw.get("external_id"),
            ))
            moved += 1
        user.save()
    click.echo(f"Migrated {moved} tag(s) into users.")

    # Rewrite tasks: tags field ObjectId list → name list.
    rewritten = 0
    for raw in db.tasks.find({"tags": {"$exists": True, "$ne": []}}):
        new_tags = []
        for tid in raw["tags"]:
            name = id_to_name.get(tid)
            if name:
                new_tags.append(name)
        if new_tags != raw["tags"]:
            db.tasks.update_one(
                {"_id": raw["_id"]},
                {"$set": {"tags": new_tags}},
            )
            rewritten += 1
    click.echo(f"Rewrote tags on {rewritten} task(s).")

    db.tags.drop()
    click.echo("Dropped legacy 'tags' collection.")

@cairn_cli.command("seed-demo")
@click.argument("username")
def seed_demo(username):
    """Create some demo projects/tags/tasks for a user."""
    user = User.objects(username=username).first()
    if not user:
        raise click.ClickException(f"user '{username}' not found")

    user.tags.append(Tag(name="home", color="#198754"))
    user.tags.append(Tag(name="office", color="#0d6efd"))
    user.tags.append(Tag(name="errands", color="#fd7e14"))
    user.save()

    proj = Item(
        owner=user,
        name="Set up Cairn",
        project_type="sequential",
        review_interval_days=7,
    ).save()

    Item(owner=user, kind="task", title="Process inbox", status="open", tags=["home"]).save()
    Item(owner=user, kind="task", title="Define perspectives", parent=proj, status="next", tags=["office"]).save()
    Item(owner=user, kind="task", title="Buy groceries", status="next", tags=["errands"]).save()
    Item(owner=user, kind="task", title="Review weekly goals", status="next", flagged=True).save()

    click.echo("Seeded demo data.")

@cairn_cli.command("export-user")
@click.argument("username")
@click.option("--output", "-o", required=True, help="Path to write the backup JSON to.")
@click.option("--include-trashed/--no-include-trashed", default=True,
              help="Include soft-deleted (trashed) items. On by default.")
def export_user(username, output, include_trashed):
    """Export a user's complete data (profile + all items) to a JSON backup.

    Full export — includes credentials so a restore into a fresh DB is
    faithful. Keep the file safe. The GUI download (/settings/export) is
    the redacted, credential-free counterpart for end users.
    """
    import json
    from application.services.backup import build_export

    user = User.objects(username=username).first()
    if not user:
        raise click.ClickException(f"user '{username}' not found")
    bundle = build_export(user, include_trashed=include_trashed, redact_secrets=False)
    with open(output, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2)
    click.echo(f"Exported {username}: {len(bundle['items'])} item(s) → {output}")


@cairn_cli.command("import-user")
@click.argument("backup_file")
@click.option("--as-user", "target_username", default=None,
              help="Import into this existing user, remapping ownership. "
                   "Default: restore to the original user (matched by id/username, "
                   "created if absent).")
@click.option("--replace", is_flag=True,
              help="Delete the target user's existing items first (clean restore "
                   "instead of merge).")
def import_user(backup_file, target_username, replace):
    """Import a backup produced by ``export-user`` (or the GUI download).

    Idempotent for restore-to-original: items upsert by their original id.
    Use --as-user to clone a user's data into another account.
    """
    import json
    from application.services.backup import BackupError, import_bundle

    try:
        with open(backup_file, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"can't read backup: {exc}")
    try:
        stats = import_bundle(payload, replace=replace, target_username=target_username)
    except BackupError as exc:
        raise click.ClickException(str(exc))

    verb = "created" if stats["user_created"] else "updated"
    msg = f"User '{stats['username']}' {verb}; imported {stats['items_imported']} item(s)"
    if stats["items_deleted"]:
        msg += f", deleted {stats['items_deleted']} existing"
    click.echo(msg)


def register_cli(app):
    app.cli.add_command(cairn_cli)

def main():
    """Entry point for the ``./cairn`` wrapper.

    Pushes an app context so the mongoengine models can talk to the DB, then
    dispatches to the same click group used by ``flask cairn``.
    """
    from application import create_app

    app = create_app()
    with app.app_context():
        cairn_cli.main(standalone_mode=True)
