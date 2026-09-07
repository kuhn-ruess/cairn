"""A small WebDAV client — enough of the protocol to mirror a folder.

Written on stdlib ``urllib`` like the rest of the outbound HTTP here, and
through :func:`safe_urlopen`, because the base URL comes from the user (same
threat model as the Zammad address).

Only what a file mirror needs: walk a collection, read a file, write a file,
delete, create collections. Depth-1 PROPFIND recursion rather than
``Depth: infinity``, which plenty of servers (Synology among them) refuse.
"""
from __future__ import annotations

import base64
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree

DAV_NS = "{DAV:}"

_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/><d:getlastmodified/><d:getetag/>"
    "</d:prop></d:propfind>"
).encode()


class DavError(Exception):
    """A WebDAV request failed. ``status`` is the HTTP code, 0 for transport
    failures (DNS, TLS, timeout, blocked address)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


@dataclass
class DavEntry:
    """One member of a collection, as a path relative to the client's root."""
    path: str
    is_dir: bool
    size: int = 0
    etag: str = ""
    mtime: str = ""

    @property
    def tag(self) -> str:
        """The cheap change validator: the ETag if the server sends one, else
        modification time and size. Only ever compared against itself."""
        return self.etag or f"{self.mtime}|{self.size}"


def _encode_path(path: str) -> str:
    """Percent-encode each segment; the separators must survive."""
    return "/".join(quote(seg, safe="") for seg in path.split("/") if seg != "")


class DavClient:
    """Talks to one WebDAV collection. Every ``path`` is relative to it."""

    def __init__(self, base_url: str, username: str = "", password: str = "",
                 timeout: int = 30):
        self.base_url = (base_url or "").rstrip("/")
        if not self.base_url:
            raise DavError("no WebDAV URL configured")
        self.timeout = timeout
        self._auth = ""
        if username or password:
            raw = f"{username}:{password}".encode()
            self._auth = "Basic " + base64.b64encode(raw).decode()
        # The base URL's own path, so hrefs coming back can be made relative.
        self._root_path = urlparse(self.base_url).path.rstrip("/")

    # ---------- plumbing ----------

    def _url(self, path: str) -> str:
        enc = _encode_path(path)
        return f"{self.base_url}/{enc}" if enc else self.base_url

    def _open(self, req):
        """``safe_urlopen``, unless the deployment says its target is on the
        local network. A NAS reached over a VPN or a LAN address fails the SSRF
        guard for a good reason, so opening that door is a deliberate,
        per-deployment decision (``BKTODO_MIRROR_ALLOW_PRIVATE=1``) rather than
        something this client decides on its own."""
        from application.services.http_safety import safe_urlopen
        try:
            from flask import current_app, has_app_context
            allow_private = bool(
                has_app_context() and current_app.config.get("MIRROR_ALLOW_PRIVATE"))
        except Exception:
            allow_private = False
        if allow_private:
            return urllib.request.urlopen(req, timeout=self.timeout)
        return safe_urlopen(req, timeout=self.timeout)

    def _request(self, method: str, path: str, *, body: bytes | None = None,
                 headers: dict | None = None, expect=(200, 201, 204, 207)):
        from application.services.http_safety import UnsafeURLError

        req = urllib.request.Request(self._url(path), data=body, method=method)
        if self._auth:
            req.add_header("Authorization", self._auth)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with self._open(req) as resp:
                if resp.status not in expect:
                    raise DavError(f"{method} {path}: HTTP {resp.status}", resp.status)
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            if exc.code in expect:
                return exc.code, exc.read(), dict(exc.headers)
            raise DavError(f"{method} {path or '/'}: HTTP {exc.code} {exc.reason}",
                           exc.code) from exc
        except UnsafeURLError as exc:
            raise DavError(f"{method} {path or '/'}: {exc}") from exc
        except urllib.error.URLError as exc:
            raise DavError(f"{method} {path or '/'}: {exc.reason}") from exc
        except OSError as exc:  # timeouts, TLS, connection reset
            raise DavError(f"{method} {path or '/'}: {exc}") from exc

    def _relative(self, href: str) -> str:
        """An href from a multistatus response as a path below our root."""
        path = unquote(urlparse(href).path)
        if self._root_path and path.startswith(self._root_path):
            path = path[len(self._root_path):]
        return path.strip("/")

    # ---------- operations ----------

    def list(self, path: str = "") -> list[DavEntry]:
        """Direct members of a collection (the collection itself excluded)."""
        _status, body, _headers = self._request(
            "PROPFIND", path, body=_PROPFIND_BODY,
            headers={"Depth": "1", "Content-Type": 'text/xml; charset="utf-8"'},
        )
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise DavError(f"PROPFIND {path or '/'}: unreadable response: {exc}") from exc

        me = path.strip("/")
        out: list[DavEntry] = []
        for resp in root.findall(f"{DAV_NS}response"):
            href = resp.findtext(f"{DAV_NS}href") or ""
            rel = self._relative(href)
            if rel == me:
                continue                       # the collection itself
            prop = resp.find(f"{DAV_NS}propstat/{DAV_NS}prop")
            if prop is None:
                continue
            is_dir = prop.find(f"{DAV_NS}resourcetype/{DAV_NS}collection") is not None
            try:
                size = int(prop.findtext(f"{DAV_NS}getcontentlength") or 0)
            except ValueError:
                size = 0
            out.append(DavEntry(
                path=rel, is_dir=is_dir, size=size,
                etag=(prop.findtext(f"{DAV_NS}getetag") or "").strip('"'),
                mtime=(prop.findtext(f"{DAV_NS}getlastmodified") or ""),
            ))
        return out

    def walk(self, path: str = "", *, skip=()) -> list[DavEntry]:
        """Every file and collection below ``path``, depth first.

        ``skip`` names folders to step over **wherever they appear** — the
        mirror's assets folders sit beside the notes they belong to, and their
        contents are checked per note rather than diffed wholesale."""
        found: list[DavEntry] = []
        queue = [path]
        while queue:
            current = queue.pop()
            for entry in self.list(current):
                if entry.path.rsplit("/", 1)[-1] in skip:
                    continue
                found.append(entry)
                if entry.is_dir:
                    queue.append(entry.path)
        return found

    def read(self, path: str) -> bytes:
        _status, body, _headers = self._request("GET", path, expect=(200,))
        return body

    def write(self, path: str, data: bytes,
              content_type="application/octet-stream") -> str:
        """Write a file. Returns the new ETag when the server sends one, so the
        caller can record what it wrote without a second request."""
        self.makedirs(path.rsplit("/", 1)[0] if "/" in path else "")
        _status, _body, headers = self._request(
            "PUT", path, body=data, headers={"Content-Type": content_type},
            expect=(200, 201, 204))
        return (headers.get("ETag") or headers.get("Etag") or "").strip('"')

    def move(self, src: str, dst: str, *, overwrite: bool = False):
        """MOVE a resource inside the share. The Destination header wants an
        absolute URL, and the target's parents have to exist first."""
        parent = dst.rsplit("/", 1)[0] if "/" in dst else ""
        if parent:
            self.makedirs(parent)
        self._request("MOVE", src, headers={
            "Destination": self._url(dst),
            "Overwrite": "T" if overwrite else "F",
        }, expect=(200, 201, 204))

    def delete(self, path: str):
        # 404 is success as far as the mirror is concerned: gone is gone.
        self._request("DELETE", path, expect=(200, 202, 204, 404))

    def makedirs(self, path: str):
        """Create ``path`` and any missing parent collection. MKCOL on an
        existing collection answers 405, which is exactly what we want to
        ignore."""
        parts = [p for p in (path or "").split("/") if p]
        for i in range(len(parts)):
            self._request("MKCOL", "/".join(parts[: i + 1]),
                          expect=(200, 201, 204, 405))

    def check(self) -> str:
        """Probe the configured collection. Returns a short human answer for
        the settings page; raises :class:`DavError` if it isn't usable."""
        entries = self.list("")
        files = sum(1 for e in entries if not e.is_dir)
        dirs = sum(1 for e in entries if e.is_dir)
        return f"Reachable — {files} file(s) and {dirs} folder(s) at the root."
