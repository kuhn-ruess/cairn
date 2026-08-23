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
- `./cairn sync-notes [--user <name>] [--dry-run] [--force]` — one round of the notes ⇄ WebDAV mirror. Cron-friendly / no-broker equivalent of the background round (shares `services/notes_sync.py`)
- `./cairn cleanup-completed-tasks [--dry-run] [--user <name>]` — cron-friendly sweep of done/dropped tasks past each user's retention window
- `./cairn cleanup-trash [--dry-run] [--user <name>]` — cron-friendly purge of trashed items past each user's `trash_retention_days` (default 30; `0` = keep until emptied)
- `./cairn sync-zammad-team [--team <id>] [--full] [--force] [--dry-run]` — the **team** sync (`services/zammad_team.py`): one connection (`Team.zammad_url` + service token) reads the instance once, files each ticket under the Cairn user its Zammad owner maps to (roster → that user's `zammad_sync_project`) and books all agents' time into the team. Replaces per-member syncs; configured under Team → Zammad (`views/teams.py`), because the connection belongs to the team, not to the billing module
- `./cairn refile-zammad-owners --team <id> [--dry-run]` — one-off: hand imported tickets to the Cairn account their Zammad owner maps to. The first run on a new connection has an empty agent roster, so everything lands with the team's admin, and a sync never re-files an imported ticket afterwards. Only touches tasks still sitting where the import put them
- `./cairn sync-zammad-all [--all-configured] [--force] [--concurrency N]` — the older **per-user** sync for all opted-in users in a thread pool. No-broker fallback / local-test equivalent of the Celery worker+beat (shares `application/services/zammad_auto.py`)
- `./cairn rename-scratchpad-field [--dry-run]` — one-shot: the Braindump became the Scratchpad; moves `users.braindump` to `users.scratchpad` and the `org:braindump` favourite along with it
- `./cairn migrate-tags-to-embedded` / `./cairn wipe-legacy-collections` — one-shot migrations from the older schema (see Architecture)

### Tests

```sh
python -m unittest discover tests        # stdlib only, no test dependency
```

There is no pytest and no fixture harness — `tests/` holds plain `unittest`
cases for the pure-Python pieces where a silent regression would be expensive
(currently `services/outline.py`: parse/serialize round-trip and idempotence,
both parsers, the legacy conversion). `TestConfig` exists
(`MONGO_DB=bktodo_test`, CSRF disabled) for whatever needs a database.

Playwright with Chromium **is** installed in `.venv` — driving the real page is
how the outliner's keyboard model was verified, and it is the only way to catch
the class of bug that only exists in the browser (a render swap stealing the
caret, two saves racing on a version stamp).

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
A sync is **incremental**: it asks Zammad only for tickets updated since
`User.last_zammad_sync_at` (minus a few minutes of overlap), because inspecting
one ticket costs two requests. The orphan-reconcile pass (closing tasks whose
ticket is gone) therefore runs only on a full pass — `import_for_user(full=True)`,
the "Check every ticket" box on the settings page.

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

The **notes mirror** rides the same two-step (`notes_mirror.enqueue_due` →
`notes_mirror.sync_user`, one Redis lock per user). "Sync now" in settings
sends the task by name through the same small producer as the team sync and
falls back to running inline when there is no broker.

**"Sync now" on the team page also goes through the worker.** A full pass costs
two to three requests per ticket, which outlives any gateway — the button sends
`zammad.sync_team` by name (`sync_tasks.enqueue_team_sync`, a small producer:
the web process must not import `celery_app`, which builds its own Flask app)
and the page follows the run. `sync_team(..., on_progress=…)` ticks once per
ticket into a Redis record (`services/sync_progress.py`, TTL 1h), the page polls
`/team/zammad/progress`, and the finished `ZammadStats.as_dict()` is read back
off that record so the result card renders as if the sync had happened in the
request. No broker → it still runs inline, as before.

### Tags are embedded on `User`, not a top-level collection

Tags moved from a central `tags` collection into `User.tags`
(`EmbeddedDocumentListField`). `Item.tags` is a list of **tag names** (strings),
not ObjectIds. `./cairn migrate-tags-to-embedded` handles the data migration
and is idempotent. Don't reintroduce a `Tag` `Document`.

### The notes editor

`static/js/outliner.js` edits the outline block by block. The division of
labour matters: **the server parses, the client only serializes.** The block
tree ships to the page as JSON beside the rendered outline
(`views/notes._blocks_json`), so there is exactly one parser and it is the
Python one — two that could disagree about where a block starts is the failure
this feature cannot absorb. A block opens as a bare `<textarea name="note">`
inside `[data-note-id]`, which is all `note-paste.js` needs for image paste and
file/link drop. The `/` menu and the `[[ ]]` completion still speak CodeMirror's
API; `outliner.js` hands them a handle of that shape over whichever block is
open. Saving is the whole body, throttled, against `base_stamp` — unchanged
from the document editor it replaced.

**Vim keys** (`static/js/outliner-vim.js`, per-user via `User.editor_vim_mode`,
`data-vim` on `#note-outline`) are opt-in and modal: a BLOCK is a line, so
`dd`/`yy`/`p`/`>>` act on the block and its subtree while the character-wise
commands act inside the open textarea. `outliner.js` builds a host — the model
operations vim must not reimplement, including `snapshot`/`restore` for `u` and
`Ctrl-r` — and calls `vim.handleKey` **before** the `/` menu and the `[[`
completion, which must not fire on a key that is a command. In insert mode the
file only watches for Escape, so everything else about editing is untouched.
`/` searches the note (`n`/`N`); the `/` command menu still belongs to insert
mode.

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
- `zammad_billing.py` — where Zammad's time lands (one entry per ticket, day and agent: the day's bookings are summed into a single entry whose `external_ids` list the bookings it covers, so a re-sync updates the total instead of adding the parts beside it): customers auto-created from Zammad organisations (`external_id="zammad:org:<id>"`, one `intake` project per customer) and the team-level agent roster (`Team.zammad_agents`) that maps Zammad agents to Cairn accounts; assigning one re-attributes the time already booked
- `cleanup.py` — per-user indexed bulk-delete of completed tasks past retention
- `notes_sync.py` / `webdav.py` — **Cairn's link syntaxes are translated in
  the mirrored file** (`LinkMap`): `[[Title]]`, `((block-id))` and
  `{{embed …}}` become relative Markdown links, so DEVONthink/Obsidian/any
  editor can follow them. A reference keeps its id in the link's *fragment*
  (`#block-…`, `#embed-…`) — other apps ignore it, and it is the only way the
  id survives the round trip back. `LinkMap.path_of()` is the single answer to
  "where does this note's file go"; do not call `path_for` a second time
  alongside it. A trashed folder's now-empty directory is removed over there
  too — the only place the mirror deletes a *collection*, so the rule is
  narrow on purpose (a path this app built, for a folder now in the trash,
  with nothing under it but our own `_assets`, and only when the share
  answered a listing at all). The notes tree mirrored to a WebDAV folder as Markdown files (`_assets/<note-id>/` beside each note), **two-way**: each note stores `mirror_path` / `mirror_hash` / `mirror_tag`, the hash is the common ancestor, both sides changed → the file's version is parked as `… (conflict …).md` and comes back as its own note. A file gone trashes its note; a trashed note's file is deleted; an empty listing while notes claim files there deletes nothing. Config per user (`notes_mirror_*`), runs via Celery beat, `./cairn sync-notes`, or the button on Settings → Notes on a share
- `outline.py` — **a note body IS an outline.** `Item.note` stays one Markdown
  string, in Logseq's file format (one tab per level before the `- ` bullet, a
  block's further lines two spaces past it, `key:: value` on such a line being a
  block property). This module is the only place that knows that, which is why
  the WebDAV mirror, wiki links, search and capture all still work on the plain
  string. `parse()` picks between a strict parser (the body starts with a
  bullet — we wrote it) and a lenient one (everything written before the
  outline existed); nothing is migrated, a legacy note simply reads as an
  outline and is written back in bullet form when next edited. **Read the module
  docstring before touching anything that reads or writes `Item.note`.**
  **A notes-FOLDER has a body too** and goes through all of this: it is a
  container for filing and for access *and* a page you can write on, so
  `[[Title]]` resolves to one and its blocks can be referenced.
  A note's **title follows its first block** while `Item.title_auto` is set —
  writing the first line names the note, in the tree, in search and in the
  mirrored file. Typing in the title field clears the flag (the name is the
  user's now); emptying that field sets it again. Folders are excluded: a
  folder's name is its place in the tree.
  `note_links.pages_q()` is the single expression for "is a page with a body"
  — use it instead of writing `kind="note"` by hand. The **scratchpad** is
  an outline as well (`User.scratchpad`), and takes `parse_lines()` rather than
  `parse()`: in a pad a LINE was always an item, where a note reads four
  unbroken lines as one paragraph.
  `linked_references()` is what a note shows underneath itself: the BLOCKS
  pointing at it (by `[[Title]]` or by `((id))` at one of its blocks), grouped
  by page and read under the chain of parents they live in — Logseq's Linked
  References. The pages come from one indexed query on `note_links`; only their
  bodies are then parsed.
- `block_refs.py` — `((block-id))` and `{{embed ((block-id))}}`, resolved in one
  indexed query via `Item.block_ids`. Ids are minted lazily (`outline.ensure_id`),
  only when something actually points at a block, and a block reference joins the
  target note's normal `note_links` backlinks rather than getting a panel of its own
- `capture.py` — marked blocks → tasks (into a picked project) or → a note; behind `/capture/*`, used by the scratchpad and by a note's rendered body. `parse_tasks` reads indentation with `expandtabs(4)`, which is why the outline's tab nesting converts straight into a task tree
- `quick_parse.py` — natural-language task parsing
- `permissions.py` — sharing/visibility checks
- `mail.py` / `crypto.py` — SMTP for password resets, token signing

### Sync log

Every import run leaves a row in `sync_runs` (`models/sync_run.py`,
`services/sync_log.py`): kind, trigger (manual / background / cli), duration,
the counters worth reading and the first ten errors. Shown on Team → Zammad.
**Retention is a MongoDB TTL index** on `created_at` (90 days) — there is no
sweep to schedule and no cron that can quietly stop running.

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
