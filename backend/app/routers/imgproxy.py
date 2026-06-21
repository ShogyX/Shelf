"""Referer-supplying image proxy for hotlink-protected comic CDNs.

Some comic hosts (notably LINE Webtoon's ``*.pstatic.net``) reject image requests
whose ``Referer`` isn't the origin site, so a chapter's <img> can't load them directly
in the reader. This endpoint re-fetches such an image server-side with the correct
Referer and streams it back. It is deliberately limited to a small allowlist of known
comic-image hosts (no arbitrary URLs → no open proxy / SSRF surface).
"""
from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db

router = APIRouter()

_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', re.I)


def rewrite_hotlinked(html: str) -> str:
    """Route any <img> whose host needs a Referer through this proxy. Idempotent
    (already-proxied/other images are left untouched). Applied at serve time so both
    existing and newly-ingested comic chapters render."""
    if not html or "<img" not in html:
        return html or ""
    # Fast path: skip the regex entirely unless an allowlisted host is even present.
    if not any(suffix in html for suffix in HOTLINK_REFERERS):
        return html

    def repl(m: re.Match) -> str:
        url = m.group(2)
        if url.startswith("//"):  # protocol-relative //host/x.jpg — normalize so the proxy check fires
            url = "https:" + url
        if url.startswith(("http://", "https://")) and referer_for(url):
            return f'{m.group(1)}/api/img?u={quote(url, safe="")}{m.group(3)}'
        return m.group(0)

    return _IMG_SRC_RE.sub(repl, html)

# host-suffix -> Referer to send when fetching from it.
HOTLINK_REFERERS: dict[str, str] = {
    "pstatic.net": "https://www.webtoons.com/",
    "webtoons.com": "https://www.webtoons.com/",
}


def referer_for(url: str) -> str | None:
    """The Referer a host needs (if it's an allowlisted hotlink-protected CDN), else None."""
    host = (urlparse(url).hostname or "").lower()
    for suffix, ref in HOTLINK_REFERERS.items():
        if host == suffix or host.endswith("." + suffix):
            return ref
    return None


# SEC-M1: cover art originates only from the metadata providers' CDNs + the operator's crawled source
# domains. Restrict /cover's REMOTE fetch to those hosts so an authenticated user can't use it as an
# arbitrary-URL egress/image proxy. (Internal SSRF is already blocked by assert_public_url; this closes
# the public open-proxy.) Almost every cover is already localized to /covers/, so this rarely fires.
_COVER_CDN_SUFFIXES = frozenset({
    "googleusercontent.com", "books.google.com", "google.com",   # Google Books
    "openlibrary.org",                                           # Open Library
    "anilist.co", "anili.st",                                    # AniList
    "hardcover.app",                                             # Hardcover
    "media-amazon.com", "ssl-images-amazon.com", "gr-assets.com",  # Amazon / Goodreads-hosted art
})
_cover_hosts_cache: tuple[float, frozenset[str]] = (-1e9, frozenset())


def _allowed_cover_hosts(db: Session) -> frozenset[str]:
    """Fixed cover CDNs + hotlink CDNs + the operator's crawled source domains (cached ~5 min)."""
    global _cover_hosts_cache
    now = time.monotonic()
    if now - _cover_hosts_cache[0] < 300:
        return _cover_hosts_cache[1]
    hosts = set(_COVER_CDN_SUFFIXES) | set(HOTLINK_REFERERS)
    from ..models import IndexSite, Source
    for stmt in (select(Source.base_url), select(IndexSite.root_url)):
        for (u,) in db.execute(stmt).all():
            h = (urlparse(u or "").hostname or "").lower()
            if not h:
                continue
            hosts.add(h)
            # Allow sibling cover-CDN subdomains (base www.foo.com, covers on cdn.foo.com) by also
            # adding the www-stripped host. Deliberately NOT a registrable-domain/PSL reduction —
            # `parts[-2:]` would yield a public suffix for a multi-label TLD (foo.co.uk → co.uk) and
            # open *.co.uk. Stripping only "www." stays safe (www.foo.co.uk → foo.co.uk).
            if h.startswith("www."):
                hosts.add(h[4:])
    _cover_hosts_cache = (now, frozenset(hosts))
    return _cover_hosts_cache[1]


def _host_in(url: str, allowed: frozenset[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(host) and any(host == s or host.endswith("." + s) for s in allowed)


def _cover_host_allowed(url: str, db: Session) -> bool:
    return _host_in(url, _allowed_cover_hosts(db))


def cover_host_allowed_cached(url: str) -> bool:
    """Allowlist check against ONLY the already-built cache (no DB) — safe to call from the download
    worker thread to re-validate each REDIRECT hop, so an allowed CDN can't 30x-redirect the fetch to
    an arbitrary public host (SEC-M1). The request handler primes the cache before the fetch."""
    return _host_in(url, _cover_hosts_cache[1])


@router.get("/cover")
async def cover_image(u: str = Query(..., description="A cover URL — local served from disk, remote "
                                                    "fetched ONCE then cached on disk"),
                      db: Session = Depends(get_db)):
    """The single path the UI uses for cover art: ALWAYS checks the on-disk cache first and only
    fetches from the web on a true cache miss, then stores the result so no further web request is
    ever made for that cover. A local path is served straight from disk; an unfetchable/blocked cover
    returns 404 so the client renders a stable generative placeholder (never an erratic broken image).
    Supersedes the browser fetching remote cover URLs directly (hotlink / Cloudflare / rate-limit
    failures were the source of covers flickering in and out)."""
    from .. import imagecache
    from ..media import media_dir
    # A local path is served straight through — but ONLY our own static mounts, and never a
    # protocol-relative "//host" or "/\\host" (which a browser resolves as an absolute cross-origin
    # URL): redirecting to that would be an open redirect. Restrict to the exact prefixes we serve.
    if u.startswith("/"):
        if u.startswith(("//", "/\\")) or not u.startswith(("/media/", "/covers/", "/api/")):
            raise HTTPException(400, "Only /media, /covers or absolute http(s) URLs may be requested.")
        return RedirectResponse(u, status_code=307)
    if not u.startswith(("http://", "https://")):
        raise HTTPException(400, "Only absolute http(s) or local URLs may be requested.")
    if not _cover_host_allowed(u, db):  # SEC-M1: only known cover sources, not an arbitrary egress proxy
        raise HTTPException(403, "Cover host not allowed.")
    # cache_image: returns the cached local path with NO fetch if already on disk; otherwise fetches
    # once (with the right Referer for hotlink CDNs), stores it, and marks permanent failures so they
    # are never retried. "" (permanent) / None (transient) → no image this time. host_ok re-checks every
    # redirect hop against the (now-primed) allowlist cache so an allowed CDN can't 30x off it (SEC-M1).
    local = await asyncio.to_thread(imagecache.cache_image, u, host_ok=cover_host_allowed_cached)
    if not local:
        raise HTTPException(404, "cover not available")
    path = media_dir() / local[len("/media/"):]
    if not path.is_file():                      # cache row exists but file vanished → treat as miss
        raise HTTPException(404, "cover not available")
    # Content is addressed by a hash of the source URL → effectively immutable, so the browser can
    # cache it forever and never re-request it.
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/img")
async def proxy_image(u: str = Query(..., description="Absolute image URL on an allowlisted CDN")):
    """Hotlinked comic image: fetched ONCE through the disk imagecache (with the host's required
    Referer) and served from disk thereafter — so a webtoon's dozens of pages don't re-proxy through
    the server on every load. Cache-Control is ``private`` (not ``public``): this is an auth-gated
    route, so a shared/intermediary cache must not store the response."""
    from .. import imagecache
    from ..media import media_dir
    if not u.startswith(("http://", "https://")):
        raise HTTPException(400, "Only absolute http(s) URLs may be proxied.")
    if referer_for(u) is None:                  # allowlist gate (same SSRF/hotlink scope as before)
        raise HTTPException(403, "Host not in the image-proxy allowlist.")
    # cache_image applies the correct Referer for the allowlisted host, enforces the SSRF guard +
    # size cap, stores to disk, and marks permanent failures so they're never re-fetched.
    local = await asyncio.to_thread(imagecache.cache_image, u)
    if not local:
        raise HTTPException(502, "Upstream image not available")
    path = media_dir() / local[len("/media/"):]
    if not path.is_file():
        raise HTTPException(502, "Upstream image not available")
    # Content-addressed (hash of the source URL) → immutable; private so only the requesting
    # browser caches it, never a shared proxy on this gated route.
    return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000, immutable"})
