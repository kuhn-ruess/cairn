# Contributing to Cairn

Thanks for looking. This is a small project with a short chain of command, so
this file is short too: what you need to run it, how to know you haven't broken
anything, and how a commit is expected to read.

## License of contributions

Cairn is licensed under the **GNU Affero General Public License v3.0** (see
[`LICENSE`](LICENSE)). Contributions are accepted under that same license — by
opening a pull request you license your work under AGPL-3.0. There is no CLA.

The copyright holder is Kuhn & Rueß GmbH, which also offers commercial licenses
for cases the AGPL doesn't fit.

## Getting it running

Either path works; the Docker one is what the maintainers use day to day.

**Docker / Podman** — `./helper` auto-detects the runtime and layers
`docker-compose.yml` + `docker-compose.local.yml`, which mounts the source tree
and runs `flask --debug run`, so edits reload immediately:

```sh
./helper up                                 # build + start mongo, redis, app
./helper create_user alice alice@example.com
./helper seed_demo alice                    # sample projects/tags/tasks
./helper logs                               # tail app logs
./helper down                               # stop + remove
```

Compose wants a `.env` beside it and a fresh clone has none, so the first
`./helper` run seeds one from `.env.example` (names only, no secrets). Fill in
what you need — SMTP, Google, Microsoft. The core values (`BKTODO_SECRET_KEY`,
Mongo, Redis) are set in `docker-compose.yml` itself and win over `.env`.

**Local venv** — needs a MongoDB reachable on `localhost:27017`:

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
docker run -d --name cairn-mongo -p 27017:27017 mongo:7

export BKTODO_SECRET_KEY=dev-secret
flask --app wsgi:app cairn create-user alice alice@example.com
flask --app wsgi:app --debug run --port 5004
```

`./cairn <cmd>` and `flask --app wsgi:app cairn <cmd>` are the same CLI;
`./helper cairn <cmd>` runs it inside the container.

## Tests

Plain stdlib `unittest` — there is no pytest and no fixture harness:

```sh
python -m unittest discover tests
```

Most of the suite talks to MongoDB, so the same `localhost:27017` the app uses
has to be up; the tests use their own database (`bktodo_test`, `TestConfig`).
`pip install -r requirements.txt` is not optional for a full run — a venv
without Pillow fails the booking-page tests with `No module named 'PIL'`.

A push to `main` deploys to production, so `main` has to stay green. If you
touch `application/api/v1.py`, add the route to the hand-written spec in
`application/api/openapi.py` as well — a test asserts the two agree.

## Commits

Conventional-commits style, which is what `git log` will show you:

```
feat(booking): the guest can bring their own meeting link
fix(zammad): a hand-over reaches its new owner, also later
docs: note how a booking is cancelled
```

* Type: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `style`,
  `ci`, `revert`. Optional scope in parentheses — the area, not the file
  (`time`, `notes`, `booking`, `api`, `inspector`, `share`, …).
* The subject describes the **outcome in plain words**, lowercase, no trailing
  period: what a user can now do, or what no longer goes wrong. Not "refactor
  the handler".
* The body is where the *why* goes — what was wrong before, what was rejected
  and for what reason. Wrap it at ~75 columns. A one-line change can skip it;
  anything with a judgement call in it should not.
* A pre-commit hook bumps `APP_VERSION` in `application/__init__.py` and
  re-stages the file. Leave that version number alone in your own edits.
  It is a single counter (593, 594, ...), no major/minor: it continues the
  last part of the old `1.4.x` numbers, so it only ever goes up.

## Pull requests

Open them against `main`. Keep a change and its tests in the same commit, and
keep unrelated cleanups out of it — the commit log is the project's memory of
why things look the way they do, and that only works if one commit is one
thought.
