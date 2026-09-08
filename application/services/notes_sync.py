"""Notes ⇄ a WebDAV folder — a two-way mirror.

Every note the user owns becomes one Markdown file in a folder tree that
matches the notes tree, with its attachments in an ``_assets`` folder beside
it, so a NAS share can be indexed by DEVONthink (or opened in Obsidian, or
grepped) and edits made *there* come back.

How a round works
-----------------
Each note remembers three things about its file (on ``Item``): where it was
written (``mirror_path``), the hash of what was written (``mirror_hash``) and
the server's change validator at that moment (``mirror_tag``). The hash is the
common ancestor, which is what makes this a sync rather than a copy:

* file's validator unchanged, note changed → **push**
* file changed, note unchanged            → **pull**
* both changed                            → **conflict**: the note stays
  canonical and the file's version is written beside it as
  ``<name> (conflict …).md``. It is picked up as a new note on the next round,
  so both texts exist in both places and a human decides. Nothing is merged
  and nothing is dropped.
* file gone, note tracked                 → the note goes to the **trash**
  (never a hard delete)
* note trashed                            → its file and assets are removed

A pull brings the file's **attachments** with it: an image dropped into the
document over there is a real file in ``_assets`` and a relative link in the
text, and both are meaningless in the app until the bytes are stored on the
note (see :meth:`_Round.absorb_assets`).

A file that isn't tracked is read once: its ``cairn-id`` front-matter says
whether it is a known note that moved (rename/move in DEVONthink → the note
moves too) or something new to import.

Safety
------
Deletions are the dangerous half. If the remote root lists **nothing** while
notes claim to have files there, the round refuses to delete anything: an
unmounted share must not empty the notebook. Any WebDAV failure aborts the
round before it writes, and the run is recorded in the sync log either way.
"""
from __future__ import annotations

import hashlib
import mimetypes
import posixpath
import re
from urllib.parse import quote, unquote
from dataclasses import dataclass, field
from datetime import datetime, timezone

from application.models.item import Item
from application.services import tasks_mirror
from application.services.webdav import DavClient, DavError

ASSETS_DIR = "_assets"
MD = ".md"
EXT_BY_CTYPE = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
    "image/webp": "webp",
}

# The front-matter block a mirrored file carries. Parsed by hand — the fields
# are ours, and a YAML dependency for six keys would be a poor trade.
_FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.S)
_LIST_RE = re.compile(r"\A\[(.*)\]\Z", re.S)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_dt(value: str):
    """A timestamp from over there as an aware datetime, or None.

    Two shapes reach this: the ISO stamp in front matter we wrote, and the
    HTTP date the share answers with (``Last-Modified``).
    """
    raw = (value or "").strip()
    if not raw:
        return None
    for read in (datetime.fromisoformat, _http_date):
        try:
            dt = read(raw)
        except (TypeError, ValueError):
            continue
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _http_date(raw: str):
    from email.utils import parsedate_to_datetime
    return parsedate_to_datetime(raw)


def _stamp(dt) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def safe_name(name: str) -> str:
    """A file or folder name that survives a NAS, a Mac and a browser."""
    s = (name or "").replace(" ", " ").strip()
    s = re.sub(r'[\\/:*?"<>|]', "-", s)
    s = re.sub(r"[\x00-\x1f]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:120] or "Untitled"


# ---------------------------------------------------------------------------
# The file: front matter + body, and the asset links inside it
# ---------------------------------------------------------------------------

def _asset_names(note) -> list[tuple[str, str, bytes]]:
    """``(in-app URL, file name, bytes)`` for everything embedded in a note."""
    out = []
    for img in (note.note_images or []):
        ext = EXT_BY_CTYPE.get(img.content_type, "img")
        out.append((f"/notes/{note.pk}/note-image/{img.token}",
                    f"{img.token}.{ext}", bytes(img.data)))
    for f in (note.note_files or []):
        out.append((f"/notes/{note.pk}/note-file/{f.token}",
                    f"{f.token[:6]}-{safe_name(f.filename)}", bytes(f.data)))
    return out


def assets_dir(path: str, note_id) -> str:
    """Where a note's attachments live: an ``_assets`` folder in the note's own
    directory, so a folder carries everything its documents need and moving one
    somewhere else takes its images along."""
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    tail = f"{ASSETS_DIR}/{note_id}"
    return f"{directory}/{tail}" if directory else tail


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

# A Markdown link whose target is a local .md file, with an optional fragment:
# what this module writes, and what it reads back.
FILE_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+?\.md)(?:#([^)\s]*))?\)", re.I)


# The target of a Markdown link or image — ``](href)``, with the angle
# brackets and the title an editor may put around it. Every attachment in a
# mirrored file looks like this, whichever folder the app that wrote it keeps
# its files in.
LINK_TARGET_RE = re.compile(r"\]\(\s*<?([^)>\s]+)>?(?:\s+\"[^\"]*\")?\s*\)")


# A block property (``id:: …``, ``collapsed:: …``) as the outline writes it:
# a line of its own, two spaces past the bullet. Markdown does not read it as a
# line of its own, though — it is a lazy continuation, so every renderer prints
# it INSIDE the bullet's text, which is where the machine noise showed up.
PROP_LINE_RE = re.compile(
    r"^(\t*  )([A-Za-z0-9_][A-Za-z0-9_-]*):: ?(.*)$", re.M)
HIDDEN_PROP_RE = re.compile(
    r"^(\t*  )<!-- ([A-Za-z0-9_][A-Za-z0-9_-]*):: ?(.*?) -->$", re.M)


def hide_props(body: str) -> str:
    """Block properties as HTML comments, for the mirrored file.

    They cannot simply be dropped: ``id::`` is what a ``((block reference))``
    from another note points at, and a file that came home without it would
    quietly break every reference into that page. So they are written as
    comments instead — invisible in anything that renders Markdown, still
    there in the text, and read back by :func:`show_props`.
    """
    return PROP_LINE_RE.sub(
        lambda m: f"{m.group(1)}<!-- {m.group(2)}:: {m.group(3)} -->", body)


def show_props(body: str) -> str:
    """The inverse. Unconditional on the way in: nobody writes that shape by
    hand, and someone who did meant a property."""
    return HIDDEN_PROP_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}:: {m.group(3)}", body)


class LinkMap:
    """Everything needed to translate Cairn's link syntaxes into ordinary
    Markdown links and back — built ONCE per round, not per note.

    ``[[Title]]``, ``((block-id))`` and ``{{embed ((block-id))}}`` mean nothing
    to anything but Cairn and Logseq, so in the mirrored folder they are
    written as relative links to the target's file. DEVONthink, Obsidian, a
    text editor and Quick Look all follow those.

    A reference keeps its id in the link's FRAGMENT (``#block-<id>``,
    ``#embed-<id>``). Other apps ignore a fragment they do not know and open
    the file anyway — and it is the only way the id survives the round trip.
    Drop it, and a block reference would come home as a plain note link, which
    is a quiet loss of what the writer actually said.
    """

    def __init__(self, user, paths: dict):
        from application.models.item import Item
        self.by_title: dict = {}      # lowercased title -> (pk, path)
        self.by_pk: dict = {}         # pk -> (title, path)
        self.by_path: dict = {}       # lowercased file path -> pk
        self.block_note: dict = {}    # block id -> pk
        self._labels: dict = {}       # block id -> its text, parsed on demand
        self._bodies: dict = {}       # pk -> body, for that parsing
        taken: set = set()
        for note in Item.objects(owner=user, kind="note").only(
                "pk", "title", "note", "parent", "block_ids"):
            path = path_for(note, paths, taken)
            taken.add(path)
            pk = str(note.pk)
            key = (note.title or "").strip().lower()
            if key and key not in self.by_title:
                self.by_title[key] = (pk, path)
            self.by_pk[pk] = (note.title or "", path)
            self.by_path[path.lower()] = pk
            self._bodies[pk] = note.note or ""
            for bid in (note.block_ids or []):
                self.block_note.setdefault(bid, pk)

    def label(self, block_id: str) -> str:
        """The referenced block's own text. Parsed only when a link needs it —
        most notes reference nothing, and parsing every body to find that out
        would be work done for nobody."""
        if block_id in self._labels:
            return self._labels[block_id]
        from application.services import block_refs, outline
        pk = self.block_note.get(block_id)
        text = ""
        if pk:
            block = outline.find(outline.parse(self._bodies.get(pk, "")), block_id)
            if block is not None:
                text = block_refs.label_for(block)
        self._labels[block_id] = text or (self.by_pk.get(pk, ("", ""))[0] if pk else "")
        return self._labels[block_id]

    def path_of(self, note) -> str:
        """Where this note's file goes — the one answer everything uses."""
        return self.by_pk.get(str(note.pk), ("", ""))[1]

    def rel(self, target_path: str, from_path: str) -> str:
        """``target`` as a link relative to the file holding it, URL-escaped."""
        here = posixpath.dirname(from_path or "")
        return quote(posixpath.relpath(target_path, here or "."), safe="/")

    def resolve(self, href: str, from_path: str):
        """A written link back to the note it points at, or None."""
        target = posixpath.normpath(
            posixpath.join(posixpath.dirname(from_path or ""), unquote(href)))
        return self.by_path.get(target.lstrip("./").lower())


def _is_relative_file(href: str) -> bool:
    """Is this link target a file lying beside the note, rather than an
    address? An absolute URL, an in-app path, a mail link or a bare fragment
    is none of our business, and another note's ``.md`` belongs to the wiki
    links (see :func:`body_from_file`)."""
    if not href or "://" in href or href.startswith(("/", "#", "?")):
        return False
    if href.split(":", 1)[0].lower() in ("mailto", "tel", "data", "file"):
        return False
    return not href.split("#", 1)[0].lower().endswith(MD)


def body_to_file(note, body: str, path: str, links=None) -> str:
    """In-app attachment URLs → the relative paths the mirrored copy uses, and
    Cairn's link syntaxes → ordinary Markdown links (see :class:`LinkMap`)."""
    for url, name, _data in _asset_names(note):
        body = body.replace(url, f"{ASSETS_DIR}/{note.pk}/{name}")
    if links is None:
        return body

    def block_link(block_id: str, kind: str) -> str:
        pk = links.block_note.get(block_id)
        if not pk:
            return ""
        title, target = links.by_pk[pk]
        label = links.label(block_id) or title or "block"
        return f"[{label}]({links.rel(target, path)}#{kind}-{block_id})"

    def wiki(m):
        target = (m.group(1) or "").strip()
        label = (m.group(2) or "").strip() or target
        hit = links.by_title.get(target.lower())
        # No such note: leave the wiki link alone rather than writing a link to
        # a file that is not there.
        return f"[{label}]({links.rel(hit[1], path)})" if hit else m.group(0)

    def app_link(m):
        """``](/notes/<id>)`` — an address inside this app, useless anywhere
        else. Same treatment: point it at the file."""
        hit = links.by_pk.get(m.group(1))
        return f"]({links.rel(hit[1], path)})" if hit else m.group(0)

    from application.services import block_refs, note_links
    # Embeds first: their text contains a plain reference.
    body = block_refs.EMBED_ANY_RE.sub(
        lambda m: block_link(m.group(1), "embed") or m.group(0), body)
    body = block_refs.BLOCKREF_RE.sub(
        lambda m: block_link(m.group(1), "block") or m.group(0), body)
    body = note_links.WIKILINK_RE.sub(wiki, body)
    return note_links.APP_LINK_RE.sub(app_link, body)


def body_from_file(note, body: str, path: str = "", links=None) -> str:
    """The reverse. Written loosely on purpose: an editor may have moved the
    file (and with it the number of ``../``), and the app only cares about the
    note id and file name."""
    body = show_props(body)
    pattern = re.compile(
        r"(?:\.\./)*" + re.escape(ASSETS_DIR) + r"/" + re.escape(str(note.pk))
        + r"/([^\s)\"']+)")

    by_name = {name: url for url, name, _d in _asset_names(note)}

    def repl(m):
        return by_name.get(m.group(1), m.group(0))

    body = pattern.sub(repl, body)
    if links is None:
        return body

    def unlink(m):
        label, href, frag = m.group(1), m.group(2), (m.group(3) or "")
        for kind, shape in (("embed-", "{{embed ((%s))}}"), ("block-", "((%s))")):
            if frag.startswith(kind):
                return shape % frag[len(kind):]
        pk = links.resolve(href, path)
        if not pk:
            return m.group(0)          # a link to something else: leave it be
        title = links.by_pk[pk][0]
        return f"[[{title}]]" if label == title else f"[[{title}|{label}]]"

    return FILE_LINK_RE.sub(unlink, body)


def render(note, path: str, links=None) -> str:
    """The complete file for ``note``: front matter, then the body."""
    from application.services import outline
    tags = ", ".join(note.tags or [])
    fmt = outline.format_of(note.note or "", note.note_format or "")
    head = [
        "---",
        f"cairn-id: {note.pk}",
        f"title: {note.title or 'Untitled note'}",
        f"tags: [{tags}]",
        # Which editor this note belongs in, written out so it can be CHANGED
        # from here: put ``format: markdown`` in a file and the note stops
        # being an outline on the next round. An outline stays an outline in
        # the file too — bullets and all — because that is what it is.
        f"format: {fmt}",
        f"created: {_stamp(note.created_at)}",
        f"updated: {_stamp(note.updated_at)}",
        "---",
        "",
    ]
    body = body_to_file(note, note.note or "", path, links)
    if fmt == "outline":
        body = hide_props(body)
    return "\n".join(head) + body


def _format_from(note, meta: dict) -> None:
    """Take ``format:`` off a file's front matter, if it says one of the two.

    This is how the choice is made from OUTSIDE: a file you rewrite as prose in
    a Markdown editor says so in its own front matter, and the note stops being
    an outline. A file that doesn't mention it leaves the note's setting alone
    — an older mirrored file has no ``format:`` line, and reading its absence
    as an instruction would flip every note on the first round.
    """
    from application.services import outline
    want = str(meta.get("format") or "").strip().lower()
    if want in outline.FORMATS:
        note.note_format = want


def parse(text: str) -> tuple[dict, str]:
    """``(front matter, body)``. A file without front matter is all body."""
    m = _FM_RE.match(text or "")
    if not m:
        return {}, text or ""
    meta = {}
    for line in m.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip('"')
        key = key.strip().lower()
        lm = _LIST_RE.match(value)
        if lm:
            meta[key] = [v.strip() for v in lm.group(1).split(",") if v.strip()]
        else:
            meta[key] = value
    return meta, text[m.end():]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def folder_paths(user, *, trashed=False) -> dict:
    """``{folder id: "Work/Acme"}`` for the user's notes folders.

    ``trashed=True`` gives the paths of the folders in the TRASH instead —
    where their directories still sit over there, waiting to be cleaned up.
    Ancestors are read from ``all_objects`` in that case, because a whole
    branch usually goes at once and a live-only lookup would lose the path
    halfway up.
    """
    manager = Item.all_objects if trashed else Item.objects
    folders = list(manager(owner=user, kind="folder", folder_domain="notes")
                   .only("pk", "title", "parent", "trashed_at"))
    if trashed:
        by_id_all = {str(f.pk): f for f in folders}
        folders = [f for f in folders if f.trashed_at]
    by_id = by_id_all if trashed else {str(f.pk): f for f in folders}
    out = {}
    for f in folders:
        parts, cur, seen = [], f, set()
        while cur is not None and str(cur.pk) not in seen:
            seen.add(str(cur.pk))
            parts.append(safe_name(cur.title))
            parent = cur.parent
            cur = by_id.get(str(parent.pk)) if parent else None
        out[str(f.pk)] = "/".join(reversed(parts))
    return out


def path_for(note, paths: dict, taken: set) -> str:
    """Where a note belongs, with same-named siblings kept apart."""
    parent = note.parent
    folder = paths.get(str(parent.pk), "") if parent else ""
    stem = safe_name(note.title or "Untitled note")
    candidate = f"{folder}/{stem}{MD}" if folder else f"{stem}{MD}"
    n = 2
    while candidate.lower() in taken:
        candidate = (f"{folder}/{stem} ({n}){MD}" if folder else f"{stem} ({n}){MD}")
        n += 1
    taken.add(candidate.lower())
    return candidate


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class MirrorStats:
    files_written: int = 0
    files_deleted: int = 0
    notes_created: int = 0
    notes_updated: int = 0
    notes_trashed: int = 0
    folders_created: int = 0
    folders_deleted: int = 0
    assets_written: int = 0
    assets_read: int = 0
    tasks_written: int = 0
    tasks_archived: int = 0
    conflicts: int = 0
    unchanged: int = 0
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "files_written": self.files_written,
            "files_deleted": self.files_deleted,
            "notes_created": self.notes_created,
            "notes_updated": self.notes_updated,
            "notes_trashed": self.notes_trashed,
            "folders_created": self.folders_created,
            "folders_deleted": self.folders_deleted,
            "assets_written": self.assets_written,
            "assets_read": self.assets_read,
            "tasks_written": self.tasks_written,
            "tasks_archived": self.tasks_archived,
            "conflicts": self.conflicts,
            "unchanged": self.unchanged,
            "errors": self.errors[:10],
        }

    def summary(self) -> str:
        return (f"{self.files_written} file(s) written, {self.files_deleted} removed; "
                f"{self.notes_created} note(s) created, {self.notes_updated} updated, "
                f"{self.notes_trashed} trashed"
                + (f"; {self.folders_deleted} empty folder(s) removed"
                   if self.folders_deleted else "")
                + (f"; {self.assets_read} attachment(s) taken from the share"
                   if self.assets_read else "")
                + (f"; {self.tasks_written} task file(s) written, "
                   f"{self.tasks_archived} archived"
                   if (self.tasks_written or self.tasks_archived) else "")
                + (f"; {self.conflicts} conflict(s)" if self.conflicts else ""))


def client_for(user) -> DavClient:
    """The configured client for a user, with the password decrypted."""
    from application.services.crypto import decrypt_for_user
    return DavClient(
        user.notes_mirror_url or "",
        user.notes_mirror_user or "",
        decrypt_for_user(user, user.notes_mirror_password or ""),
    )


# ---------------------------------------------------------------------------
# The round
# ---------------------------------------------------------------------------

def sync_user(user, client: DavClient | None = None, *, dry_run: bool = False,
              trigger: str = "manual") -> MirrorStats:
    """Run one two-way round for ``user``. Raises :class:`DavError` if the
    share can't be read — nothing is written in that case."""
    import time
    from application.services import sync_log

    started = time.monotonic()
    stats = MirrorStats()
    client = client or client_for(user)
    error = ""
    try:
        _Round(user, client, stats, dry_run).run()
        if not dry_run:
            user.last_notes_mirror_at = _utcnow()
            user.notes_mirror_status = stats.summary()
            user.save()
    except DavError as exc:
        error = str(exc)
        if not dry_run:
            user.notes_mirror_status = f"Failed: {exc}"
            user.save()
        raise
    finally:
        sync_log.record("notes-mirror", user=user, trigger=trigger, stats=stats,
                        error=error or ("; ".join(stats.errors[:3]) if stats.errors else ""),
                        seconds=time.monotonic() - started)
    return stats


class _Round:
    """One two-way pass. Holds the state the steps share — what the share
    contains, which files a note has claimed, and whether deleting is safe."""

    def __init__(self, user, client: DavClient, stats: MirrorStats, dry_run: bool):
        self.user = user
        self.client = client
        self.stats = stats
        self.dry_run = dry_run
        self.paths = folder_paths(user)
        # Where every note lives and which blocks it holds — built once, so a
        # note with links costs no queries of its own (see LinkMap).
        self.links = LinkMap(user, self.paths)
        self.handled: set[str] = set()

    # ---------- the pass ----------

    def run(self) -> None:
        # The tasks subtree belongs to tasks_mirror and is a copy, not a sync:
        # left in, every task file would be adopted here as a new note.
        remote = {e.path: e for e in self.client.walk("", skip=(ASSETS_DIR,))
                  if not tasks_mirror.owns(e.path)}
        self.files = {p: e for p, e in remote.items()
                      if not e.is_dir and p.lower().endswith(MD)}
        remote_dirs = {p for p, e in remote.items() if e.is_dir}

        notes = list(Item.objects(owner=self.user, kind="note"))

        # Deleting is the dangerous half. An empty listing while notes still
        # point at files there means "not mounted", not "the user deleted the
        # lot" — so this round reads and writes, but removes nothing.
        tracked = [n for n in notes if n.mirror_path]
        self.may_delete = bool(remote) or not tracked
        if not self.may_delete:
            self.stats.errors.append(
                "The share came back empty while notes still point at files "
                "there — nothing was deleted this round.")

        # Files no note claims, read once each: their front matter says whether
        # this is a note that moved over there or something new. Reading them
        # BEFORE the per-note pass is what makes a rename a rename — otherwise
        # the note's old path is simply missing and would look like a deletion.
        claimed = {n.mirror_path for n in notes if n.mirror_path}
        # A trashed note still owns its file until the deletion pass removes it.
        # Adopting it in between would resurrect the note from its own corpse.
        doomed = list(Item.all_objects(owner=self.user, kind="note",
                                       trashed_at__ne=None, mirror_path__ne="")
                      .only("pk", "mirror_path"))
        doomed_paths = {n.mirror_path for n in doomed if n.mirror_path}
        doomed_ids = {str(n.pk) for n in doomed}
        self.loose = {}
        self.by_id = {}
        for path in sorted(set(self.files) - claimed - doomed_paths):
            text = self.client.read(path).decode("utf-8", "replace")
            meta, body = parse(text)
            self.loose[path] = (meta, body, text)
            note_id = (meta.get("cairn-id") or "").strip()
            if note_id in doomed_ids:
                del self.loose[path]
                continue
            if note_id:
                self.by_id.setdefault(note_id, path)

        # The folder tree, so an empty folder is still visible over there.
        for folder_path in sorted(set(self.paths.values())):
            if folder_path and folder_path not in remote_dirs:
                if not self.dry_run:
                    self.client.makedirs(folder_path)
                self.stats.folders_created += 1

        for note in notes:
            # The map already worked out where every note goes; asking
            # path_for a second time would run the same-name disambiguation
            # over again and could land on a different answer, which is a link
            # pointing at the wrong file.
            want = self.links.path_of(note) or path_for(note, self.paths, set())
            try:
                self.one(note, want)
            except DavError as exc:
                self.stats.errors.append(f"{note.title}: {exc}")

        for path in sorted(set(self.loose) - self.handled):
            try:
                self.adopt(path, *self.loose[path])
            except DavError as exc:
                self.stats.errors.append(f"{path}: {exc}")

        self.drop_files_of_trashed_notes()
        # After the files, so a folder that just lost its last one is empty by
        # the time this looks.
        self.drop_dirs_of_trashed_folders()

        # Tasks last: their folder is written, never read, so nothing above
        # depends on it — and a failure there must not cost the notes round.
        if self.user.notes_mirror_tasks:
            self.mirror_tasks()

    # ---------- one note ----------

    def one(self, note, want_path: str) -> None:
        local_text = render(note, want_path, self.links)
        local_hash = _hash(local_text)
        path = note.mirror_path or ""
        entry = self.files.get(path) if path else None
        moved = False

        if entry is None:
            # Not where we left it. Did it move over there, or is a file
            # already sitting where this note belongs?
            elsewhere = self.by_id.get(str(note.pk))
            if elsewhere:
                path, entry, moved = elsewhere, self.files[elsewhere], True
            elif not path and want_path in self.files:
                path, entry = want_path, self.files[want_path]

        if entry is not None:
            self.handled.add(path)

        if entry is None:
            if not path:                       # never mirrored → create it
                self.write(note, want_path, local_text)
                return
            if not self.may_delete:            # share unreadable: leave it be
                return
            self.trash(note)                   # the file was deleted there
            return

        # The file exists. Which side moved?
        if not moved and entry.tag and entry.tag == note.mirror_tag:
            remote_text = None                 # untouched since we wrote it
        elif path in self.loose:
            remote_text = self.loose[path][2]     # already read this round
        else:
            remote_text = self.client.read(path).decode("utf-8", "replace")

        remote_changed = (remote_text is not None
                          and _hash(remote_text) != note.mirror_hash)
        local_changed = local_hash != note.mirror_hash

        # A link the note still carries to a file lying over there. Normally
        # that is a pull's business — but a note whose file has been read once
        # already matches its hash, and nothing would ever look again. So it is
        # asked here too, on the way past, and answering it is simply a change
        # on THIS side: the branches below write it out like any other edit.
        # Not when the file is winning anyway (``moved``, ``remote_changed``):
        # that path absorbs on its own, and doing it twice would fetch the same
        # picture twice.
        if not moved and not remote_changed:
            absorbed = self.absorb_assets(note, note.note or "", path)
            if absorbed != (note.note or ""):
                note.note = absorbed
                if not self.dry_run:
                    note.save()
                local_text = render(note, want_path, self.links)
                local_hash = _hash(local_text)
                local_changed = True

        if moved and not (remote_changed and local_changed):
            # The file moved or was renamed over there. That is the file
            # winning — location, name and text — which is what dragging a
            # document into another folder means.
            self.apply_remote(note, path, remote_text, moved=True, entry=entry)
            return

        if not remote_changed and not local_changed:
            if path != want_path:              # renamed in Cairn, nothing else
                self.move(note, path, want_path, local_text)
            else:
                if not self.dry_run:
                    self.remember(note, path, note.mirror_hash, entry.tag)
                self.stats.unchanged += 1
            return

        if remote_changed and local_changed:
            self.conflict(note, path, remote_text)
            self.write(note, want_path, local_text)
            if path != want_path and not self.dry_run:
                self.client.delete(path)
            return

        if remote_changed:
            self.apply_remote(note, path, remote_text, moved=moved, entry=entry)
            return

        if path != want_path:
            self.move(note, path, want_path, local_text)
        else:
            self.write(note, want_path, local_text)

    # ---------- the individual moves ----------

    def write(self, note, path: str, text: str) -> None:
        """Write the file and whatever it embeds, then remember what we wrote."""
        if self.dry_run:
            self.stats.files_written += 1
            return
        etag = self.client.write(path, text.encode("utf-8"),
                                 "text/markdown; charset=utf-8")
        self.stats.files_written += 1
        self.write_assets(note, path)
        if not etag:
            # No ETag on the PUT — ask for the file's validator, or the next
            # round would fetch the whole file just to find it unchanged.
            entry = self._stat(path)
            etag = entry.tag if entry else ""
        self.remember(note, path, _hash(text), etag)

    @staticmethod
    def remember(note, path: str, digest: str, tag: str) -> None:
        """Record what the file now holds — with an atomic update, not a save.

        ``Item.save`` stamps ``updated_at``, and bookkeeping is not an edit:
        stamping it here would make the note look changed on the next round,
        which would push the file again, which would stamp it again."""
        note.mirror_path, note.mirror_hash, note.mirror_tag = path, digest, tag
        Item.all_objects(pk=note.pk).update(
            set__mirror_path=path, set__mirror_hash=digest, set__mirror_tag=tag)

    def write_assets(self, note, path: str) -> None:
        """Put the note's attachments beside it, and only those. Deleting the
        extras is bounded to this note's own ``_assets/<id>`` folder, so an
        image the user removed from the note stops taking up space on the NAS
        while nothing else can ever be caught by it."""
        assets = _asset_names(note)
        target = assets_dir(path, note.pk)
        have = {e.path.rsplit("/", 1)[-1]: e
                for e in self._list(target) if not e.is_dir}
        if not assets:
            if have:
                self.client.delete(target)
            return
        for _url, name, data in assets:
            entry = have.pop(name, None)
            if entry is None or entry.size != len(data):
                self.client.write(f"{target}/{name}", data)
                self.stats.assets_written += 1
        for name in have:                       # no longer part of the note
            self.client.delete(f"{target}/{name}")

    # ---------- attachments coming home ----------

    def body_in(self, note, body: str, path: str) -> str:
        """A file's body as the note stores it: the attachments added over
        there pulled in first, then the links translated."""
        if not self.dry_run and note.pk:
            body = self.absorb_assets(note, body, path)
        return body_from_file(note, body, path, self.links)

    def absorb_assets(self, note, body: str, path: str) -> str:
        """Files the note does not own yet, taken into it.

        An image dropped into a mirrored document is two things over there: a
        file somewhere beside it and a relative link in the text. Neither
        means anything here — the link resolves to nothing in the app, and
        :meth:`write_assets` would delete the file on the next round if it sat
        in this note's own folder. So the bytes are fetched and stored on the
        note, exactly like a paste in the browser, and the link is rewritten
        to the in-app URL. From then on it is a normal attachment.

        Which folder the file sits in is deliberately not part of the rule.
        DEVONthink writes ``_resources/``, Obsidian an attachments folder of
        the vault's choosing, Logseq ``assets/``, and this app
        ``_assets/<note id>/`` — a subfolder beside the document is the
        convention, and the link is what all of them agree on. So any
        RELATIVE target that is not another note's ``.md`` is treated as an
        attachment: it is looked up on the share first, and one that isn't
        there is left exactly as it was.
        """
        known = {name for _url, name, _data in _asset_names(note)}
        here = posixpath.dirname(path or "")
        seen: dict[str, str] = {}

        def repl(m):
            href = m.group(1)
            if href in seen:
                return m.group(0).replace(href, seen[href])
            if not _is_relative_file(href):
                return m.group(0)
            rel = unquote(href)
            if posixpath.basename(rel) in known:
                return m.group(0)      # ours: body_from_file maps it back
            remote = posixpath.normpath(posixpath.join(here, rel))
            if remote.startswith(".."):
                return m.group(0)      # above the share: not ours to read
            entry = self._probe(remote)
            if entry is None:
                # Nothing of that name over there. A link into another app, or
                # one that was already broken — either way not ours to repair,
                # and not worth a line in the log every round.
                return m.group(0)
            url = self.store_asset(note, posixpath.basename(rel), entry, remote)
            if not url:
                return m.group(0)
            seen[href] = url
            self.stats.assets_read += 1
            return m.group(0).replace(href, url)

        return LINK_TARGET_RE.sub(repl, body)

    def store_asset(self, note, name: str, entry, remote: str) -> str:
        """One file from the share as an inline attachment. Returns its in-app
        URL, or "" when it does not fit — the same 16 MB document cap the
        upload endpoints guard, answered here by leaving the link pointing at
        the file on the share. The size is checked BEFORE the download, so a
        note is never asked to swallow a video."""
        import secrets
        from application.models.item import ITEM_MAX_BYTES, NoteFile, NoteImage

        if entry.size and entry.size > ITEM_MAX_BYTES:
            self.stats.errors.append(
                f"{note.title}: '{name}' is too large for a note ("
                f"{entry.size // 1024 // 1024} MB).")
            return ""
        try:
            data = self.client.read(remote)
        except DavError as exc:
            self.stats.errors.append(f"{remote}: {exc}")
            return ""
        # The paste endpoint's own sniffing: an image is rendered inline,
        # anything else becomes a download link (and SVG is not an image here).
        from application.views.tasks import _safe_attachment_name, _sniff_image_type

        token = secrets.token_urlsafe(12)
        ctype = _sniff_image_type(data[:12])
        if ctype:
            note.note_images.append(
                NoteImage(token=token, content_type=ctype, data=data))
            url = f"/notes/{note.pk}/note-image/{token}"
        else:
            guessed, _enc = mimetypes.guess_type(name)
            note.note_files.append(NoteFile(
                token=token, filename=_safe_attachment_name(name),
                content_type=guessed or "application/octet-stream", data=data))
            url = f"/notes/{note.pk}/note-file/{token}"
        if note.storage_bytes() > ITEM_MAX_BYTES:
            (note.note_images if ctype else note.note_files).pop()
            self.stats.errors.append(
                f"{note.title}: no room for '{name}' — the note is full.")
            return ""
        return url

    def move(self, note, old_path: str, new_path: str, text: str) -> None:
        self.write(note, new_path, text)
        if not self.dry_run and old_path and old_path != new_path:
            self.client.delete(old_path)
            old_assets = assets_dir(old_path, note.pk)
            if old_assets != assets_dir(new_path, note.pk):
                self.client.delete(old_assets)
            self.stats.files_deleted += 1

    def trash(self, note) -> None:
        from application.services.trash import trash_item
        if not self.dry_run:
            self.remember(note, "", "", "")
            trash_item(note)
        self.stats.notes_trashed += 1

    def conflict(self, note, path: str, remote_text: str) -> None:
        """Park the file's version beside it. It comes back as its own note on
        the next round, which is how both texts stay reachable without merging
        either one."""
        stamp = _utcnow().strftime("%Y-%m-%d %H%M")
        stem = path[: -len(MD)] if path.lower().endswith(MD) else path
        meta, body = parse(remote_text)
        title = (meta.get("title") or note.title or "").strip()
        head = f"---\ntitle: {title} (conflict {stamp})\n---\n\n"
        if not self.dry_run:
            self.client.write(f"{stem} (conflict {stamp}){MD}",
                              (head + body).encode("utf-8"),
                              "text/markdown; charset=utf-8")
        self.stats.conflicts += 1

    def apply_remote(self, note, path: str, remote_text: str, *, moved=False,
                     entry=None) -> None:
        """The file won: take its text (title, tags, body) into the note, then
        write the note back so the file carries current front matter again."""
        meta, body = parse(remote_text)
        note.note = self.body_in(note, body, path)
        _format_from(note, meta)
        self._title_from(note, meta, path)
        tags = meta.get("tags")
        if isinstance(tags, list):
            note.tags = [t for t in tags if t]
        if moved:
            self.refile(note, path)
        if self.dry_run:
            self.stats.notes_updated += 1
            return
        from application.services import note_links
        if "[[" in (note.note or "") or note._data.get("note_links"):
            note_links.rebuild(note, note_links.title_index(self.user))
        was_at = note.mirror_path or ""
        note.save()
        # When the file was last touched, not when this round noticed. The edit
        # happened over there, and stamping it "now" is how a note edited in
        # DEVONthink last week came home at the top of the recently-changed
        # list — and how an imported archive lost its dates altogether.
        self.retime(note, updated=self.remote_time(entry, meta))
        self.stats.notes_updated += 1
        # Write back: the body came from there, but ``updated`` moved, and a
        # stale one would read as a change next round and push for nothing.
        self.write(note, path, render(note, path, self.links))
        self.stats.files_written -= 1          # the same file, not a second
        if was_at and assets_dir(was_at, note.pk) != assets_dir(path, note.pk):
            # It was dragged into another folder over there; its attachments
            # have just been written beside it, so the old copies can go.
            self.client.delete(assets_dir(was_at, note.pk))

    def adopt(self, path: str, meta: dict, body: str, _text: str) -> None:
        """A file no note claimed and whose id we don't know: import it."""
        stem = path.rsplit("/", 1)[-1][: -len(MD)]
        title = (meta.get("title") or "").strip() or stem
        note = Item(owner=self.user, kind="note", title=title)
        _format_from(note, meta)
        tags = meta.get("tags")
        if isinstance(tags, list):
            note.tags = [t for t in tags if t]
        if self.dry_run:
            self.stats.notes_created += 1
            return
        note.parent = self.folder_for(path)
        note.save()
        # The body last, and after the first save: an attachment is addressed
        # by the note's id, so there has to be one before its links can be
        # written. Links to other notes' files become [[wiki links]] here too,
        # the same way they do when a tracked file wins a round.
        note.note = self.body_in(note, body, path)
        note.save()
        updated = self.remote_time(self.files.get(path), meta)
        # ``created:`` is the only place a file's real age survives; the share's
        # modification time is not one (a copied file is modified today).
        self.retime(note, created=_parse_dt(meta.get("created")) or updated,
                    updated=updated)
        self.stats.notes_created += 1
        # Write it back so the file gains its cairn-id and is tracked from now
        # on — at the path it already has, so nothing moves under the user.
        self.write(note, path, render(note, path, self.links))
        self.stats.files_written -= 1

    def drop_dirs_of_trashed_folders(self) -> None:
        """A notes folder that went to the trash leaves its directory behind
        over there. Take it — but only when it is really empty.

        Deleting directories is where a mirror can do actual damage, so the
        rule is deliberately narrow, and every part of it has to hold:

        * the path is one THIS app built, for a folder that is now trashed —
          never a directory somebody made over there;
        * nothing is inside it but the ``_assets`` tree we put there ourselves.
          A single file, or one folder of the user's own, and it stays;
        * the share answered a listing at all (``may_delete``), the same guard
          that stops an empty or failed listing from deleting anything;
        * and it is asked of the SHARE, now — not of the listing this round
          started with, which is older than the files just deleted.

        Deepest first, so a whole trashed branch collapses in one round.
        """
        if not self.may_delete:
            return
        paths = folder_paths(self.user, trashed=True).values()
        for path in sorted({p for p in paths if p}, key=lambda p: p.count("/"),
                           reverse=True):
            try:
                if not self._only_our_leftovers(path):
                    continue
                if not self.dry_run:
                    self.client.delete(path)
                self.stats.folders_deleted += 1
            except DavError as exc:
                if exc.status == 404:
                    continue      # already gone over there: nothing to clean up
                self.stats.errors.append(f"{path}: {exc}")

    def _only_our_leftovers(self, path: str) -> bool:
        """Is there nothing under ``path`` but the ``_assets`` tree?"""
        for entry in self.client.walk(path):
            rest = entry.path[len(path):].strip("/") if entry.path.startswith(path) \
                else entry.path
            first = rest.split("/", 1)[0]
            if not entry.is_dir:
                return False                 # a file: leave the whole thing
            if first != ASSETS_DIR:
                return False                 # someone else's folder
        return True

    def drop_files_of_trashed_notes(self) -> None:
        if not self.may_delete:
            return
        for note in Item.all_objects(owner=self.user, kind="note",
                                     trashed_at__ne=None, mirror_path__ne=None):
            if not note.mirror_path:
                continue
            try:
                if not self.dry_run:
                    self.client.delete(note.mirror_path)
                    self.client.delete(assets_dir(note.mirror_path, note.pk))
                    self.remember(note, "", "", "")
                self.stats.files_deleted += 1
            except DavError as exc:
                self.stats.errors.append(f"{note.mirror_path}: {exc}")

    def mirror_tasks(self) -> None:
        """The tasks half of the round (see :mod:`tasks_mirror`).

        ``Tasks`` at the root is that module's; a notes folder of the same name
        would have both halves writing into one directory, so the notes tree
        keeps it and the tasks are left out with a word about why."""
        if tasks_mirror.TASKS_DIR in {p.split("/", 1)[0] for p in self.paths.values()}:
            self.stats.errors.append(
                f"A notes folder called '{tasks_mirror.TASKS_DIR}' sits at the root "
                "of the share — rename it to mirror tasks as well.")
            return
        try:
            tasks_mirror.sync(self)
        except DavError as exc:
            self.stats.errors.append(f"{tasks_mirror.TASKS_DIR}: {exc}")

    # ---------- helpers ----------

    @staticmethod
    def remote_time(entry, meta: dict):
        """When the file says it changed: the share's own Last-Modified first.

        An editor that rewrites a document does not rewrite the ``updated:``
        line we once put in its front matter, so that header is the older and
        less honest of the two. It is the fallback for a share that answers no
        modification time at all.
        """
        return (_parse_dt(getattr(entry, "mtime", "")) if entry is not None
                else None) or _parse_dt(meta.get("updated"))

    def retime(self, note, *, created=None, updated=None) -> None:
        """Put the file's timestamps on the note — with an atomic update.

        ``Item.save`` stamps ``updated_at`` itself, so this has to come after
        the save and go around it, the same way ``remember`` does."""
        fields = {}
        if created:
            note.created_at = created
            fields["set__created_at"] = created
        if updated:
            note.updated_at = updated
            fields["set__updated_at"] = updated
        if fields and not self.dry_run:
            Item.all_objects(pk=note.pk).update(**fields)

    def _title_from(self, note, meta: dict, path: str) -> None:
        """Whichever of the two names the user actually changed wins: the
        ``title:`` line if it moved, else the file name (renaming the file is
        how you rename a document in a file manager)."""
        fm_title = (meta.get("title") or "").strip()
        stem = path.rsplit("/", 1)[-1][: -len(MD)]
        if fm_title and fm_title != (note.title or ""):
            note.title = fm_title
        elif stem and stem != safe_name(note.title or ""):
            note.title = stem

    def refile(self, note, path: str) -> None:
        """Point a note at the notes folder its file now lives in."""
        parent = self.folder_for(path)
        current = note.parent
        if (str(current.pk) if current else None) != (str(parent.pk) if parent else None):
            note.parent = parent

    def folder_for(self, path: str):
        """The notes folder matching a file's directory, created if missing."""
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
        if not directory:
            return None
        by_path = {p: fid for fid, p in self.paths.items()}
        parent = None
        walked = ""
        for part in directory.split("/"):
            walked = f"{walked}/{part}" if walked else part
            fid = by_path.get(walked)
            if fid:
                parent = Item.objects(pk=fid).first()
                continue
            folder = Item.objects(owner=self.user, kind="folder",
                                  folder_domain="notes", parent=parent,
                                  title=part).first()
            if folder is None:
                if self.dry_run:
                    return parent
                folder = Item(owner=self.user, kind="folder",
                              folder_domain="notes", parent=parent,
                              title=part, status="active").save()
                self.stats.folders_created += 1
            self.paths[str(folder.pk)] = walked
            by_path[walked] = str(folder.pk)
            parent = folder
        return parent

    def _list(self, path):
        try:
            return self.client.list(path)
        except DavError:
            return []

    def _probe(self, path: str):
        """The share's entry for one file, from a listing kept for the round.

        Absorbing asks this before it downloads: it answers whether the target
        exists at all and how big it is, and a folder full of images costs one
        PROPFIND rather than one per picture."""
        cache = self.__dict__.setdefault("_listings", {})
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent not in cache:
            cache[parent] = {e.path: e for e in self._list(parent) if not e.is_dir}
        return cache[parent].get(path)

    def _stat(self, path):
        """The entry for ``path``, read from its parent listing."""
        parent, _, name = path.rpartition("/")
        for entry in self._list(parent):
            if entry.path == path or entry.path.rsplit("/", 1)[-1] == name:
                return entry
        return None


def due_user_ids(now=None) -> list:
    """Users whose mirror is due — the broker-agnostic half, same shape as
    ``zammad_auto.due_user_ids`` so the Celery task and the CLI agree."""
    from datetime import timedelta
    from application.models.user import User

    now = now or _utcnow()
    out = []
    for user in User.objects(notes_mirror_enabled=True).only(
            "pk", "notes_mirror_url", "notes_mirror_interval", "last_notes_mirror_at"):
        if not user.notes_mirror_url:
            continue
        every = max(int(user.notes_mirror_interval or 0), 0)
        if every <= 0:
            continue                    # manual only
        last = user.last_notes_mirror_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is None or last + timedelta(minutes=every) <= now:
            out.append(str(user.pk))
    return out
