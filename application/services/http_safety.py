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
"""
from __future__ import annotations

import ipaddress
import socket
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
