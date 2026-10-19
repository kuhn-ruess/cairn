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
- **Tags** with colors and on-hold state
- **Tasks** with defer- and due-dates, priority, flags, estimates,
  per-day recurrence and embedded **comments**
- **Collaboration**: share projects with other users, assign tasks
- **REST API** at `/api/v1` with bearer-token authentication

## Quick start (Docker)

The `./helper` script wraps the most common dev-loop commands (modeled on the
cmdbsyncer helper). It auto-detects docker or podman.

Compose reads a `.env` beside the compose file, and a fresh clone has none —
`./helper` copies `.env.example` into place on its first run so the stack comes
up, then you fill in what you actually use (SMTP, Google, Microsoft …). The
core four (`BKTODO_SECRET_KEY`, Mongo host/db, Redis URL) are set in
`docker-compose.yml` itself and take precedence over `.env`; change them there
before this goes anywhere near production.

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
| `./cairn test-mail ADDRESS` | Send a test email and print why it failed |

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

### Impressum & Datenschutz (who runs your instance)

Cairn ships the **text** of `/impressum` and `/datenschutz` — what the software
stores, which cookies it sets, which third parties it talks to once you switch
them on. It does **not** ship an operator. Until you fill these in, both pages
print "Der Betreiber dieser Instanz hat keine Impressumsangaben hinterlegt"
instead of somebody else's name and address, so an instance can never publish a
legal identity that isn't yours.

| Variable | | |
|---|---|---|
| `BKTODO_LEGAL_OPERATOR_NAME` | required | without it the pages show the notice |
| `BKTODO_LEGAL_OPERATOR_ADDRESS` | required | `\|` separates lines: `Musterstr. 1\|12345 Musterstadt` |
| `BKTODO_LEGAL_OPERATOR_EMAIL` | required | |
| `BKTODO_LEGAL_OPERATOR_ADDITION` | optional | second line, e.g. a trade name |
| `BKTODO_LEGAL_OPERATOR_PHONE` | optional | |
| `BKTODO_LEGAL_OPERATOR_REPRESENTATIVE` | optional | Vertretungsberechtigte(r) |
| `BKTODO_LEGAL_OPERATOR_REGISTER` | optional | `Amtsgericht München, HRB 12345` |
| `BKTODO_LEGAL_OPERATOR_VAT_ID` | optional | USt-IdNr. |
| `BKTODO_LEGAL_OPERATOR_INSURANCE` | optional | `\|`-separated lines |
| `BKTODO_LEGAL_HOSTING_PROVIDER` | optional | named in the hosting section, carries its own article: `der Example Hosting GmbH (Musterstadt)` |

Optional fields are simply left out of the page when unset. What the pages say
beyond the identity is Cairn's own text — check that it matches how *you* run
the instance (hosting, mail, which integrations you allow) before you rely on
it; it is a starting point, not legal advice.

### Email

Mail is **off** until `BKTODO_SMTP_HOST` is set — nothing is sent and nothing
complains, which is the usual reason password resets and booking
confirmations don't arrive.

| Variable | Default | |
|---|---|---|
| `BKTODO_SMTP_HOST` | empty | empty = mail switched off |
| `BKTODO_SMTP_PORT` | `587` | 587 STARTTLS · 465 implicit TLS |
| `BKTODO_SMTP_USER` / `_PASSWORD` | empty | omit both for a relay without login |
| `BKTODO_SMTP_FROM` | the SMTP user | many providers refuse a foreign From |
| `BKTODO_SMTP_TLS` | `1` | `0` disables STARTTLS |
| `BKTODO_SMTP_SSL` | by port | `1` forces implicit TLS on any port |

Check it end to end — this prints the resolved settings and the server's own
answer, which is the difference between "wrong password" and "wrong TLS mode":

```sh
./helper cairn test-mail you@example.com     # docker
./cairn test-mail you@example.com            # venv
```

### Microsoft Teams meetings (booking page)

Every appointment taken through a booking page can carry its own freshly
created Teams meeting. It takes two things, and they are done by two different
people: the **administrator registers one Entra app for the whole deployment**,
and then **each user connects their own Microsoft account**, because a Teams
meeting has an organiser and the organiser is whoever's page was booked.

**1. Register the app** (once, at
[entra.microsoft.com](https://entra.microsoft.com)):

1. Applications → App registrations → New registration, any name.
2. Supported account types: *Accounts in any organizational directory*. A
   personal Microsoft account cannot host a Teams meeting through Graph, so
   letting one consent would only buy a connection that fails on first use.
3. Redirect URI, platform **Web**:
   `https://<host>/settings/booking/microsoft/callback` — locally
   `http://localhost:5004/settings/booking/microsoft/callback`. It has to match
   the address users reach Cairn on, character for character.
4. API permissions → Microsoft Graph → **Delegated**:
   `OnlineMeetings.ReadWrite`, `User.Read`, `offline_access`. Grant admin
   consent if the tenant requires it, otherwise each user is asked at connect
   time.
5. Certificates & secrets → New client secret → copy the **Value** (not the
   Secret ID). It is shown once, and it expires — note the date.

**2. Put it in the environment** and restart (on the server: `/opt/cairn/.env`,
then the compose rebuild; see the deploy notes):

| Variable | Default |
|---|---|
| `BKTODO_MS_CLIENT_ID` | empty (no Teams option shown) |
| `BKTODO_MS_CLIENT_SECRET` | empty |
| `BKTODO_MS_TENANT` | `organizations` |

**3. Connect an account**, per user, under Settings → Booking page: save the
form first (connecting leaves the page), *Connect Microsoft*, consent, and the
page then names the account it linked. Pick *A fresh Microsoft Teams meeting
per appointment* under **How you meet** and save — the option stays disabled
until both halves are in place, so a page can never promise a link it cannot
make. *Disconnect* drops the tokens and puts a page that was on `teams` back to
no meeting, for the same reason.

From then on every booking calls `/me/onlineMeetings` once. The join URL lands
on the **booking**, not on the page, so changing the mode later never moves an
appointment somebody was already told to turn up at, and it goes into both
confirmation mails, into the `.ics` they carry, and into the calendar event's
`LOCATION` — the field a calendar turns into a join button.

What it deliberately does not do:

* **Nothing is ever read from Microsoft.** Busy times come only from the
  calendars connected under Settings → Calendars (Google, CalDAV, iCal). For a
  Microsoft 365 mailbox that means publishing the Outlook calendar as an ICS
  URL and adding it there — Exchange Online speaks no CalDAV, and a published
  ICS refreshes on Microsoft's own schedule, not instantly.
* **`/me/onlineMeetings` creates a meeting, not a calendar entry.** The
  appointment appears in the host's calendar only through the calendar Cairn
  writes to (Google or CalDAV), or by opening the `.ics` from the confirmation
  mail.
* A **link the visitor pastes** into the booking form wins over the page
  setting and skips the Graph call — two links only invite the two of you to
  different places.
* A failed Graph call is never the visitor's problem: the slot is confirmed
  either way and the reason is recorded on the booking, shown as *no meeting
  link* under Appointments. The usual cause is a connected account without a
  Teams licence, which Graph answers with 403.

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
| GET | `/api/v1/due` | everything that is due (late + today) |
| GET/POST | `/api/v1/tasks` | list / create tasks |
| GET/PATCH/DELETE | `/api/v1/tasks/<id>` | single task |
| POST | `/api/v1/tasks/<id>/complete` | complete + spawn next recurrence |
| POST | `/api/v1/tasks/<id>/assign` | `{ "user_id": "..." }` or `null` |
| POST | `/api/v1/tasks/<id>/comments` | `{ "body": "...", "commits": [...]? }` |
| POST | `/api/v1/tasks/<id>/commits` | `{ "commits": [{ "repo": "...", "sha": "...", "subject": "..."? }], "comment_id": "..."? }` |
| GET/POST | `/api/v1/projects` | list / create projects |
| GET/PATCH/DELETE | `/api/v1/projects/<id>` | single project |
| POST | `/api/v1/projects/<id>/review` | mark reviewed |
| GET/POST | `/api/v1/tags` | list / create tags |
| PATCH/DELETE | `/api/v1/tags/<id>` | single tag |

**Commits** made for a task are registered on it, so a review can jump to
the change: `POST /api/v1/tasks/<id>/commits` (or `commits` on the comment
that reports the work, which ties them to that comment). Each entry takes
`repo`, `sha` (full hash preferred, short accepted) and optionally `subject`,
`url` or `remote` (the git remote, turned into the forge's commit page). A
(repo, sha) is stored once. `GET /api/v1/tasks/<id>` returns them as
`commits` (each with its resolved `url`), the compact row as `commit_count`,
and the task page lists them with the short hash linked. Repos without a
`url`/`remote` are linked through `COMMIT_URL_TEMPLATES` in
`application/config.py` (repo name to `{sha}` template or remote; add one
with `BKTODO_COMMIT_URL_TEMPLATES__<repo>=...`); an unknown repo shows the
hash as copyable text.

Task list query params: `status`, `status_not`, `project_id`, `tag_id`,
`flagged=true`, `assigned_to_me=true`, `mine=true`, `awaiting_reply=true`,
`limit`, `offset`. Without `mine=true` the
list also carries what is merely visible to you — every task in a project
someone shared.

`status` and `status_not` take one status or a comma list
(`status_not=waiting,done`). Every row, compact or full, names its newest
comment by `created_at`: `last_comment_at`, `last_comment_author_id` (null for
a reply mirrored in without a Cairn account) and `last_comment_author_name`.
`awaiting_reply=true` keeps the tasks whose newest comment is not yours, or
that have none, so an inbox needs one call:
`/api/v1/tasks?assigned_to_me=true&status_not=waiting&awaiting_reply=true&kind=task&compact=true`.

`/api/v1/due` is the queue reader: open tasks whose due date has been reached,
oldest first, widened with `days=N` / `until=YYYY-MM-DD` or narrowed with
`overdue=true`. No focus and no perspective, so it never comes back short.

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

## License

Cairn is free software under the **GNU Affero General Public License v3.0**
(see [`LICENSE`](LICENSE) for the full text).

In practice: run it for yourself, inside your company, on your own server, and
modify it as much as you like — that costs nothing and asks nothing of you. The
one obligation AGPL adds over the ordinary GPL is section 13: if you run a
*modified* Cairn as a service other people can reach over a network, those
users must be able to get your modified source. Using it privately, however
heavily, is never a trigger.

If that doesn't fit — an OEM integration, a closed fork, a hosted product built
on Cairn — commercial licenses are available from Kuhn & Rueß GmbH.

```
Cairn — a GTD-style todo web application
Copyright (C) 2026 Kuhn & Rueß GmbH

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
```

Patches are welcome under the same license — see
[`CONTRIBUTING.md`](CONTRIBUTING.md). Security reports go the private route in
[`SECURITY.md`](SECURITY.md), not into an issue.
