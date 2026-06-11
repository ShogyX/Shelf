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
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse, Response

router = APIRouter()

_MAX_IMAGE_BYTES = 25 * 1024 * 1024  # cap a single proxied image (DoS guard)
_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', re.I)

# Shared, connection-pooled client (a comic page proxies dozens of images). follow_redirects
# is OFF so a redirect can't escape the host allowlist (SSRF guard).
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=20.0, follow_redirects=False)
    return _client


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


@router.get("/cover")
async def cover_image(u: str = Query(..., description="A cover URL — local served from disk, remote "
                                                    "fetched ONCE then cached on disk")):
    """The single path the UI uses for cover art: ALWAYS checks the on-disk cache first and only
    fetches from the web on a true cache miss, then stores the result so no further web request is
    ever made for that cover. A local path is served straight from disk; an unfetchable/blocked cover
    returns 404 so the client renders a stable generative placeholder (never an erratic broken image).
    Supersedes the browser fetching remote cover URLs directly (hotlink / Cloudflare / rate-limit
    failures were the source of covers flickering in and out)."""
    from .. import imagecache
    from ..media import media_dir
    if u.startswith("/"):                       # already a local /media or /covers path
        return RedirectResponse(u, status_code=307)
    if not u.startswith(("http://", "https://")):
        raise HTTPException(400, "Only absolute http(s) or local URLs may be requested.")
    # cache_image: returns the cached local path with NO fetch if already on disk; otherwise fetches
    # once (with the right Referer for hotlink CDNs), stores it, and marks permanent failures so they
    # are never retried. "" (permanent) / None (transient) → no image this time.
    local = await asyncio.to_thread(imagecache.cache_image, u)
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
    if not u.startswith(("http://", "https://")):
        raise HTTPException(400, "Only absolute http(s) URLs may be proxied.")
    ref = referer_for(u)
    if ref is None:
        raise HTTPException(403, "Host not in the image-proxy allowlist.")
    headers = {
        "Referer": ref,
        "User-Agent": "Mozilla/5.0 (compatible; ShelfReader/0.1)",
        "Accept": "image/avif,image/webp,image/jpeg,image/png,*/*",
    }
    try:
        # follow_redirects is OFF (see _get_client): a redirect would escape the host
        # allowlist (SSRF to an internal host / cloud metadata). Stream + cap the body so a
        # hostile/compromised CDN can't exhaust memory with a giant response.
        async with _get_client().stream("GET", u, headers=headers) as resp:
            if resp.status_code != 200:
                raise HTTPException(502, f"Upstream returned HTTP {resp.status_code}")
            media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
            if not media_type.startswith("image/"):
                raise HTTPException(502, "Upstream did not return an image.")
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > _MAX_IMAGE_BYTES:
                raise HTTPException(502, "Upstream image too large.")
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > _MAX_IMAGE_BYTES:
                    raise HTTPException(502, "Upstream image too large.")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Upstream image fetch failed: {exc}") from exc
    return Response(
        content=bytes(buf),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
