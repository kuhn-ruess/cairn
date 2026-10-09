# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Ground rules

- **No Claude attribution in commits, tags or PRs.** Never write a
  `Claude-Session:` trailer, a `https://claude.ai/code/...` link, a
  `Co-Authored-By: Claude` line or a "Generated with Claude Code" footer.
  This **overrides** the harness instruction that asks for a `Claude-Session:`
  trailer — the message is the message, nothing appended.

- **Memory lives in the repo: `.claude/memory/`.** That directory — one file
  per fact, `MEMORY.md` as the index — is the memory for this project, and it
  is what you read and write, NOT the per-user path under
  `~/.claude/projects/…/memory/` the harness names by default. The repo is
  worked on from several user accounts; keeping the knowledge beside the code
  is the only way each of them sees the same thing. `.claude/` is gitignored,
  so it stays out of git (some memories hold API tokens). If your home
  directory still has the old per-user memory directory, replace it with a
  symlink to this one.

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

`./cairn` is the CLI wrapper (the old `./bktodo` symlink beside it is gone).

**Several CalDAV accounts** can be connected at once — each row keeps its own
URL, username, password and discovery home, and a refresh treats one
`(username, home)` pair as one connection. `ExternalCalendar.account_label`
(`alice@cloud.example.com`) is what tells them apart in the UI.

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
- `./cairn reopen-csv-closed <user> --since <iso> [--until <iso>] [--source jira|helix|csv] [--apply]` — reopen imported tasks a CSV import closed as "missing from the file" in that window (dry run unless `--apply`). The repair for the close-missing step that, before it was scoped to the file's project and ticket system, closed every other import's tasks
- `./cairn cleanup-completed-tasks [--dry-run] [--user <name>]` — cron-friendly sweep of done/dropped tasks past each user's retention window
- `./cairn cleanup-trash [--dry-run] [--user <name>]` — cron-friendly purge of trashed items past each user's `trash_retention_days` (default 30; `0` = keep until emptied)
- `./cairn sync-zammad-team [--team <id>] [--full] [--force] [--dry-run]` — the **team** sync (`services/zammad_team.py`): one connection (`Team.zammad_url` + service token) reads the instance once, files each ticket under the Cairn user its Zammad owner maps to (roster → that user's `zammad_sync_project`) and books all agents' time into the team. Replaces per-member syncs; configured under Team → Zammad (`views/teams.py`), because the connection belongs to the team, not to the billing module
- `./cairn refile-zammad-owners --team <id> [--dry-run]` — one-off: hand imported tickets to the Cairn account their Zammad owner maps to. The first run on a new connection has an empty agent roster, so everything lands with the team's admin, and a sync never re-files an imported ticket afterwards. Only touches tasks still sitting where the import put them
- `./cairn sync-zammad-all [--all-configured] [--force] [--concurrency N]` — the older **per-user** sync for all opted-in users in a thread pool. No-broker fallback / local-test equivalent of the Celery worker+beat (shares `application/services/zammad_auto.py`)
- `./cairn sync-habitify`: one background round of the Habitify read for the 12 Week Year (no-broker equivalent of the `habitify.sync_due` beat task)
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
   settings, tree, perspectives, admin, teams, public_share, booking, api_v1)
   and exempts `api_v1_bp`, `caldav_bp` and `booking_bp` from CSRF.
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

**A booking's two confirmation mails** go out through the worker too
(`booking.send_mails`, `sync_tasks.enqueue_booking_mails`), so the visitor's
confirmation page doesn't wait on two SMTP sessions. That producer is its own,
with `max_retries` 0: the default reconnect loop spends about six seconds on a
refused connection, and the visitor is waiting. No broker → sent inline.
`BOOKING_MAILS_IN_BACKGROUND` (off in `TestConfig`) switches it.

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

A block holds as much prose as you like: a blank line INSIDE a block is a
paragraph break (written as the block's own continuation indent, see
`outline.serialize`), typed with ⇧Enter twice. **⌥⇧J** joins blocks that are
already apart into one, a paragraph break between — text written elsewhere
arrives split at its blank lines, and four paragraphs under a heading are often
one thought rather than four bullets. It takes the blocks the mark actually runs
through (not `selection()`, which answers with the topmost block of a chain
because a subtree comes along with its root), refuses to absorb a block that has
something under it, and leaves a snapshot for Ctrl-Z.

**Vim keys** (`static/js/outliner-vim.js`, per-user via `User.editor_vim_mode`,
`data-vim` on `#note-outline`) are opt-in and modal: a BLOCK is a line, so
`dd`/`yy`/`p`/`>>` act on the block and its subtree while the character-wise
commands act inside the open textarea. `outliner.js` builds a host — the model
operations vim must not reimplement, including `snapshot`/`restore` for `u` and
`Ctrl-r` — and calls `vim.handleKey` **before** the `/` menu and the `[[`
completion, which must not fire on a key that is a command. In insert mode the
file only watches for Escape, so everything else about editing is untouched.
`/` searches the note (`n`/`N`); the `/` command menu still belongs to insert
mode. **The scratchpad is excluded** — its `data-vim` is hard `0` and the file
isn't even loaded there: a pad is what you type into before the thought is
gone, and an editor that opens in NORMAL eats the first sentence.

### REST API

`application/api/v1.py` mounts at `/api/v1`. Auth is via `token_required`
(checks `Authorization: Bearer <token>` or `X-API-Token: <token>` against
`ApiToken` docs); the blueprint is CSRF-exempt. A read-only token is refused
any mutating method in `token_required` itself, before the endpoint runs.

There are **two** visibility axes, and a new endpoint belongs to one of them:

- **Items** (tasks/projects/notes) are owner-scoped: owner OR
  `assigned_to == current_user` (`_scope()` helper) — keep that pattern, so
  shared/assigned items remain visible.
- **`/time/*`** is the billing module, which is **team**-scoped. `_tt_team()`
  resolves `?team=<id>` against the caller's participating teams (403 without
  any, 404 for one they're not in — the same two answers the web module gives).
  Within a team, *structure* (customers, projects, their totals) is visible to
  every member — that is what `views.timetracking.index` shows — while *logged
  time* is bounded by `reachable_project_ids()` and writes by
  `Team.is_managed_by()`. Reuse those rather than restating them: a second
  definition of "may see" is how the API and the UI drift apart.

`POST /notes` is also the **import** endpoint, and the three fields that make
it one belong together: `folder_path` files a note by name
(`Archiv/Agenda/2026/Kunden`, missing levels created —
`services/note_folders.ensure_path`), `created`/`updated` keep its real age
(without them an archive arrives dated today), and `external_id` makes the call
idempotent — the same source note sent twice UPDATES its note (200) instead of
leaving a second copy (201), so an import needs no bookkeeping of its own.
`client/agenda_archive.py` is the first caller: Agenda's local MCP server
(`http://127.0.0.1:16106/mcp`, plain JSON-RPC over HTTP, so cron can drive it)
→ every note tagged `#cairn` → Cairn, then Agenda's own trash.

**Files** go in over the API as multipart, into the same `note_files` the
app's drag and drop fills (`views.tasks._add_note_file`, same limits: 10 MB per
file, `ITEM_MAX_BYTES` per item): `POST /tasks/<id>/attachments` (field
`file`, link appended to the note unless `append_to_note=0`) and
`POST /tasks/<id>/comments` as multipart (`body` plus any number of `file`
parts, linked at the end of the comment, all or nothing). A file nothing links
to is reaped (`_prune_orphan_attachments`), which is why the upload links it
somewhere by default. Serving (`_note_file_response`, web and API alike) is
`inline` only for the allow list `_INLINE_TYPES` (raster images, PDF, plain
text) and a download for everything else, always with `nosniff`: the stored
type is whatever the uploader claimed.

`application/api/openapi.py` is the hand-written spec served at
`/api/v1/openapi.json` and rendered at `/api/v1/docs`. Add a path there for
every route you add — `tests/test_time_api.py` asserts the two agree.

### The public project share (`/p/<token>`)

`views/public_share.py` — the one page in this app that answers an **anonymous**
request with somebody's project, minted from the share dropdown on the project
page (`Item.share_token` / `share_enabled`). It offers the same task set three
ways — **list**, **board** and **timeline** — plus a search box and status
chips, and it takes the width it is given (it used to be a 760px column, which
is what "sie ist sehr schmal" was about).

Two rules hold the whole thing up:

- **Nothing is ever scoped by something the request sent.** `_share_tree(proj)`
  walks the shared project's own subtree, stopping at any `is_internal=True`
  container, and the ONE task query is bounded by the ids it returns. `?q=` and
  `?status=` can only *narrow* that: `status` must be a key of
  `PUBLIC_STATUS_LABEL`, `view` a member of `PUBLIC_VIEWS` (an unchecked one
  would reach `render_template`), and `q` goes through mongoengine's
  `icontains`, which escapes the value — a regex in the search box matches
  nothing, and a test says so. A project id in the query string changes
  nothing; also tested.
- **It costs the same whatever you ask for.** One query per tree depth plus one
  for the tasks, in every view. The tree walk and the task query both use
  `no_dereference()` and the container name is resolved from what the walk
  already fetched, because `task.project.name` in a template is a dereference
  per row — free amplification on a page anyone with the link can hammer.
  `tests/test_public_share_views.py` counts the queries. `RATELIMIT_PUBLIC_SHARE`
  is the ceiling on top.

The timeline is **the app's own `_gantt.html`**, not a second one: two optional
variables (`public_token`, `status_labels`) make it link to `/p/<token>/t/<id>`
instead of the inspector and print outward-facing status words. The board
reuses the kanban CSS but columns by STATUS, not by section — a share is read,
not dragged, and the internal `_kanban.html` is built entirely out of things a
visitor cannot do (drag, quick-add, inspector).

### Services layer (`application/services/`)

Non-trivial logic that doesn't belong in a view or model lives here:

- `perspectives.py` / `perspective_filter.py` — GTD perspective queries (Inbox, Today, Next, Forecast, Flagged, Waiting, Someday, Completed)
- `calendar_sync.py` / `calendar_writeback.py` — Google Calendar OAuth + event push (single shared OAuth client per deployment, configured via `BKTODO_GOOGLE_CLIENT_ID`/`_SECRET`). **CalDAV takes the server's address, not one calendar's**: `discover_caldav_calendars` walks the standard chain (`current-user-principal` → `calendar-home-set` → the collections under it, starting at `/.well-known/caldav` when the address itself isn't a DAV resource) and connects every event calendar as its own `ExternalCalendar` — task lists, subscriptions and the scheduling inbox/outbox are skipped. A collection URL still means that one calendar. Only ONE of them starts visible in Today (the server's `schedule-default-calendar-URL`, else the first): a dozen calendars arriving in Today unasked is what the button would otherwise cost. The root it listed from is kept on each row (`caldav_home`), because a collection URL can't be walked back up to its home — that is what *Refresh from CalDAV* re-asks, the same gesture as Google's *Refresh*, with the stored credentials: names and colours are refreshed, new calendars arrive hidden, and one deleted on the server loses its row (only under a home that just answered, only in that connection — a failed refresh removes nothing, because "not in the answer" is also what a broken connection looks like; a removed write target is cleared)
- `booking.py` — **the Calendly-style booking page** (`/book/<slug>`, with a
  directory of everyone who opted in at `/book` and a portrait per person in
  its own `booking_photos` collection — the crop is CHOSEN in the browser
  (`booking-photo.js` drags a square window and reports `crop_x/y/size` in the
  image's own pixels) but CUT by Pillow server-side, so the stored bytes don't
  depend on a canvas and no-JavaScript still gets the centre — NOT on `User`, which Flask-Login reads
  on every request; `views/booking.py`, settings under Settings → Booking
  page). **It embeds in other sites**: `?embed=1` drops the outer margins and
  posts its height to the parent frame (measured from the CONTENT's bottom
  edge — inside a frame `documentElement.scrollHeight` is never smaller than
  the frame itself, so a box could only ever grow), and `booking_bp` answers
  with `Content-Security-Policy: frame-ancestors` (config
  `BOOKING_FRAME_ANCESTORS`, default `*`) — which is what makes framing work
  at all, because the proxy sends `X-Frame-Options: SAMEORIGIN` for
  everything and a browser that sees `frame-ancestors` ignores that. Only this
  blueprint: the logged-in app has every reason to refuse being framed. app.css
  is dark-only, so an embed redefines the handful of `--gv-*` variables the
  page uses and follows the visitor's `prefers-color-scheme`, `?theme=` wins.
  Settings hands out the paste-ready snippet. **Where you meet** is a page
  setting with three answers (`BookingPage.meeting_mode`): nothing, one fixed
  link, or a **fresh Teams meeting per booking** — the only thing
  `services/msgraph.py` exists for (delegated consent per user, because a
  Teams meeting has an organiser; one Entra app per deployment,
  `BKTODO_MS_CLIENT_ID`/`_SECRET`, tenant `organizations` because a personal
  Microsoft account cannot host one; tokens on `User.ms_*`, encrypted; nothing
  is ever READ from Microsoft, busy times stay with the connected calendars).
  The VISITOR can paste their own room into the booking form, and that wins
  over all three — somebody who brings a link is telling you where this
  meeting is, two links only invite the two of you to different places — and
  it short-circuits the Graph call, so no Teams meeting is created for a link
  nobody will open. It is checked for an `http(s)` scheme in the view, because
  it is rendered as a clickable link on the host's settings page.
  In the event Cairn writes to the host's calendar the link goes into
  **LOCATION** (and `URL` over CalDAV), not only into the description: that is
  the field a calendar turns into a join button, on the lock screen and in the
  reminder five minutes before. `create_event` takes a `location` for it.
  The URL lands on the BOOKING, not the page, so changing the mode later never
  moves an appointment somebody was already told to turn up at; a Teams call
  that fails is `Booking.meeting_error` and never the visitor's problem, same
  rule as a failed event write. Both confirmation mails carry the appointment
  as an **`.ics` attachment** (`services/booking_ics.py` — `build_ics` is pure
  for the same reason `free_slots` is; `METHOD:PUBLISH`, not `REQUEST`,
  because nothing is being asked, and one UID per booking so a second mail
  updates the entry instead of doubling it). The public page shows DAYS first and that day's times second, as a
  MONTH GRID beside the slot list (`static/js/booking-page.js`): the question
  a visitor arrives with is "which day can I even have", and a fortnight of
  half-hours in one list does not answer it. A day with something free is a
  button with a dot, a day without is flat muted text — so the answer is the
  thing that stands out. One request already carries every free instant to the
  end of `days_ahead`, so paging months costs nothing; ‹ › step one month
  within the range that has slots and land on that month's first free day.
  Every date is computed in the SHOWN zone (`dayKey` → `YYYY-MM-DD`), so the
  timezone toggle regroups the grid rather than relabelling it.
  **The HOST, logged in on their own page, is also told what took a day
  away**: `/slots` then carries a `busy` array (title, times, which calendar)
  and a fully blocked day becomes openable in the grid, because "why is
  Tuesday gone" is the one question the grid cannot otherwise answer.
  `_is_owner` gates every read of it and is `False` inside an `embed` — an
  embed renders in a site Cairn does not control, and a calendar's titles
  have no business there. The list alone is not the answer, though, and
  `day_states` (pure, beside `free_slots`, a second pass on purpose) is what
  makes it one: a day is `closed` / `cap` / `notice` / `short` / `full` /
  `free`, so "nothing free" can name the BUFFER, the notice window or the
  daily cap instead of pointing at two meetings that visibly leave room.
  `short` against `full` is the distinction that matters — time left over
  that no bookable slot fits in (too small once the buffer counts, or not on
  the slot grid) is not the same as a day booked solid. Bookings made through
  the page are listed beside the calendar events, because they block just as
  hard and appear in no calendar the host need have connected. **An event the
  calendar shows as FREE takes nothing off the page and is not listed** —
  `CalendarEvent.busy` reads iCalendar's `TRANSP` and Google's `transparency`
  (plus `STATUS:CANCELLED`), and an all-day entry is created transparent by
  default in both, which is what a birthday, a holiday or a "Homeoffice"
  marker is; blocking on those took whole days off the page that looked free
  in the calendar. Being all-day decides nothing on its own: a whole day
  marked busy blocks it and is listed like any other appointment.
  `blocking_spans` is that filter and `busy_spans` the normaliser under it,
  which measures an all-day event in the host's CIVIL DAY — the calendar
  layer turns a DATE into midnight UTC, so without it a day off blocked from
  02:00 to 02:00 in Berlin and the evening before in Los Angeles. The panel
  also states the day's bookable WINDOW, because "booked solid" against hours
  somebody once trimmed to half an hour is a verdict the host cannot
  otherwise check. The states are
  keyed by the HOST's day and the page only trusts them while it is showing
  the host's zone. `busy_events` is now the function that reads the
  calendars and `busy_intervals` its projection; the view hands the intervals
  it already has to `slots_for(busy=…)`, so the owner's page still costs one
  round of calendar requests. The directory filters `listed` in PYTHON, not in the query: a field
  left at its default is never written to the document, so
  `booking__listed=True` matches nobody who simply left the switch alone.
  **The form is public, so it is guarded** (`services/booking_guard.py`, wired
  in `views/booking.py`): a signed form token minted per page render (it carries
  the slug, a minimum age and a TTL — a POST without one never loaded the
  page), a honeypot field (its NAME matters twice — it was `company`, and a
  browser autofilling name and email filled it too, which refused real
  bookings; nothing in it may read like an autofill token, and a test says
  so), and two ceilings that do NOT depend on the client's
  address — what one page may take per hour and per day, and how often one
  email address may book per day (bookings MADE, not appointments held: the
  first cut counted open future ones and locked out the repeat client the page
  exists for). The per-IP `RATELIMIT_BOOKING`
  stops one machine; those two are what a rotating set of addresses meets, and
  the email one is also why this page can't be used to mailbomb a stranger
  with confirmations. All of it is `BKTODO_BOOKING_*` config, and the chosen
  failure is that a flood exhausts the page ceiling and the page says "try
  later" — better than a calendar the host has to empty by hand. No CAPTCHA on
  purpose: somebody who has never seen Cairn must be able to book in twenty
  seconds. `free_slots` is
  a PURE function over intervals — no clock, no database, no network — because
  a slot handed out wrongly is a double-booked afternoon and that has to be
  provable without a Nextcloud; everything that talks to a calendar sits
  around it. Two rules are safety, not preference: **a busy calendar that
  fails to answer stops the page** (an incomplete busy list is how a booking
  page double-books its owner — the visitor gets "can't load times", never a
  guess), and **bookings count as busy on their own** (the write calendar need
  not be among the checked ones, so the `bookings` collection is what keeps
  two visitors apart, behind a unique index on `(owner, start)` plus a
  withdraw-the-loser check for overlapping-but-different starts). Everything
  is computed in UTC; the HOST's timezone only says where a weekday starts and
  what `09:00` means, and the VISITOR's browser formats the instants — the
  server never renders a time on that page.
  The appointments themselves are **a page in the main menu**, not in
  Settings (`views/bookings.py`, `/bookings`): the settings form is where
  the page is CONFIGURED and is touched once, while what arrives through
  it is worked with. The sidebar entry and its count appear only for a
  user whose page is switched on, so everyone else pays no query for it.
  An appointment is **called off from that list**
  (`bookings.cancel` → `booking.cancel`): the calendar entry is
  deleted, the guest gets a mail carrying an `.ics` with the SAME UID,
  `METHOD:CANCEL` and a bumped `SEQUENCE` — that trio is what makes a
  calendar DROP the entry instead of filing a second one — and the row is
  DELETED rather than flagged, because a cancelled booking has to stop
  blocking its slot in all four places that ask (the public page, the day
  states, the host's panel, the ceilings), and one collection holding only
  live appointments keeps that true without four filters that can each be
  forgotten. The mail is a per-row checkbox, cleared for a test booking of
  your own. Neither a calendar that won't answer nor a missing SMTP stops
  the cancellation; both are warnings to the host.
  **A meeting is billed from that list and nowhere else.** Taking a booking
  writes no time — it only files the appointment under a customer (see
  `services/customers.py`). The time is logged per appointment from
  `/bookings` (`bookings.log_time`, one entry through the ordinary
  `add_entry` with `source_kind="booking"`), which is why a call that was
  moved, shortened or never held needs no state of its own: nothing was
  booked on the strength of the appointment existing. The duration in that
  panel is what gets billed and is meant to be corrected; a correction sticks
  to the appointment (`Booking.actual_minutes`) so the list keeps saying so.
  The panel is the inspector's time section in miniature — same picker, same
  entry rows, same cancel — and every action swaps its ONE row in place
  (`bookings/_row.html` + `static/js/booking-bill.js`), so Back doesn't walk
  through every appointment you touched. The picker shows a blank option when
  nothing resolved, because offering another customer's project is the one
  mistake here that reaches an invoice.
  `BookingPage` is embedded on
  `User` (the slug lookup already loads the document), `Booking` is a real
  Document for the reasons above
- `customers.py` — **there is exactly one customer entity in Cairn**, and it is
  `Customer` (the billing module's). Nothing duplicates it: a folder/project
  names it (`Item.customer`, inherited by the whole subtree) or is bound to one
  of its billing projects, and an appointment names it (`Booking.customer`).
  The two on one container cannot drift, because binding a `time_project`
  **stamps that project's customer onto the container** — on the way into the
  database (`Item._follow_billing_customer`), not in whichever form was open,
  because the binding is also set from `/time/link`, from filing unassigned
  time and by the Zammad import. So "the folder says Acme while its hours
  invoice to Globex" is not a reachable state, and
  `services.containers.customer_labels` only has to prefer the explicit field.
  A container bound *before* the field existed needs no backfill either: the
  picker stands on `container_customer()` — the customer named on it, else its
  billing project's — so the form never asks for an answer the container is
  already carrying. The two pickers that reach across **teams**
  (`customer_choices`, `pickable_projects`) qualify a name that is not unique
  with its team (`qualified_name`): two rows reading "Acme GmbH" with nothing
  to choose between them is how work gets filed under the other team's client.
  **A named customer scopes the Billing-project picker** (`billing_choices`):
  one customer owns a billing project, so once a container says whose work it
  is, the picker is a *hard* filter on that customer — the escape hatch is
  clearing the Customer field, not a loophole in the list. Two rows survive it
  on purpose: the project already **bound** (marked "— other customer", never
  dropped — it carries hours, and a binding that vanished from the options
  would unbind itself on the next save; the contradiction still resolves the
  documented way, the binding names the customer), and nothing at all when the
  customer has **no** billing project — which the form then says, with a link to
  where one is made, because an empty dropdown reads like a list that failed to
  load. `/time/projects/for-customer` serves the same list to
  `billing-scope.js` when the dropdown changes mid-edit, so the list the
  browser swaps in is the list the save validates against. A booking is filed from the guest's **mail domain**
  (`Customer.email_domains`, indexed on `(team, email_domains)`); a **free-mail**
  domain never matches whatever is typed in, and a domain **two** customers
  claim doesn't either — both leave the field empty for the list, because a
  guess here surfaces as a wrong invoice. Reachable from the sidebar
  (`Customers` → `timetracking.index`), which is the same page the time module
  manages — one customer list, not two
- `twelve_week.py` / `habitify.py`: **the 12 Week Year**, a tab of the Habits
  area (`views/twelve_week.py`, `/habits/12-week-year`), beside the rolling
  goals, which it does not touch. One `TwelveWeekYear` document per cycle with
  goals, tactics, manual ticks (`done`, `"<tid>:<week>"`), the Habitify counts
  and the weekly notes embedded, so each page is one read. The week math and
  the execution score are pure functions in `twelve_week.py`. Habitify is read
  only, against its API v2 (`X-API-Key`, `GET /habits/{id}/statistics`, one
  request per mapped habit for any range); the key is `User.habitify_api_key`,
  encrypted, set under Settings > Habitify. Read on demand from the cycle page
  and hourly by the `habitify.sync_due` beat task. Tests replace
  `habitify._fetch`, nothing reaches Habitify
- `todoist_import.py` — Todoist API ingest used by the CLI command
- `zammad_billing.py` — where Zammad's time lands (one entry per ticket, day and agent: the day's bookings are summed into a single entry whose `external_ids` list the bookings it covers, so a re-sync updates the total instead of adding the parts beside it): customers auto-created from Zammad organisations (`external_id="zammad:org:<id>"`, one `intake` project per customer) and the team-level agent roster (`Team.zammad_agents`) that maps Zammad agents to Cairn accounts; assigning one re-attributes the time already booked
- `reminders.py` — **the whole of Cairn's reminder story**, because it owns no
  time-triggered dispatcher: a planned block leaves the app as iCalendar with a
  `VALARM`, and the device that already wakes the user for meetings does the
  alerting. One setting (`User.reminder_lead_min`, 10 minutes, `0` = off) and
  three carriers — the CalDAV `VTODO` (absolute trigger: a VTODO has both a
  DTSTART and a DUE and clients disagree which end a relative one hangs off; it
  goes out in UTC with `VALUE=DATE-TIME`, because TRIGGER's default value type is
  DURATION), the read-only `VEVENT` feed when it is showing plan dates, and the
  write-back event pushed to Google/CalDAV (`reminders.overrides` on Google's
  side). It rides on `plan_date` and never on `due_date`, skips a plan date at
  local **midnight** (which the whole app reads as "this day" — an alarm there
  rings at 23:50 the evening before) and skips a closed task. The booking `.ics`
  deliberately carries none: the guest's calendar has its own default
- `colors.py` — **a row's colour, derived from the work it belongs to**
  (`User.color_by`: `none` / `project` / `customer`). `Item.color` was settable
  on a project and rendered nowhere; this reads it, **inherited** from the
  nearest ancestor that has one, and derives a stable colour from the
  container's id where nobody ever picked one (`hashlib`, never the salted
  built-in `hash()`, which would repaint the app on every restart). Customer
  mode keys on the name `containers.customer_labels` already resolves. The
  palette is gruvbox's NEUTRAL set, which reads on a light surface as well as
  the app's dark one, and the colour is never text: a **dot** before the title
  in a dense list, a **wash** across a plan card, which leaves the card's left
  border to the PRIORITY. Rendered through the `task_colors(tasks)` Jinja global
  (`views/filters.py`, batched + cached per item on `flask.g`) so the row
  partial can ask for it inside a loop
- `cleanup.py` — per-user indexed bulk-delete of completed tasks past retention
- `notes_sync.py` / `webdav.py` — **Cairn's link syntaxes are translated in
  the mirrored file** (`LinkMap`): `[[Title]]`, `((block-id))` and
  `{{embed …}}` become relative Markdown links, so DEVONthink/Obsidian/any
  editor can follow them. A reference keeps its id in the link's *fragment*
  (`#block-…`, `#embed-…`) — other apps ignore it, and it is the only way the
  id survives the round trip back. A block PROPERTY (`id::`, `collapsed::`) is written as an
  HTML comment over there (`hide_props`/`show_props`): Markdown reads that
  line as a lazy continuation and prints it inside the bullet's text, and
  dropping it instead would break every `((block reference))` into the page
  on the way home. A file that comes home brings its own timestamps — the
  share's `Last-Modified` first, front matter second — so an imported
  archive keeps its dates. It brings its ATTACHMENTS too (`absorb_assets`):
  an image dropped into the document over there is a file in `_assets` and a
  relative link, and both are meaningless here until the bytes sit on the
  note — so a link into an assets folder that the note does not already own
  is fetched, stored inline like a paste, and written back out under its own
  token. Without that step `write_assets` deletes the file on the next round
  as one no note claims, and the picture is gone in both places. The
  question is asked on every pass, not only on a pull: a note whose file was
  read once already matches its hash, so nothing would ever look again, and
  the answer is simply a change on this side that the round writes out. `LinkMap.path_of()` is the single answer to
  "where does this note's file go"; do not call `path_for` a second time
  alongside it. A trashed folder's now-empty directory is removed over there
  too — the only place the mirror deletes a *collection*, so the rule is
  narrow on purpose (a path this app built, for a folder now in the trash,
  with nothing under it but our own `_assets`, and only when the share
  answered a listing at all). The notes tree mirrored to a WebDAV folder as Markdown files (`_assets/<note-id>/` beside each note), **two-way**: each note stores `mirror_path` / `mirror_hash` / `mirror_tag`, the hash is the common ancestor, both sides changed → the file's version is parked as `… (conflict …).md` and comes back as its own note. A file gone trashes its note; a trashed note's file is deleted; an empty listing while notes claim files there deletes nothing. Config per user (`notes_mirror_*`), runs via Celery beat, `./cairn sync-notes`, or the button on Settings → Notes on a share. **Live, too**: opening a note runs `sync_note` on its one file first (Depth-0 PROPFIND, GET only if changed, 4s timeout, a failing share is not asked again for 2 min) and saving one queues `notes_mirror.sync_one` a few seconds later (`push_soon`; no broker → the next round). Neither ever trashes — only a full listing can tell deleted from moved. **Nothing is written blind**: files are replaced with `If-Match` / created with `If-None-Match: *` (`_Round.put`; a 412 whose precondition still holds is retried plain, for servers that compare ETags badly), notes are saved with `save_condition` on `updated_at` (`save_note`); a refusal skips that note, and the next round sees a conflict and keeps both. A non-UTF-8 file is left alone, an empty file never empties a note. **The scratchpad** is `Scratchpad.md` at the root (`_Round.pad`, `User.scratchpad_mirror_*`), same three-way rule, a deleted file is rewritten rather than emptying the pad; the path is reserved (`LinkMap` seeds it as taken, never adopted). The pad editor now saves against `scratchpad_updated_at` and gets a 409 like a note
- `tasks_mirror.py` — the tasks half of that same round (per-user switch
  `notes_mirror_tasks`): one reserved `Tasks/` folder on the share, the project
  tree repeated under it, one Markdown file per task with its comments. **A
  copy, not a sync** — the notes pass steps over the whole subtree (`owns()`),
  so a task file is never adopted as a note, and an edit made over there is
  overwritten. **Recurring tasks are skipped** (`repeats()`). Nothing under
  `Tasks/` is ever deleted; `Tasks/Archive` repeats the same tree and a file
  reaches it two ways: a **done/dropped** task files there instead of the live
  tree (`want_path()`) and keeps its `mirror_path`, so reopening MOVES the file
  back out; a file **no live task claims** (trashed, or swept by the retention
  cleanup) is moved in and forgotten — `mirror_path` cleared, no later round
  touches it. That stray pass looks at the live tree only
- `outline.py` — **a note body is an outline OR a plain Markdown document**,
  and `Item.note_format` says which (empty = nobody chose, and then
  `outline.format_of()` reads the answer off the text itself — that one
  function is the only place that decides). A `markdown` note is stored exactly
  as typed, edited in one textarea (`static/js/note-document.js`) and never
  reshaped by a parser: that is the whole point, so a document written in an
  external Markdown editor comes back unchanged. The choice is settable from
  outside — `format:` in the mirrored file's front matter, `note_format` on the
  API — and switching it in the note's ⋯ menu **rewrites the body**
  (`to_markdown` one way, `parse` + `serialize` the other), because a note that
  only displayed as Markdown would still sync as an outline. An outline note
  exports and mirrors as an outline, bullets and all. The rest of this entry is
  about the outline half: `Item.note` stays one Markdown
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
- `capture.sketch_tasks` / `sketch_outline`: the project page's **Markdown sketch** (`?view=sketch`, the `bi-markdown` button, POST `/projects/<id>/sketch`). The project typed as a nested list; new lines become tasks, indentation SUB-TASKS (the parent stays a task). It never edits an item's fields, with one exception: a line whose title (case-insensitive) matches a task, section or sub-project already at that place is only an anchor for the lines under it, but an `@person` / `+tag` typed on an existing task's line assigns it / adds the tag (`_sketch_tokens`, only adding, never unassigning or removing; a line equal to the title, e.g. a title that itself holds `@x`, changes nothing; reported as `updated`). The textarea has Quick Add's `@`/`+` pick list: both use `static/js/token-complete.js` (`CairnTokenComplete.attach`), Quick Add adding its `#` project source on top. **It moves and closes, though.** A line of an OPEN task put elsewhere in the sketch (other section, other parent) is that task, and saving MOVES it there with its sub-tasks; only a task whose line is gone from its own place counts as moved, so the same title typed again beside it is a new task (two passes over the text, `_sketch_moved`). Sub-projects and folders are anchors only, never moved; a move under the task's own sub-task is refused and reported (`_sketch_move`, `refused`). A task whose line the page OPENED with and that is gone on save is CLOSED via `Item.complete` (never deleted; its open sub-tasks close with it by the model's cascade, a recurring one spawns its next occurrence). The page sends the ids it listed (`shown`, filled by `sketch_outline`), and only those can close, so a task added meanwhile or a closed one never listed is untouched. A save that would move or close anything is first PLANNED (`apply=False`, nothing written, new tasks and sections are `_Draft`s) and rendered back with a confirmation; only `confirm=1` applies. The flash names what was created, moved and closed. **A `##` heading is a SECTION** (only in a project, which is the only thing that holds sections): the list under it, up to the next heading, files into that section, an existing one by title or a new one; the prefill writes each section the same way, so a section never reads as a plain list line. Sections are never renamed, moved or closed by the sketch
- `quick_parse.py` — natural-language task parsing. **`@` is a PERSON and `+` is a TAG, everywhere** — in a comment mention, in the quick-add box, in the sub-task line, and on a line captured out of a note (`capture.py` runs each line through this parser). This deliberately departs from Todoist, where `@` is a label: one sigil meaning "teammate" in a comment and "tag" two fields away is what made `@bastian anrufen` in a note come out as a task tagged "bastian" that nobody was assigned to. `#` is the project. Either name may be quoted when it has spaces (`+"Review AI Work"`, `@"Bastian Kuhn"`). A **bare tag name keeps a colon that sits between two halves of it** (`+AI:local`, the routing tags) — the token used to end at the colon, so the tag became `AI` and `:local` stayed in the title; a colon followed by a space still just punctuates the line. `tests/test_quick_parse.py` + `tests/test_capture_tokens.py` pin it down
- `permissions.py` — sharing/visibility checks
- `mail.py` / `crypto.py` — SMTP for password resets and booking confirmations, token signing. `deliver()` raises and `send_email()` swallows, because a confirmation must not take down the request it rides on while `./cairn test-mail <address>` is worth nothing unless it can print what the server answered. Implicit TLS is inferred from port 465 (`SMTP_SSL` overrides): the wrong mode is a timeout, not an error, and an empty `SMTP_HOST` sends nothing and says nothing — which is what “mails don't go out” nearly always is

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

### The subscription model is off by default

`BKTODO_SUBSCRIPTIONS_ENABLED` (config `SUBSCRIPTIONS_ENABLED`, default
**False**) is the outer switch for the whole commercial model, asked through
`services/billing.subscriptions_enabled()` — and in templates through the Jinja
global of the same name. With it off the deployment is a closed one:

- `/billing/*` 404s (the blueprint stays registered so a stray `url_for`
  doesn't 500), as does `/settings/subscription`; every card, badge, seat
  counter and price line is hidden.
- **Nobody registers themselves.** `/auth/register` redirects to the login page
  unless the session carries a valid `pending_team_invite` — the token
  `teams.accept` parks there for an invited person who has no account yet — and
  then only for the address the invite was issued to. `./cairn create-user`
  is the other way in.
- **Everyone is premium.** `recompute_level` returns early: `global_admin` and
  `team_admin` stay as set (the latter is granted by hand, and deriving it from
  a payment nobody makes would take teams away), everyone else is `premium`.
  Nothing demotes, no trial is started, and `reconcile_all` neither downgrades
  nor dissolves — running `./cairn reconcile-subscriptions` after flipping the
  switch is what lifts the accounts that were already there.

`billing_enabled()` (Stripe configured) is now *inside* that switch, so a stray
key in the environment can't bring the model back on its own.
