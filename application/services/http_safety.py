"""Helpers that guard outbound HTTP from server-side request forgery.

User-supplied URLs (iCal subscriptions, Zammad instance address, future
webhook targets) must not be allowed to point at internal services
(metadata endpoints, internal management interfaces, file://, etc.).

``validate_outbound_url`` performs scheme + host checks and resolves DNS
to make sure the hostname doesn't map to a private / loopback / reserved
range. There's still a small TOCTOU window between the resolution we
check and the resolution urllib does — for an extra-paranoid setup the
caller could pin the resolved IP, but the time window is short and the
attacker would need active DNS-rebinding control.

Validating the URL alone is NOT enough, which is why ``safe_urlopen``
exists and callers must use it instead of ``urllib.request.urlopen``:
urlopen follows 3xx redirects on its own, so a public host that passes
the check can answer ``302 Location: http://169.254.169.254/…`` and the
fetch lands on the internal target anyway. ``safe_urlopen`` runs the same
validation on every hop.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.request
from urllib.parse import urlparse


ALLOWED_SCHEMES = {"http", "https"}


class UnsafeURLError(ValueError):
    """Raised when a URL targets a forbidden scheme or address range."""


def _is_blocked_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_outbound_url(url: str) -> str:
    """Return ``url`` unchanged if it's safe to fetch, else raise.

    Safe means: scheme ∈ {http, https}, hostname present, and every IP
    the hostname resolves to falls in a globally-routable range.
    """
    if not url:
        raise UnsafeURLError("empty URL")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme not allowed: {parsed.scheme or '<none>'}")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    # If the host is already a literal IP we don't need to resolve.
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise UnsafeURLError(f"DNS resolution failed for {host}: {exc}") from exc
        candidates = list({info[4][0] for info in infos})
        if not candidates:
            raise UnsafeURLError(f"no addresses resolved for {host}")
    for addr in candidates:
        if _is_blocked_ip(addr):
            raise UnsafeURLError(
                f"address {addr} for host {host} is in a non-routable range"
            )
    return url


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target before following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Raises UnsafeURLError (a ValueError) straight out of urlopen, which
        # is what the callers already catch alongside URLError.
        validate_outbound_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_ValidatingRedirectHandler)


def safe_urlopen(req, timeout: int = 15):
    """``urlopen`` for user-supplied URLs: validates the target, then every
    redirect hop it follows.

    ``req`` may be a URL string or a ``urllib.request.Request``. Use this
    anywhere the URL came from a user — a bare ``urlopen`` re-opens the SSRF
    hole through redirects even when the first URL was checked.
    """
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    validate_outbound_url(url)
    return _opener.open(req, timeout=timeout)
