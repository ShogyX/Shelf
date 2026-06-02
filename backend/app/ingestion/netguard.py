"""SSRF egress guard.

The crawler, the headless browser, and the integration clients all fetch URLs the *user*
supplies (index a site, hook a feed, point at a Readarr). Without a guard a user could aim
them at internal addresses — cloud metadata (169.254.169.254), localhost admin panels,
RFC-1918 ranges — and read the responses back through the app (a classic SSRF leading to
credential theft). This module rejects any URL whose host resolves to a non-public address,
and is applied before every outbound fetch (and re-checked on each redirect hop).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class BlockedAddress(Exception):
    """Raised when a URL targets a non-public (internal/loopback/metadata) address."""


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # Block loopback, private (RFC1918 / fc00::/7), link-local (incl. 169.254 metadata),
    # reserved, multicast, unspecified. Only globally-routable addresses pass.
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        return False
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) — unwrap and re-check.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return _ip_is_public(str(mapped))
    return True


def assert_public_url(url: str) -> None:
    """Raise BlockedAddress unless ``url`` is http(s) to a host that resolves to only
    public IPs. Resolving here (not trusting the literal) defeats DNS that points a name
    at an internal IP."""
    pr = urlparse(url)
    scheme = (pr.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise BlockedAddress(f"blocked non-http(s) URL scheme: {scheme!r}")
    host = pr.hostname
    if not host:
        raise BlockedAddress("blocked URL with no host")
    try:
        infos = socket.getaddrinfo(host, pr.port or (443 if scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise BlockedAddress(f"could not resolve host {host!r}") from exc
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise BlockedAddress(f"host {host!r} resolved to no addresses")
    bad = [ip for ip in addrs if not _ip_is_public(ip)]
    if bad:
        raise BlockedAddress(f"blocked internal address for {host!r}: {', '.join(bad)}")


def is_public_url(url: str) -> bool:
    try:
        assert_public_url(url)
        return True
    except BlockedAddress:
        return False
