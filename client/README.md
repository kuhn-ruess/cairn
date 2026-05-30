# Cairn CLI

A small command-line client for [Cairn](https://app.cairn-app.de/) that lets you
fire tasks into your inbox (or any project) straight from the terminal, and list
them with filters. Pure Python standard library — no third-party dependencies.

## Install

Recommended (isolated, on `PATH`):

```sh
pipx install cairn-cli
```

Or with pip:

```sh
pip install --user cairn-cli
```

From this repo (editable):

```sh
pipx install ./client      # or: pip install -e ./client
```

Works on macOS and Linux (Python 3.9+).

## Log in

```sh
cairn login
```

This suggests `https://app.cairn-app.de`, opens your browser to authorize, mints
an API token on the server, and writes it back into your local config — no
copy-pasting. Point at another server with `cairn login --host https://my.cairn`.

The token lives in `~/.config/cairn/config.json` (mode `0600`). Revoke it any
time from **Settings → Profile → API tokens** on the web app, or locally with
`cairn logout`.

## Add tasks

```sh
cairn add "Buy milk"                      # → Inbox
cairn add "Ship release" -p "Work"        # → the "Work" project
cairn add "Idea" --inbox                  # force Inbox, ignore default project
cairn add "Call Sam" -f --due 2026-06-01  # flagged, with a due date
cairn add "Read paper" -t reading -t deep # with tags
```

Set a default project so plain `add` goes there:

```sh
cairn config set default_project "Work"
cairn add "Goes to Work now"
cairn add "But this to inbox" --inbox
```

## List & filter

```sh
cairn list                       # recent tasks
cairn list --inbox               # only inbox (no project)
cairn list -p "Work"             # only the Work project
cairn list -s next               # by status
cairn list -f                    # only flagged
cairn list --assigned-to-me
cairn list --limit 100
```

## Other commands

```sh
cairn projects        # list project names (marks your default)
cairn whoami          # show the authenticated account
cairn config get      # show host / default project / login state
cairn logout
```

## Environment overrides

- `CAIRN_HOST` — override the server URL
- `CAIRN_TOKEN` — supply a token directly (useful in CI / scripts)
- `XDG_CONFIG_HOME` — relocate the config directory
