"""Commits registered on a task (``Item.commits``) and their web links.

A link is found in this order: the entry's own ``url`` (sent by the caller,
or derived from the ``remote`` it sent), then ``COMMIT_URL_TEMPLATES`` by
repo name (case-insensitive). Without either the hash is shown as copyable
text. A mapping value is either a template holding ``{sha}`` or a remote
(``git@github.com:org/repo.git``, ``https://gitlab.example.com/g/repo``),
which ``commit_template`` turns into one.
"""
import re
from urllib.parse import urlsplit

from flask import current_app

SHA_RE = re.compile(r"^[0-9a-f]{4,64}$")
_SCP_RE = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)$")


def web_base(remote: str) -> str | None:
    """``https://host/org/repo`` for a git remote, or None when it isn't one
    a browser can open (a local path, a bundle). Credentials and ports of an
    ssh remote never reach the link."""
    r = (remote or "").strip()
    if not r:
        return None
    host = path = None
    if "://" in r:
        parts = urlsplit(r)
        if parts.scheme not in ("http", "https", "ssh", "git", "git+ssh"):
            return None
        host = parts.hostname
        path = parts.path
        scheme = parts.scheme if parts.scheme in ("http", "https") else "https"
        # An http(s) remote may carry a real web port; an ssh port is not one.
        if parts.scheme in ("http", "https") and parts.port:
            host = f"{host}:{parts.port}"
    else:
        m = _SCP_RE.match(r)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
        scheme = "https"
    path = (path or "").strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or "/" not in path:
        return None
    return f"{scheme}://{host}/{path}"


def commit_template(value: str) -> str | None:
    """A ``{sha}`` URL template from a mapping value (a template already, or
    a remote)."""
    v = (value or "").strip()
    if not v:
        return None
    if "{sha}" in v:
        return v if v.startswith(("https://", "http://")) else None
    base = web_base(v)
    if not base:
        return None
    if urlsplit(base).hostname == "bitbucket.org":
        return base + "/commits/{sha}"
    # GitHub, GitLab, Gitea/Forgejo all answer /commit/<sha>.
    return base + "/commit/{sha}"


def _mapping() -> dict:
    raw = current_app.config.get("COMMIT_URL_TEMPLATES") or {}
    return {str(k).lower(): v for k, v in raw.items() if v}


def commit_url(entry) -> str | None:
    """The web page of one registered commit, or None."""
    if entry.url:
        return entry.url
    tpl = commit_template(_mapping().get((entry.repo or "").lower(), ""))
    return tpl.replace("{sha}", entry.sha) if tpl else None


def _same(a: str, b: str) -> bool:
    """One hash names the other (a short sha is a prefix of the full one)."""
    return a.startswith(b) or b.startswith(a)


def parse_commits(entries):
    """Check what a caller sent: ``(clean, error)``. ``clean`` is a list of
    ``(repo, sha, subject, url)``; on an error nothing of it is usable."""
    if not isinstance(entries, list) or not entries:
        return None, "commits must be a non-empty list"
    clean = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            return None, f"commits[{i}] must be an object"
        repo = str(e.get("repo") or "").strip()
        sha = str(e.get("sha") or "").strip().lower()
        if not repo:
            return None, f"commits[{i}].repo is required"
        if not SHA_RE.match(sha):
            return None, f"commits[{i}].sha is not a commit hash"
        url = str(e.get("url") or "").strip() or None
        if url and not url.startswith(("https://", "http://")):
            return None, f"commits[{i}].url must be http(s)"
        if not url and e.get("remote"):
            tpl = commit_template(str(e["remote"]))
            url = tpl.replace("{sha}", sha) if tpl else None
        subject = str(e.get("subject") or "").strip()[:500] or None
        clean.append((repo[:200], sha, subject, url))
    return clean, None


def apply_commits(task, user, clean, comment_id=None) -> int:
    """Put checked entries (``parse_commits``) on ``task`` and save it;
    returns how many were new. A (repo, sha) already there is not added
    twice; a longer hash, a subject or a link the stored one lacked is
    filled in instead."""
    from application.models.item import CommitRef

    added = 0
    for repo, sha, subject, url in clean:
        old = next((c for c in task.commits
                    if c.repo.lower() == repo.lower() and _same(c.sha, sha)), None)
        if old is None:
            task.commits.append(CommitRef(
                repo=repo, sha=sha, subject=subject, url=url,
                comment_id=comment_id or None, author=user))
            added += 1
            continue
        if len(sha) > len(old.sha):
            old.sha = sha
        old.subject = old.subject or subject
        old.url = old.url or url
        old.comment_id = old.comment_id or comment_id or None
    task.save()
    return added


def add_commits(task, user, entries, comment_id=None):
    """``parse_commits`` + ``apply_commits``: ``(added, error)``, nothing
    stored on an error. ``comment_id`` must name a comment of this task."""
    if comment_id and not any(c.uid == comment_id for c in task.comments or []):
        return 0, "unknown comment_id"
    clean, error = parse_commits(entries)
    if error:
        return 0, error
    return apply_commits(task, user, clean, comment_id), None
