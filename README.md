# Cairn

A web-based GTD-style todo app inspired by OmniFocus, with Todoist-style
collaboration (task assignment + comments) and a REST API.

Built on the same stack as
[cmdbsyncer](https://github.com/Bastian-Kuhn/cmdbsyncer): Flask + MongoDB
(mongoengine) + Flask-Login + Jinja2/Bootstrap.

## Features

- **GTD perspectives**: Inbox, Today, Next, Forecast, Flagged, Waiting,
  Someday/Maybe, Assigned-to-me, Completed
- **Projects** (parallel / sequential / single-actions) with folders,
  review intervals and a weekly-review screen
- **Tags / contexts** with colors and on-hold state
- **Tasks** with defer- and due-dates, priority, flags, estimates,
  per-day recurrence and embedded **comments**
- **Collaboration**: share projects with other users, assign tasks
- **REST API** at `/api/v1` with bearer-token authentication

## Quick start (Docker)

The `./helper` script wraps the most common dev-loop commands (modeled on the
cmdbsyncer helper). It auto-detects docker or podman.

```sh
./helper up                                 # build + start (mongo + app, foreground)
./helper create_user alice alice@example.com
./helper issue_token alice                  # prints a REST API token
./helper logs                               # tail app logs
./helper shell                              # shell inside the app container
./helper cairn list-users                  # run any cairn CLI subcommand
./helper down                               # stop + remove
```

`./helper up` uses `docker-compose.yml` plus `docker-compose.local.yml` (the
latter mounts the source tree and runs `flask --debug run`, so edits reload
instantly).

## Local development

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# start MongoDB (one option):
docker run -d --name cairn-mongo -p 27017:27017 mongo:7

export BKTODO_SECRET_KEY=dev-secret
flask --app wsgi:app cairn create-user alice alice@example.com
flask --app wsgi:app cairn seed-demo alice
flask --app wsgi:app --debug run
```

## CLI commands

Two equivalent entry points:

* `./cairn <cmd>` — source-checkout wrapper (same pattern as
  `./cmdbsyncer`). Loads the app context and dispatches to Click.
* `flask --app wsgi:app cairn <cmd>` — Flask CLI group, useful when the
  `flask` binary is already in your shell.

| Command | What it does |
|---|---|
| `./cairn create-user USER EMAIL` | Create a user (prompts for password) |
| `./cairn issue-token USER [--name NAME]` | Print a new API token |
| `./cairn list-users` | List all users |
| `./cairn seed-demo USER` | Create sample projects/tags/tasks |

## Configuration

All settings are overridable through `BKTODO_*` env vars:

| Variable | Default |
|---|---|
| `BKTODO_SECRET_KEY` | `dev-change-me` |
| `BKTODO_MONGO_HOST` | `localhost` |
| `BKTODO_MONGO_PORT` | `27017` |
| `BKTODO_MONGO_DB` | `bktodo` (the legacy internal name — keeps existing data working) |
| `BKTODO_MONGO_USER` / `_PASSWORD` / `_AUTH_SOURCE` | empty |
| `BKTODO_CONFIG` | `application.config.DevConfig` |

## REST API

Authenticate every request with either header:

```
Authorization: Bearer <token>
X-API-Token: <token>
```

Tokens are issued with `flask --app wsgi:app cairn issue-token <user>`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/me` | current user |
| GET/POST | `/api/v1/tasks` | list / create tasks |
| GET/PATCH/DELETE | `/api/v1/tasks/<id>` | single task |
| POST | `/api/v1/tasks/<id>/complete` | complete + spawn next recurrence |
| POST | `/api/v1/tasks/<id>/assign` | `{ "user_id": "..." }` or `null` |
| POST | `/api/v1/tasks/<id>/comments` | `{ "body": "..." }` |
| GET/POST | `/api/v1/projects` | list / create projects |
| GET/PATCH/DELETE | `/api/v1/projects/<id>` | single project |
| POST | `/api/v1/projects/<id>/review` | mark reviewed |
| GET/POST | `/api/v1/tags` | list / create tags |
| PATCH/DELETE | `/api/v1/tags/<id>` | single tag |

Task list query params: `status`, `project_id`, `tag_id`, `flagged=true`,
`assigned_to_me=true`, `limit`, `offset`.

Example:

```sh
curl -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"title": "Buy milk", "status": "next"}' \
     http://localhost:5004/api/v1/tasks
```

## Command-line client

A standalone CLI (in [`client/`](client/)) fires tasks into your inbox and lists
them from the terminal. Pure Python standard library — no third-party
dependencies — and runs on macOS and Linux (Python 3.9+).

```sh
pipx install ./client      # or: pip install -e ./client
```

Log in once. This suggests `https://app.cairn-app.de`, opens your browser to
authorize, mints an API token on the server, and writes it back into the local
config automatically — no copy-pasting:

```sh
cairn login                          # point elsewhere with: cairn login --host https://my.cairn
```

The token lands in `~/.config/cairn/config.json` (mode `0600`). Revoke it from
**Settings → Profile → API tokens**, or locally with `cairn logout`.

```sh
cairn add "Buy milk"                 # → Inbox
cairn add "Ship release" -p "Work"   # → a project (by name)
cairn add "Call Sam" -f --due 2026-06-01 -t phone   # flagged, due date, tag
cairn config set default_project "Work"   # plain `add` now goes to Work…
cairn add "Quick note" --inbox            # …unless you force the inbox

cairn list                           # recent tasks
cairn list --inbox                   # only inbox
cairn list -p "Work" -s next         # filter by project and status
cairn list -f                        # only flagged

cairn projects                       # list project names
cairn whoami
```

Env overrides: `CAIRN_HOST`, `CAIRN_TOKEN` (handy in CI/scripts). Full reference
in [`client/README.md`](client/README.md). The browser login leg is served by
the app at `/settings/cli-authorize`.

## Layout

```
application/
  __init__.py         app factory
  config.py           Dev / Prod / Test config
  cli.py              flask cairn commands + ./cairn entrypoint
  forms.py            WTForms
  models/             User, Project, Tag, Task (mongoengine)
  views/              auth, dashboard, tasks, projects, tags, review
  api/v1.py           REST API (token auth)
  templates/          Jinja2 + Bootstrap 5
  static/             css/js
cairn                CLI wrapper (source checkout)
helper                dev-loop wrapper for docker/podman compose
wsgi.py               WSGI entrypoint
docker-compose.yml    app + mongo (prod-style)
docker-compose.local.yml   dev overlay (volume mount + FLASK_DEBUG)
Dockerfile            production image
Dockerfile.local      dev image
requirements.txt
```
