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

@cairn_cli.command("rename-user")
@click.argument("old_username")
@click.argument("new_username")
def rename_user(old_username, new_username):
    """Change a user's username (login is by email, so this is safe).

    Used to repair auto-generated usernames — e.g. names with ß/umlauts that
    predate the transliteration fix (``sven-rue`` → ``sven-ruess``).
    """
    user = User.objects(username=old_username).first()
    if not user:
        raise click.ClickException(f"no user '{old_username}'")
    if User.objects(username=new_username).first():
        raise click.ClickException(f"username '{new_username}' already taken")
    user.username = new_username
    user.save()
    click.echo(f"renamed {old_username} → {user.username} ({user.pk})")

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

@cairn_cli.command("drop-legacy-time-entries")
def drop_legacy_time_entries():
    """Remove the old embedded ``time_entries`` field left on ``items``.

    Time tracking moved to the team billing module (separate ``tt_*``
    collections), so the field was dropped from the ``Item`` model. Under
    mongoengine's strict mode, items that still carry the stale field fail to
    load (FieldDoesNotExist). This one-shot ``$unset`` clears it. Raw pymongo,
    so it bypasses the model and never trips the strict check. Idempotent.
    """
    from mongoengine import get_db
    res = get_db().items.update_many(
        {"time_entries": {"$exists": True}},
        {"$unset": {"time_entries": ""}},
    )
    click.echo(f"cleared time_entries on {res.modified_count} item(s)")


@cairn_cli.command("import-mite")
@click.argument("username")
@click.option("--account", required=True,
              help="mite account name (the 'acme' in acme.mite.de).")
@click.option("--key", required=True, help="mite API key (Mein Konto → API).")
@click.option("--team", "team_id", help="Team to import into (default: the "
                                        "user's tracking default).")
@click.option("--from", "date_from", help="Only entries from this day (YYYY-MM-DD).")
@click.option("--to", "date_to", help="Only entries up to this day (YYYY-MM-DD).")
@click.option("--projects-only", is_flag=True,
              help="Only the current customers/projects — no time entries "
                   "(implies --no-include-archived).")
@click.option("--include-archived/--no-include-archived", default=None,
              help="Also import archived customers/projects "
                   "(default: yes, unless --projects-only).")
@click.option("--dry-run", is_flag=True, help="Report only, don't write.")
def import_mite(username, account, key, team_id, date_from, date_to,
                projects_only, include_archived, dry_run):
    """Import a mite.de account (customers, projects, time entries) into a team.

    Idempotent: everything imported carries its mite id, so re-running updates
    names/rates and appends only entries that aren't there yet — run it once for
    the history and again right before switching over.

    With --projects-only it brings just the structure across, which is the
    normal way to start: set the customers and projects up here, keep logging
    new time in Cairn, and leave the old history in mite. That run is repeatable
    too, and a full import later still fits on top.
    """
    from datetime import date as _date
    from application.services.mite_import import MiteError, import_for_user
    from application.services.timetracking import default_team, user_teams

    user = User.objects(username=username).first()
    if not user:
        raise click.ClickException(f"user '{username}' not found")
    teams = user_teams(user)
    if not teams:
        raise click.ClickException(
            f"'{username}' is in no team — time tracking is team-only")
    if team_id:
        team = next((t for t in teams if str(t.pk) == team_id), None)
        if team is None:
            raise click.ClickException(f"'{username}' is not in team {team_id}")
    else:
        team = default_team(user)

    def _day(raw, label):
        if not raw:
            return None
        try:
            return _date.fromisoformat(raw)
        except ValueError:
            raise click.ClickException(f"--{label} must be YYYY-MM-DD")

    try:
        stats = import_for_user(
            user, account=account, api_key=key, team=team,
            date_from=_day(date_from, "from"), date_to=_day(date_to, "to"),
            with_entries=not projects_only,
            # Unspecified follows the mode: a "current projects" run has no use
            # for archived ones, a full import wants everything.
            include_archived=(not projects_only if include_archived is None
                              else include_archived),
            dry_run=dry_run,
        )
    except MiteError as exc:
        raise click.ClickException(str(exc))

    click.echo(f"team: {team.name}{'  (dry run)' if dry_run else ''}")
    click.echo(f"customers: {stats.customers_created} created, "
               f"{stats.customers_updated} updated")
    click.echo(f"projects:  {stats.projects_created} created, "
               f"{stats.projects_updated} updated"
               f"{'' if stats.include_archived else '  (current only)'}")
    if stats.with_entries:
        click.echo(f"entries:   {stats.entries_imported} imported, "
                   f"{stats.entries_skipped} skipped, {stats.entries_fetched} fetched")
    else:
        click.echo("entries:   none requested (--projects-only)")
    if stats.unmatched_users:
        click.echo("mite users with no Cairn account (their name is kept on the "
                   "entries): " + ", ".join(sorted(set(stats.unmatched_users))))
    for err in stats.errors[:20]:
        click.echo(f"  ! {err}")
    if len(stats.errors) > 20:
        click.echo(f"  … and {len(stats.errors) - 20} more")


@cairn_cli.command("split-time-buckets")
@click.option("--dry-run", is_flag=True, help="Report only, don't write.")
def split_time_buckets(dry_run):
    """Re-file monthly time buckets into the daily buckets the model now uses.

    Time entries used to be bucketed per ``(team, customer, month)``; they are
    now bucketed per ``(team, customer, day, part)`` so a customer's day is one
    document for the whole team, with an automatic overflow part if it ever
    grows too large. This walks every bucket whose ``period`` is still a month
    (``YYYY-MM``), redistributes its entries into day buckets, and deletes the
    old document. Entries keep their identity (``entry_id``) and all snapshots,
    so nothing about billing, locks or reports changes. Idempotent — buckets
    that already carry a day period are skipped.
    """
    from collections import defaultdict
    from application.models.timetracking import (
        MAX_ENTRIES_PER_BUCKET, TimeBucket,
    )
    from application.services.timetracking import local_date
    moved = removed = created = 0
    for bucket in TimeBucket.objects(period__not__regex=r"^\d{4}-\d{2}-\d{2}$"):
        by_day = defaultdict(list)
        for e in bucket.entries:
            by_day[local_date(e.spent_on).isoformat()].append(e)
        click.echo(f"{bucket.customer} {bucket.period}: "
                   f"{len(bucket.entries)} entr(ies) → {len(by_day)} day(s)")
        if dry_run:
            continue
        seen_parts = set()
        for day, entries in by_day.items():
            # One push per entry — the same atomic append a live write uses, so
            # a re-filed month ends up shaped exactly like organically written
            # data (including the overflow parts on a very busy day).
            for i, e in enumerate(entries):
                part = i // MAX_ENTRIES_PER_BUCKET + 1
                TimeBucket.objects(team=bucket.team, customer=bucket.customer,
                                   period=day, part=part).update_one(
                    upsert=True,
                    push__entries=e,
                    inc__total_minutes=(e.minutes or 0) if e.status == "active" else 0,
                    inc__entry_count=1,
                    set__updated_at=bucket.updated_at,
                    set_on_insert__created_at=bucket.created_at,
                )
                seen_parts.add((day, part))
                moved += 1
        created += len(seen_parts)
        bucket.delete()
        removed += 1
    click.echo(f"{'would move' if dry_run else 'moved'} {moved} entr(ies) into "
               f"{created} day bucket(s); removed {removed} monthly bucket(s)")


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
    """Promote / demote a user. Auto-creates a team when needed.

    This grants an operator *comp* plan (no Stripe charge) so the level
    survives the nightly ``reconcile-subscriptions`` sweep — use it for
    friends / grandfathered / complimentary accounts. To put someone back on
    paid-only billing, set them to ``free`` (clears the comp) and let them
    subscribe via /billing.
    """
    user = (
        User.objects(email=email_or_username).first()
        or User.objects(username=email_or_username).first()
    )
    if not user:
        raise click.ClickException(f"no user '{email_or_username}'")
    # Mirror the level onto a comp plan so billing treats it as entitled.
    user.comp_plan = {"premium": "premium", "team_admin": "team"}.get(level, "")
    user.level = level
    if level == "team_admin" and not user.team:
        from application.models.team import Team
        team = Team(name=f"{user.display_name or user.username}'s team",
                    admin=user).save()
        user.team = team
        click.echo(f"created team {team.name} ({team.pk})")
    user.save()
    click.echo(f"{user.username} is now {level}"
               + (f" (comp: {user.comp_plan})" if user.comp_plan else ""))

@cairn_cli.command("reconcile-subscriptions")
def reconcile_subscriptions():
    """Expire trials, downgrade lapsed subs, dissolve admin-less teams.

    Cron-friendly backstop for missed/late Stripe webhooks — run nightly.
    Idempotent: in steady state webhooks keep everything in sync and this
    reports zero changes.
    """
    from application.services.billing import reconcile_all
    counters = reconcile_all()
    click.echo(
        f"reconciled {counters['users']} user(s): "
        f"{counters['downgraded']} level change(s), "
        f"{counters['teams_dissolved']} team(s) dissolved"
    )


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
@click.option("--read-only", is_flag=True, help="Token may only make GET requests.")
def issue_token(username, name, read_only):
    """Issue an API token for a user."""
    user = User.objects(username=username).first()
    if not user:
        raise click.ClickException(f"user '{username}' not found")
    _tok, raw = user.issue_api_token(name, read_only=read_only)
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


@cairn_cli.command("zammad-diagnose")
@click.argument("username")
@click.argument("ticket")
def zammad_diagnose(username, ticket):
    """Explain why a Zammad TICKET (number or id) isn't syncing for USERNAME.

    Read-only. Prints whether the ticket is fetched by the sync, its live
    Zammad tags, the Cairn task's stored tags, the user's status-tag set,
    and exactly what the next sync would do to the tags.
    """
    from application.models.item import Item
    from application.services.crypto import decrypt_for_user
    from application.services.external_tags import overwrite_group_tags
    from application.services import zammad_import as Z

    user = User.objects(username=username).first()
    if not user:
        raise click.ClickException(f"user '{username}' not found")
    if not user.zammad_url or not user.zammad_token:
        raise click.ClickException("Zammad URL/token not configured for this user")
    token = decrypt_for_user(user, user.zammad_token)
    base = user.zammad_url.rstrip("/")
    if not base.endswith("/api/v1"):
        base += "/api/v1"

    def req(path, **params):
        return Z._request(base + path, token, params=params or None)

    me = req("/users/me")
    me_id = me.get("id") if isinstance(me, dict) else None
    click.echo(f"token holder: {me.get('login')} (id={me_id})")

    # Resolve ticket by number, then by id.
    t = None
    res = req("/tickets/search", query=f"number:{ticket}", expand="true", per_page=5)
    rows = res if isinstance(res, list) else []
    if isinstance(res, dict) and "tickets" in res:
        ids = res.get("tickets") or []
        assets = (res.get("assets") or {}).get("Ticket") or {}
        rows = [assets[str(i)] for i in ids if str(i) in assets]
    if rows:
        t = rows[0]
    else:
        try:
            t = req(f"/tickets/{ticket}", expand="true")
        except Z.ZammadError:
            t = None
    if not t:
        raise click.ClickException(f"ticket '{ticket}' not found in Zammad")

    zid = t.get("id")
    click.echo(f"ticket id={zid} number={t.get('number')} state={t.get('state')} "
               f"owner_id={t.get('owner_id')} customer_id={t.get('customer_id')}")

    # Is it returned by the owner search the sync uses?
    in_search = False
    page = 1
    while True:
        r = req("/tickets/search", query=f"owner_id:{me_id}", expand="true",
                per_page=200, page=page)
        ch = r if isinstance(r, list) else []
        if isinstance(r, dict) and "tickets" in r:
            ch = r.get("tickets") or []
            ch = [{"id": i} for i in ch]
        if not ch:
            break
        if any((row.get("id") if isinstance(row, dict) else row) == zid for row in ch):
            in_search = True
            break
        page += 1
        if page > 50:
            break
    click.echo(f"fetched by sync (owner_id:{me_id} search)? {in_search}")
    if not in_search:
        click.echo("  → NOT fetched: the sync never sees this ticket, so nothing "
                   "updates. Likely you're not the Zammad owner (only "
                   "owner-of-record tickets are pulled).")

    live = Z._ticket_tags(base, token, str(zid))
    click.echo(f"live Zammad tags: {live}")

    task = Item.all_objects(owner=user, external_id=f"zammad:{zid}").first()
    if not task:
        click.echo("Cairn task: none yet (would be created on next sync).")
        return
    click.echo(f"Cairn task: status={task.status} trashed={bool(task.trashed_at)}")
    click.echo(f"  tags         = {task.tags}")
    click.echo(f"  external_tags= {task.external_tags}")

    zammad_owned = {tg.name for tg in (user.tags or [])
                    if tg.parent == "Zammad" or tg.name == "Zammad"}
    # A tag is owned (and thus overwritten) if it's under the Zammad group OR
    # it was in this task's previous external_tags (= Zammad's own last-set
    # tags). Mirrors the sync in zammad_import so this prediction is accurate.
    owned = zammad_owned | set(task.external_tags or [])
    click.echo(f"Zammad-group tags: {sorted(zammad_owned)}")
    click.echo(f"owned this task (group ∪ prior external_tags): {sorted(owned)}")
    would = overwrite_group_tags(task.tags, owned, live)
    click.echo(f"next sync would set tags = {would}")
    if set(would) == set(task.tags or []):
        click.echo("  → no change expected. If the live tags above are already "
                   "right, the ticket simply isn't being re-synced (deploy / "
                   "filter / ownership). If a stale tag persists and it's NOT in "
                   "'owned' above, the sync never recorded it as Zammad's (older "
                   "import / pre-existing user tag) — use reconcile-zammad-tags, "
                   "or remove it once by hand.")
    else:
        click.echo("  → a sync SHOULD update this task. If it didn't, the running "
                   "build is older than this code — redeploy.")


@cairn_cli.command("reconcile-zammad-tags")
@click.option("--user", "username", default=None,
              help="Limit to one user; omit to run for every user with Zammad set up.")
@click.option("--dry-run", is_flag=True, help="Report only, don't write.")
def reconcile_zammad_tags_cmd(username, dry_run):
    """Hard-reset every Zammad task's tags to the live Zammad set.

    One-off repair for stale status tags (e.g. "Warte auf Kunde" left over
    after the ticket moved to "Warte auf Antwort") on tasks imported before
    external-tag tracking. Zammad becomes authoritative — manually added
    tags on Zammad tasks are dropped.
    """
    from application.services.crypto import decrypt_for_user
    from application.services.zammad_import import reconcile_tags

    users = User.objects(username=username) if username else User.objects(zammad_url__nin=[None, ""])
    total_changed = 0
    for user in users:
        if not user.zammad_url or not user.zammad_token:
            if username:
                raise click.ClickException(f"{user.username}: Zammad URL/token not configured")
            continue
        token = decrypt_for_user(user, user.zammad_token)
        if not token:
            click.echo(f"{user.username}: saved token couldn't be decrypted — skipped")
            continue
        res = reconcile_tags(user, user.zammad_url, token, dry_run=dry_run)
        total_changed += res["changed"]
        verb = "would update" if dry_run else "updated"
        click.echo(f"{user.username}: {verb} {res['changed']}/{res['scanned']} Zammad task(s)")
    click.echo(f"{'(dry-run) ' if dry_run else ''}total: {total_changed} task(s)")


@cairn_cli.command("sync-zammad-all")
@click.option("--all-configured", is_flag=True,
              help="Sync everyone with Zammad set up, ignoring the auto-sync opt-in.")
@click.option("--concurrency", default=8, show_default=True,
              help="How many users to sync in parallel (I/O-bound, threads).")
@click.option("--force", is_flag=True,
              help="Ignore the per-user interval; sync even if recently synced.")
def sync_zammad_all_cmd(all_configured, concurrency, force):
    """Sync Zammad for all opted-in (or all configured) users, in parallel.

    This is the no-broker fallback / local-test equivalent of the Celery
    beat+worker setup — it shares the same core (application.services
    .zammad_auto), so behaviour matches the background workers.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from flask import current_app
    from application.services import zammad_auto
    from application.services.zammad_import import ZammadError

    interval = int(current_app.config.get("ZAMMAD_SYNC_INTERVAL_MIN", 5))
    ids = zammad_auto.due_user_ids(interval, force=force,
                                   all_configured=all_configured)
    if not ids:
        click.echo("No users due for a Zammad sync.")
        return
    click.echo(f"Syncing {len(ids)} user(s) with concurrency {concurrency}…")

    def _run(uid):
        try:
            return uid, zammad_auto.sync_user(uid), None
        except ZammadError as exc:
            return uid, None, str(exc)

    ok = failed = 0
    with ThreadPoolExecutor(max_workers=max(concurrency, 1)) as pool:
        futures = [pool.submit(_run, uid) for uid in ids]
        for fut in as_completed(futures):
            uid, stats, err = fut.result()
            if err:
                failed += 1
                click.echo(f"  {uid}: ERROR {err}")
            elif stats is None:
                click.echo(f"  {uid}: skipped (not configured)")
            else:
                ok += 1
                click.echo(f"  {uid}: created {stats.tickets_created}, "
                           f"updated {stats.tickets_updated}, "
                           f"errors {len(stats.errors)}")
    click.echo(f"Done — {ok} ok, {failed} failed.")


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
