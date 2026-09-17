"""Cairn command-line client.

Commands:
    cairn login [--host URL]      Browser login; stores an API token locally.
    cairn logout                  Forget the saved token.
    cairn add "Title" [opts]      Create a task (inbox by default).
    cairn list [filters]          List your tasks, with filters.
    cairn due [--days N]          Everything that is due (late + today).
    cairn projects                List your projects (names + ids).
    cairn config ...              Show / set host & default project.
    cairn whoami                  Show the authenticated account.

Pure standard library — installable with `pipx install cairn-cli`.
"""
import argparse
import http.server
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime

from . import config as cfg_mod
from .api import ApiError, Client

# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _eprint(*args):
    print(*args, file=sys.stderr)


def _client(cfg: dict, require_token: bool = True) -> Client:
    host = cfg_mod.get_host(cfg)
    token = cfg_mod.get_token(cfg)
    if require_token and not token:
        _eprint("Not logged in. Run `cairn login` first.")
        sys.exit(2)
    return Client(host, token)


def _resolve_project_id(client: Client, name: str) -> str:
    """Map a project name (case-insensitive) to its id, or exit with help."""
    data = client.list_projects() or {}
    projects = [p for p in data.get("items", []) if p.get("kind") == "project"]
    matches = [p for p in projects if (p.get("name") or "").lower() == name.lower()]
    if not matches:
        # fall back to a substring match for convenience
        matches = [p for p in projects if name.lower() in (p.get("name") or "").lower()]
    if not matches:
        _eprint(f"No project matching '{name}'. Try `cairn projects`.")
        sys.exit(1)
    if len(matches) > 1:
        names = ", ".join(repr(p["name"]) for p in matches)
        _eprint(f"Ambiguous project '{name}' — matches: {names}")
        sys.exit(1)
    return matches[0]["id"]


# ----------------------------------------------------------------------------
# login (gh-style loopback)
# ----------------------------------------------------------------------------


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server_version = "CairnCLI/0.1"
    result: dict = {}

    def do_GET(self):  # noqa: N802 (http.server API)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        token = (params.get("token") or [None])[0]
        state = (params.get("state") or [None])[0]
        type(self).result = {"token": token, "state": state}
        ok = bool(token) and state == self.server.expected_state  # type: ignore[attr-defined]
        body = (
            "<html><body style='font-family:sans-serif;text-align:center;"
            "margin-top:4rem'><h2>{}</h2><p>You can close this tab and return "
            "to the terminal.</p></body></html>"
        ).format("✓ Cairn CLI authorized" if ok else "✗ Authorization failed")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):  # silence default request logging
        pass


def _run_login_flow(host: str, token_name: str, open_browser: bool = True) -> str:
    state = secrets.token_urlsafe(16)
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    httpd.expected_state = state  # type: ignore[attr-defined]
    httpd.timeout = 1
    port = httpd.server_address[1]

    query = urllib.parse.urlencode({"port": port, "state": state, "name": token_name})
    auth_url = f"{host}/settings/cli-authorize?{query}"

    print("Opening your browser to authorize the Cairn CLI...")
    print(f"  {auth_url}")
    print("If the browser doesn't open, paste that URL into it manually.\n")
    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    # Block until the browser hits our loopback callback (Ctrl-C to abort).
    _CallbackHandler.result = {}
    deadline_loops = 300  # ~5 min at timeout=1s
    try:
        while not _CallbackHandler.result and deadline_loops > 0:
            httpd.handle_request()
            deadline_loops -= 1
    except KeyboardInterrupt:
        _eprint("\nLogin cancelled.")
        sys.exit(130)
    finally:
        httpd.server_close()

    result = _CallbackHandler.result
    if not result.get("token"):
        _eprint("Login timed out or was denied.")
        sys.exit(1)
    if result.get("state") != state:
        _eprint("Security check failed (state mismatch). Aborting.")
        sys.exit(1)
    return result["token"]


def cmd_login(args):
    cfg = cfg_mod.load()
    host = args.host or cfg.get("host")
    if not host:
        suggested = cfg_mod.DEFAULT_HOST
        entered = input(f"Cairn server [{suggested}]: ").strip()
        host = entered or suggested
    host = host.rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "https://" + host

    token = _run_login_flow(host, args.name, open_browser=not args.no_browser)

    # Verify the token works before persisting it.
    client = Client(host, token)
    try:
        me = client.me()
    except ApiError as exc:
        _eprint(f"Token verification failed: {exc}")
        sys.exit(1)

    cfg["host"] = host
    cfg["token"] = token
    cfg_mod.save(cfg)
    who = me.get("display_name") or me.get("username") if me else "?"
    print(f"\n✓ Logged in to {host} as {who}.")
    print(f"  Token saved to {cfg_mod.config_path()}")


def cmd_logout(args):
    cfg = cfg_mod.load()
    cfg.pop("token", None)
    cfg_mod.save(cfg)
    print("Logged out — token removed from local config.")


# ----------------------------------------------------------------------------
# tasks
# ----------------------------------------------------------------------------


def cmd_add(args):
    cfg = cfg_mod.load()
    client = _client(cfg)

    payload = {"title": args.title}
    if args.note:
        payload["note"] = args.note
    if args.flag:
        payload["flagged"] = True
    if args.due:
        payload["due_date"] = args.due
    if args.tag:
        payload["tags"] = args.tag

    # Project resolution: --inbox forces inbox; otherwise --project, else the
    # configured default. No project at all == inbox.
    project_name = None
    if not args.inbox:
        project_name = args.project or cfg.get("default_project")
    if project_name:
        payload["project_id"] = _resolve_project_id(client, project_name)

    try:
        task = client.create_task(payload)
    except ApiError as exc:
        _eprint(f"Failed to add task: {exc}")
        sys.exit(1)
    print(f"✓ Added to {project_name or 'Inbox'}: {task['title']}")


_STATUS_GLYPH = {"done": "✓", "dropped": "✗"}


def _project_names(client: Client, items: list) -> dict:
    """id → name for the projects the rows refer to.

    One extra call for the whole page: the task serializer carries ids only,
    and dereferencing per task on the server would be an N+1.
    """
    if not any(t.get("project_id") for t in items):
        return {}
    data = client.list_projects() or {}
    return {p["id"]: p["name"] for p in data.get("items", [])}


def _local(iso):
    """Parse an API timestamp into this machine's local time.

    The API answers in UTC; a due date is read on the clock on the wall, so
    every date this file prints goes through here first.
    """
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return None


def _due_str(iso) -> str:
    """``17.09. 16:05`` — the time only when there is one, since a date-only
    due date arrives as midnight."""
    dt = _local(iso)
    if dt is None:
        return ""
    day = dt.strftime("%d.%m.")
    return day if (dt.hour, dt.minute) == (0, 0) else f"{day} {dt:%H:%M}"


def cmd_list(args):
    cfg = cfg_mod.load()
    client = _client(cfg)

    # Tasks only (projects have their own command) and, unless asked
    # otherwise, only the ones on your plate: the endpoint also serves what is
    # merely visible to you — every task in a project someone shared.
    params = {"limit": args.limit, "kind": "task"}
    if not args.shared:
        params["mine"] = "true"
    if args.status:
        params["status"] = args.status
    if args.flagged:
        params["flagged"] = "true"
    if args.assigned_to_me:
        params["assigned_to_me"] = "true"

    if args.project:
        params["project_id"] = _resolve_project_id(client, args.project)

    try:
        data = client.list_tasks(**params)
    except ApiError as exc:
        _eprint(f"Failed to list tasks: {exc}")
        sys.exit(1)

    items = data.get("items", []) if data else []

    # Inbox filter (no parent) is client-side: the API has no "no project"
    # query param, so honour --inbox here.
    if args.inbox:
        items = [t for t in items if not t.get("project_id")]

    if not items:
        print("No tasks.")
        return

    proj_names = _project_names(client, items)

    for t in items:
        glyph = _STATUS_GLYPH.get(t.get("status"), "○")
        flag = " ⚑" if t.get("flagged") else ""
        proj = proj_names.get(t.get("project_id"))
        proj_str = f"  [{proj}]" if proj else ""
        due = _local(t.get("due_date"))
        due_str = f"  (due {due:%Y-%m-%d})" if due else ""
        print(f"{glyph} {t['title']}{flag}{proj_str}{due_str}")
    if len(items) >= args.limit:
        print(f"\n…showing first {args.limit}; raise with --limit.")


def cmd_due(args):
    cfg = cfg_mod.load()
    client = _client(cfg)

    params = {"limit": args.limit}
    if args.days:
        params["days"] = args.days
    if args.until:
        params["until"] = args.until
    if args.overdue:
        params["overdue"] = "true"

    try:
        data = client.list_due(**params)
    except ApiError as exc:
        _eprint(f"Failed to list due tasks: {exc}")
        sys.exit(1)

    tasks = data.get("tasks", []) if data else []
    if not tasks:
        print("Nothing overdue." if args.overdue else "Nothing due.")
        return

    proj_names = _project_names(client, tasks)
    for t in tasks:
        mark = "!" if t.get("overdue") else "○"
        flag = " ⚑" if t.get("flagged") else ""
        proj = proj_names.get(t.get("project_id"))
        proj_str = f"  [{proj}]" if proj else ""
        print(f"{mark} {_due_str(t.get('due_date')):>12}  {t['title']}{flag}{proj_str}")

    overdue = data.get("overdue_count", 0)
    total = data.get("count", len(tasks))
    print(f"\n{total} overdue." if args.overdue
          else f"\n{total} due, {overdue} of them late.")


def cmd_projects(args):
    cfg = cfg_mod.load()
    client = _client(cfg)
    try:
        data = client.list_projects()
    except ApiError as exc:
        _eprint(f"Failed to list projects: {exc}")
        sys.exit(1)
    items = [p for p in (data.get("items", []) if data else []) if p.get("kind") == "project"]
    if not items:
        print("No projects.")
        return
    default = cfg.get("default_project")
    for p in items:
        mark = " (default)" if default and p["name"].lower() == default.lower() else ""
        print(f"{p['name']}{mark}")


# ----------------------------------------------------------------------------
# config / whoami
# ----------------------------------------------------------------------------


def cmd_config(args):
    cfg = cfg_mod.load()
    if args.config_action == "get" or args.config_action is None:
        print(f"host             {cfg_mod.get_host(cfg)}")
        print(f"default_project  {cfg.get('default_project') or '(none — inbox)'}")
        print(f"logged_in        {'yes' if cfg_mod.get_token(cfg) else 'no'}")
        print(f"config_file      {cfg_mod.config_path()}")
        return
    if args.config_action == "set":
        key, value = args.key, args.value
        if key not in ("host", "default_project"):
            _eprint("Settable keys: host, default_project")
            sys.exit(1)
        cfg[key] = value.rstrip("/") if key == "host" else value
        cfg_mod.save(cfg)
        print(f"Set {key} = {cfg[key]}")
        return
    if args.config_action == "unset":
        cfg.pop(args.key, None)
        cfg_mod.save(cfg)
        print(f"Unset {args.key}")


def cmd_whoami(args):
    cfg = cfg_mod.load()
    client = _client(cfg)
    try:
        me = client.me()
    except ApiError as exc:
        _eprint(str(exc))
        sys.exit(1)
    print(f"{me.get('display_name') or me.get('username')} <{me.get('email')}>")
    print(f"  host: {cfg_mod.get_host(cfg)}")


# ----------------------------------------------------------------------------
# argparse wiring
# ----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cairn", description="Cairn command-line client.")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("login", help="Authorize via the browser and save a token.")
    sp.add_argument("--host", help=f"Server URL (default: {cfg_mod.DEFAULT_HOST}).")
    sp.add_argument("--name", default=_default_token_name(), help="Token name shown in your profile.")
    sp.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser.")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("logout", help="Remove the saved token.")
    sp.set_defaults(func=cmd_logout)

    sp = sub.add_parser("add", help="Add a task (inbox by default).")
    sp.add_argument("title", help="Task title.")
    sp.add_argument("-p", "--project", help="Target project name (overrides default).")
    sp.add_argument("--inbox", action="store_true", help="Force inbox, ignoring the default project.")
    sp.add_argument("-n", "--note", help="Task note/body.")
    sp.add_argument("-f", "--flag", action="store_true", help="Flag the task.")
    sp.add_argument("--due", help="Due date (ISO, e.g. 2026-06-01 or 2026-06-01T17:00).")
    sp.add_argument("-t", "--tag", action="append", help="Tag name (repeatable).")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("list", help="List your tasks, with filters.")
    sp.add_argument("-p", "--project", help="Only this project (by name).")
    sp.add_argument("--inbox", action="store_true", help="Only inbox tasks (no project).")
    sp.add_argument("-s", "--status", help="Filter by status (e.g. inbox, next, done, waiting…).")
    sp.add_argument("-f", "--flagged", action="store_true", help="Only flagged tasks.")
    sp.add_argument("--assigned-to-me", action="store_true", help="Only tasks assigned to me.")
    sp.add_argument("--shared", action="store_true",
                    help="Also list tasks that merely reach you through a shared project.")
    sp.add_argument("--limit", type=int, default=50, help="Max rows (default 50).")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("due", help="Everything that is due: late, plus today.")
    sp.add_argument("--days", type=int, help="Look N days ahead (0 = today, the default).")
    sp.add_argument("--until", help="Last day to include (YYYY-MM-DD); wins over --days.")
    sp.add_argument("--overdue", action="store_true", help="Only what is already late.")
    sp.add_argument("--limit", type=int, default=200, help="Max rows (default 200).")
    sp.set_defaults(func=cmd_due)

    sp = sub.add_parser("projects", help="List your projects.")
    sp.set_defaults(func=cmd_projects)

    sp = sub.add_parser("config", help="Show or change CLI settings.")
    csub = sp.add_subparsers(dest="config_action")
    csub.add_parser("get", help="Show current settings.")
    cset = csub.add_parser("set", help="Set host or default_project.")
    cset.add_argument("key")
    cset.add_argument("value")
    cunset = csub.add_parser("unset", help="Remove a setting.")
    cunset.add_argument("key")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("whoami", help="Show the authenticated account.")
    sp.set_defaults(func=cmd_whoami)

    return p


def _default_token_name() -> str:
    try:
        return f"CLI@{socket.gethostname()}"
    except Exception:
        return "CLI"


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
