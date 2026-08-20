# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Collaboration principles

1. Don't assume. Don't hide confusion. Surface tradeoffs.
2. Minimum code that solves the problem. Nothing speculative.
3. Touch only what you must. Clean up only your own mess.
4. Define success criteria. Loop until verified.
5. Simple is better than complex, readability counts.

## Brand vs. internals

The user-facing product is **Cairn** (page titles, CLI name, navbar). The repo
directory, env-var prefix, MongoDB database, and Docker Compose project name all
remain `bktodo` for backward-compatibility — **do not rename these**. Changing
the compose project name silently swaps the named volume (`bktodo_mongo-data`)
and loses the user's data. Containers are intentionally named `bktodo-app-1` /
`bktodo-mongo-1`.

`./cairn` is the canonical CLI wrapper; `./bktodo` is a symlink to it.

## Common commands

### Docker dev loop (preferred — `./helper` auto-detects docker/podman)

```sh
./helper up                                 # build + start mongo + app (foreground, hot reload)
./helper down                               # stop + remove
./helper logs                               # tail app logs
./helper shell                              # sh inside app container
./helper cairn <subcommand>                 # run any cairn CLI subcommand inside the container
./helper create_user alice alice@example.com
./helper issue_token alice                  # mint a REST API token
./helper seed_demo alice                    # populate with sample projects/tags/tasks
```

`./helper up` always layers `docker-compose.yml` + `docker-compose.local.yml`
(volume-mount + `flask --debug run`), so source edits reload immediately.

### Local (venv) dev loop

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
docker run -d --name cairn-mongo -p 27017:27017 mongo:7
export BKTODO_SECRET_KEY=dev-secret
flask --app wsgi:app cairn create-user alice alice@example.com
flask --app wsgi:app cairn seed-demo alice
flask --app wsgi:app --debug run --port 5004
```

### CLI subcommands

`./cairn <cmd>` and `flask --app wsgi:app cairn <cmd>` are equivalent. Notable
commands beyond the user-facing ones in the README:

- `./cairn set-level <user> {free|premium|team_admin|global_admin}` — promote/demote; auto-creates a team for `team_admin`
- `./cairn reset-password <user>` — issues a one-time reset URL (useful when SMTP isn't configured)
- `./cairn import-todoist <user> --token <todoist-token>` — full import of projects/labels/tasks/comments
- `./cairn import-mite <user> --account <acct> --key <api-key> [--team <id>] [--from/--to] [--projects-only] [--dry-run]` — migrate a mite.de account (customers, billing projects with rates/budgets, full time history) into a team. `--projects-only` takes the current structure without any time. Idempotent via the stored mite ids, so runs can be repeated or widened
- `./cairn split-note-times [--user <name>] [--dry-run]` — lift `(10:00 bis 12:00)` out of imported mite notes into `TimeEntry.start_time`/`end_time` (the importer does this now; this fixes what came in before)
- `./cairn split-time-buckets [--dry-run]` — one-shot: re-file legacy monthly time buckets into the per-day buckets the model now uses
- `./cairn cleanup-completed-tasks [--dry-run] [--user <name>]` — cron-friendly sweep of done/dropped tasks past each user's retention window
- `./cairn cleanup-trash [--dry-run] [--user <name>]` — cron-friendly purge of trashed items past each user's `trash_retention_days` (default 30; `0` = keep until emptied)
- `./cairn sync-zammad-all [--all-configured] [--force] [--concurrency N]` — sync Zammad for all opted-in users in a thread pool. No-broker fallback / local-test equivalent of the Celery worker+beat (shares `application/services/zammad_auto.py`)
- `./cairn migrate-tags-to-embedded` / `./cairn wipe-legacy-collections` — one-shot migrations from the older schema (see Architecture)

### Tests

The `tests/` directory is currently empty — there is no test suite. `TestConfig`
exists (`MONGO_DB=bktodo_test`, CSRF disabled) and is ready to wire up.

## Architecture

### Stack

Flask 3 + mongoengine + Flask-Login + Flask-WTF (CSRF) + Jinja2/Bootstrap 5.
Same lineage as [cmdbsyncer](https://github.com/Bastian-Kuhn/cmdbsyncer) —
expect the same app-factory pattern, blueprint layout, and helper-script
conventions.

### App factory

`application/__init__.py::create_app` is the single entry point. It:

1. Loads `.env` via `python-dotenv` (does NOT override real env vars).
2. Reads config from `BKTODO_CONFIG` (default `application.config.DevConfig`)
   then overlays `BKTODO_*` env vars via `app.config.from_prefixed_env("BKTODO")`
   — so e.g. `BKTODO_MONGO_HOST` lands as `app.config["MONGO_HOST"]`.
3. Initializes a single mongoengine connection (`alias="default"`, `tz_aware=True`).
4. Registers all blueprints (auth, dashboard, tasks, projects, tags, review,
   settings, tree, perspectives, admin, teams, public_share, api_v1) and
   exempts `api_v1_bp` from CSRF.
5. Registers the `cairn` CLI group and Jinja filters.

`wsgi.py` simply calls `create_app()` — gunicorn (prod) and `flask run` (dev)
both go through the same factory.

### Unified `Item` model — read before touching tasks/projects

`application/models/item.py` defines a single `Item` document that
**replaces** the historical separate `Task` and `Project` models. The
discriminator is `kind ∈ {task, project, section, folder}`, and hierarchy is
expressed via the self-referencing `parent` field. Valid parent combinations
are documented in the module docstring.

Implications when writing queries or features:

- Tasks and projects live in the **same** `items` collection. Always filter by
  `kind`; never assume a query returns only tasks.
- There are no `reverse_delete_rule`s on `parent` — cascading deletes are
  intentionally **not** declarative (would N+1 at scale). Use
  `application/services/cleanup.py` for bulk-delete paths.
- The legacy `tasks` and `projects` collections may still exist in older
  databases but are inert. `./cairn wipe-legacy-collections` drops them.
- **Soft delete / trash:** deleting from the UI sets `Item.trashed_at` instead
  of dropping the doc. `Item.objects` is overridden (mongoengine
  `queryset_manager`) to hide trashed items everywhere by default; use
  `Item.all_objects` for trash/restore/purge/cascade paths that must see them.
  Reference dereferences (`parent`/`owner`/`assigned_to`) bypass the manager, so
  a trashed parent still resolves. Purge logic lives in
  `application/services/trash.py` (`./cairn cleanup-trash`).

### Background workers (Celery + Redis)

Distributed Zammad auto-sync runs on Celery, NOT in the web process.
`application/celery_app.py` builds the Celery app (it calls `create_app()`
so every task runs inside a Flask app context with the mongoengine
connection). Two `@shared_task`s in `application/services/sync_tasks.py`:
`zammad.enqueue_due` (Celery beat fires it every minute → fans out per due
user) and `zammad.sync_user` (one user; horizontally scalable). A per-user
Redis lock prevents double-runs. The broker-agnostic core is
`application/services/zammad_auto.py` (`due_user_ids` + `sync_user`), shared
by the tasks AND the `sync-zammad-all` CLI fallback — so swapping the broker
later only touches the wrapper. Compose adds `redis`, `worker`, and `beat`
services (run exactly one `beat`; scale `worker` with `--scale worker=N`).
Per-user opt-in + replayed import options (project/scope) live on `User`
(`zammad_auto_sync`, `zammad_sync_project`, …).

### Tags are embedded on `User`, not a top-level collection

Tags moved from a central `tags` collection into `User.tags`
(`EmbeddedDocumentListField`). `Item.tags` is a list of **tag names** (strings),
not ObjectIds. `./cairn migrate-tags-to-embedded` handles the data migration
and is idempotent. Don't reintroduce a `Tag` `Document`.

### REST API

`application/api/v1.py` mounts at `/api/v1`. Auth is via `token_required`
(checks `Authorization: Bearer <token>` or `X-API-Token: <token>` against
`ApiToken` docs); the blueprint is CSRF-exempt. Visibility scope is owner OR
`assigned_to == current_user` (`_scope()` helper) — keep that pattern when
adding endpoints, so shared/assigned items remain visible.

### Services layer (`application/services/`)

Non-trivial logic that doesn't belong in a view or model lives here:

- `perspectives.py` / `perspective_filter.py` — GTD perspective queries (Inbox, Today, Next, Forecast, Flagged, Waiting, Someday, Completed)
- `calendar_sync.py` / `calendar_writeback.py` — Google Calendar OAuth + event push (single shared OAuth client per deployment, configured via `BKTODO_GOOGLE_CLIENT_ID`/`_SECRET`)
- `todoist_import.py` — Todoist API ingest used by the CLI command
- `zammad_billing.py` — where Zammad's time lands: customers auto-created from Zammad organisations (`external_id="zammad:org:<id>"`, one `intake` project per customer) and the team-level agent roster (`Team.zammad_agents`) that maps Zammad agents to Cairn accounts; assigning one re-attributes the time already booked
- `cleanup.py` — per-user indexed bulk-delete of completed tasks past retention
- `quick_parse.py` — natural-language task parsing
- `permissions.py` — sharing/visibility checks
- `mail.py` / `crypto.py` — SMTP for password resets, token signing

### Configuration

All settings are overridable via `BKTODO_*` env vars (see `.env.example`).
`application/config.py` has `BaseConfig` / `DevConfig` / `ProdConfig` /
`TestConfig`. Production must supply `BKTODO_SECRET_KEY` — `ProdConfig` leaves
it blank deliberately. `BKTODO_DEFAULT_TZ` (IANA name) is how naive
datetime-local form values are interpreted when pushed to Google Calendar.

### User levels & teams

`User.level ∈ {free, premium, team_admin, global_admin}` gates limits
(e.g. free has `FREE_PROJECT_LIMIT`) and sharing. `team_admin` owns one
`Team` and can invite members. Only `global_admin` can grant levels via
`./cairn set-level`.
