#!/usr/bin/env python3
"""Agenda → Cairn: archive the notes you marked, then clear them out of Agenda.

Agenda is where a note is *current* — a meeting, a ticket, this week. Cairn is
where it is *kept*. This is the one-way trip between the two: tag a note
``#cairn`` in Agenda, run this, and the note is in Cairn's tree with its
original dates and Agenda's note is in Agenda's trash (which is reversible —
the app has no hard delete at all).

Where a note lands
------------------
From Agenda's own structure, under a prefix you choose::

    Kunden/<Kategorie>/<Projekt>/

A project with no category simply has no category level, and a category that
repeats the prefix is not written twice — an Agenda category "Kunden" under
the prefix ``Kunden`` stays one level. The API creates what is missing
(``folder_path`` on ``POST /notes``), so there is nothing to set up here or
over there.

Run twice, and nothing is duplicated: every note goes up with
``external_id: agenda:<uuid>``, which the API treats as "update that one".
A note whose upload failed is NOT trashed in Agenda, so a failed run can
simply be run again.

Both ends are local-ish and need no libraries:

* Agenda's MCP server answers on ``http://127.0.0.1:16106/mcp`` while the app
  is running (plain JSON-RPC over HTTP — no Claude in the loop, so this works
  from cron).
* Cairn is the REST API: ``CAIRN_URL`` + ``CAIRN_TOKEN`` in the environment,
  the token minted under Settings → Profile → API tokens.

Usage::

    ./agenda_archive.py --dry-run          # what would go, and where
    ./agenda_archive.py                    # do it
    ./agenda_archive.py --tag archiv --prefix "Archiv/Agenda" --keep
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

AGENDA_URL = os.environ.get("AGENDA_MCP_URL", "http://127.0.0.1:16106/mcp")
PROTOCOL = "2024-11-05"


class Failed(Exception):
    """Something the user has to see, said in one line."""


def _post_json(url: str, payload: dict, headers: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **headers})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw[:300]}
    except urllib.error.URLError as exc:
        raise Failed(f"{url}: {exc.reason}") from exc


class Agenda:
    """The local MCP server, spoken to directly.

    One session for the whole run: ``initialize`` hands out a session id that
    every later call carries. A tool answers with its JSON inside
    ``result.content[0].text`` — a string, not an object.
    """

    def __init__(self, url: str = AGENDA_URL):
        self.url = url
        self.session = None
        self._id = 0
        self._open()

    def _open(self) -> None:
        status, body, session = self._send({
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "cairn-agenda-archive",
                                      "version": "1.0"}},
        }, want_session=True)
        if status != 200 or "result" not in body:
            raise Failed(f"Agenda's MCP server did not start a session "
                         f"({status}). Is Agenda running?")
        self.session = session

    def _send(self, message: dict, want_session: bool = False):
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, **message}
        headers = {"Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              **headers})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                parsed = json.loads(raw) if raw else {}
                if want_session:
                    return resp.status, parsed, resp.headers.get("Mcp-Session-Id")
                return resp.status, parsed
        except urllib.error.URLError as exc:
            raise Failed(f"Agenda at {self.url}: {exc.reason}. "
                         f"Is the app running?") from exc

    def call(self, tool: str, **arguments):
        status, body = self._send({"method": "tools/call",
                                   "params": {"name": tool,
                                              "arguments": arguments}})
        if status != 200 or "result" not in body:
            raise Failed(f"{tool}: {body.get('error') or status}")
        content = body["result"].get("content") or []
        text = content[0].get("text", "") if content else ""
        if body["result"].get("isError"):
            raise Failed(f"{tool}: {text[:200]}")
        try:
            return json.loads(text) if text else {}
        except ValueError:
            return {"text": text}


class Cairn:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def whoami(self) -> str:
        """Which account this token writes into — said out loud before the
        first note goes up, because a token in the environment belongs to
        whoever minted it, and an archive filed under the wrong account is a
        quiet mistake."""
        req = urllib.request.Request(
            f"{self.url}/api/v1/me",
            headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                me = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise Failed(f"Cairn refused the token ({exc.code}).") from exc
        except urllib.error.URLError as exc:
            raise Failed(f"{self.url}: {exc.reason}") from exc
        return me.get("username") or me.get("email") or "?"

    def create_note(self, payload: dict) -> dict:
        status, body = _post_json(f"{self.url}/api/v1/notes", payload,
                                  {"Authorization": f"Bearer {self.token}"})
        if status not in (200, 201):
            raise Failed(f"Cairn answered {status}: "
                         f"{body.get('error') or body}")
        body["_created"] = status == 201
        return body


def folder_name(title: str) -> str:
    """A folder title that survives being a path segment — and, later, a file
    name in the WebDAV mirror. Only the separator has to go."""
    return re.sub(r"\s+", " ", (title or "").replace("/", "-")).strip() or "Ohne Titel"


def strip_marker(text: str, tag: str) -> str:
    """Take the trigger tag out of the archived text.

    ``#cairn`` is an instruction to this script, not something the note says,
    and Cairn has no ``#tag`` syntax to show it as anything but noise.
    """
    out = re.sub(rf"(?<!\w)#{re.escape(tag)}\b", "", text or "")
    return re.sub(r"[ \t]+(\n|$)", r"\1", out)


def note_path(prefix: str, parts: list) -> str:
    """The folder path for a note: the prefix, then Agenda's own levels.

    A first Agenda level that repeats the prefix is dropped — filing the
    category "Kunden" under the folder "Kunden" says the same thing twice, and
    nobody means that by "Kunden/<Agenda-Pfad>".
    """
    levels = [p for p in prefix.strip("/").split("/") if p]
    rest = list(parts)
    if levels and rest and rest[0].casefold() == levels[-1].casefold():
        rest = rest[1:]
    return "/".join(levels + rest)


def paths_of(projects: list) -> dict:
    """``project id -> [Kategorie, Projekt]``, each part already a safe name."""
    out = {}
    for p in projects:
        parts = [p.get("categoryTitle"), p.get("title")]
        out[p.get("id")] = [folder_name(x) for x in parts if (x or "").strip()]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Archive tagged Agenda notes into Cairn.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="CAIRN_URL and CAIRN_TOKEN come from the environment.")
    ap.add_argument("--tag", default="cairn",
                    help="the Agenda tag that marks a note for archiving, "
                         "without the # (default: cairn)")
    ap.add_argument("--prefix", default="Kunden",
                    help="where the Agenda tree hangs in Cairn's notes tree "
                         "(default: Kunden)")
    ap.add_argument("--limit", type=int, default=100,
                    help="most notes per run (default: 100)")
    ap.add_argument("--dry-run", action="store_true",
                    help="say what would happen, write nothing")
    ap.add_argument("--keep", action="store_true",
                    help="leave the notes in Agenda instead of trashing them")
    ap.add_argument("--agenda-url", default=AGENDA_URL)
    ap.add_argument("--cairn-url", default=os.environ.get("CAIRN_URL", ""))
    args = ap.parse_args(argv)

    token = os.environ.get("CAIRN_TOKEN", "")
    if not args.dry_run and not (args.cairn_url and token):
        raise Failed("CAIRN_URL and CAIRN_TOKEN have to be in the environment "
                     "(Settings → Profile → API tokens).")

    agenda = Agenda(args.agenda_url)
    projects = paths_of(agenda.call("agenda_list_projects",
                                    includeArchived=True).get("projects") or [])
    found = agenda.call("agenda_search_notes", query=f"#{args.tag}",
                        limit=max(1, min(args.limit, 100)))
    notes = found.get("notes") or []
    if not notes:
        print(f"Nothing tagged #{args.tag} in Agenda.")
        return 0

    # Where this is going, said before anything moves — and in a dry run too,
    # which is the moment you actually want to read it. Asking the server who
    # the token belongs to is a GET, so the rehearsal stays a rehearsal.
    cairn = Cairn(args.cairn_url, token) if (args.cairn_url and token) else None
    target = (f"{args.cairn_url} as {cairn.whoami()}" if cairn
              else "nowhere — no CAIRN_URL / CAIRN_TOKEN in the environment")
    print(f"{len(notes)} note(s) tagged #{args.tag} → {target}")
    done = failed = 0
    for summary in notes:
        note_id = summary.get("id")
        try:
            note = agenda.call("agenda_get_note", noteId=note_id)
            path = note_path(args.prefix,
                             projects.get(note.get("projectId"), []))
            title = (note.get("title") or "").strip()
            payload = {
                "note": strip_marker(note.get("markdown") or "", args.tag),
                # Agenda writes prose, not an outline, and it comes back
                # exactly as typed — that is the whole point of the format.
                "note_format": "markdown",
                "folder_path": path,
                "external_id": f"agenda:{note_id}",
                "source": "agenda",
                "tags": ["agenda"],
                "created": note.get("createdDate"),
                "updated": note.get("editedDate"),
            }
            if title:
                payload["title"] = title
            if args.dry_run:
                print(f"  would archive: {title or '(untitled)'} → {path}")
                done += 1
                continue
            saved = cairn.create_note(payload)
            verb = "archived" if saved.get("_created") else "updated"
            print(f"  {verb}: {saved.get('title')} → {path}")
            if not args.keep:
                agenda.call("agenda_trash_note", noteId=note_id)
            done += 1
        except Failed as exc:
            failed += 1
            print(f"  FAILED {summary.get('title') or note_id}: {exc}",
                  file=sys.stderr)

    where = "would go to Cairn" if args.dry_run else "in Cairn"
    kept = " (left in Agenda)" if args.keep or args.dry_run else ""
    print(f"{done} note(s) {where}{kept}, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failed as exc:
        print(f"agenda-archive: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
