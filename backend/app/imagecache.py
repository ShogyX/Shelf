"""Permanent local image cache.

Covers and comic/illustration images are downloaded ONCE and stored permanently under
``media/imgcache/`` (served at ``/media/imgcache/<hash>.<ext>``), so the app never depends
on remote requests to display them — important because remote covers/CDN images are slow,
sometimes hotlink-protected, and sometimes served from short-lived token URLs that would
otherwise expire. Cataloged + hooked images are localized via this module.

Idempotent and self-bounding: a URL maps to a deterministic filename (fetched once, reused
forever); a definitive failure writes a ``.fail`` marker so it's never re-fetched.
"""
from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx

from .ingestion.netguard import BlockedAddress, assert_public_url
from .media import media_dir

log = logging.getLogger("shelf.imagecache")

_SUBDIR = "imgcache"
_MAX_BYTES = 25 * 1024 * 1024
_EXT_BY_MIME = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/gif": "gif",
    "image/webp": "webp", "image/avif": "avif", "image/svg+xml": "svg", "image/bmp": "bmp",
}
_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', re.I)

# Tri-state sentinel: caching permanently failed (caller should stop pointing at the URL).
PERMANENT_FAIL = ""

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None or _client.is_closed:
        # follow_redirects OFF so a redirect can't escape the SSRF check to an internal host.
        _client = httpx.Client(
            follow_redirects=False, timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ShelfReader/0.1)"},
        )
    return _client


def is_remote(url: str | None) -> bool:
    return bool(url) and url.startswith(("http://", "https://"))


def _dir():
    d = media_dir() / _SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _name(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def _existing_local(name: str) -> str | None:
    d = _dir()
    for ext in ("jpg", "png", "webp", "gif", "avif", "svg", "bmp"):
        if (d / f"{name}.{ext}").exists():
            return f"/media/{_SUBDIR}/{name}.{ext}"
    return None


def _referer_for(url: str) -> str | None:
    # Lazy import to avoid a router import cycle; reuses the hotlink allowlist.
    try:
        from .routers.imgproxy import referer_for
        return referer_for(url)
    except Exception:
        return None


def _is_gbooks_no_cover(data: bytes) -> bool:
    """True if ``data`` is Google Books' grey 'image not available' placeholder. Google serves it
    (HTTP 200) when a cover doesn't exist at the requested size — so a high-res request can return
    it even when a real low-res cover exists. Detected by content: it's fully grayscale, mostly
    white, with almost no distinct colors (real covers have thousands). Strict thresholds so a
    legitimately pale/B&W cover is never mistaken for it."""
    try:
        import io

        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB").resize((64, 64))
        px = list(im.getdata())
        n = len(px)
        gray = sum(1 for r, g, b in px if abs(r - g) < 8 and abs(g - b) < 8 and abs(r - b) < 8) / n
        white = sum(1 for r, g, b in px if r > 235 and g > 235 and b > 235) / n
        return gray >= 0.98 and white >= 0.6 and len(set(px)) < 200
    except Exception:  # noqa: BLE001 — never let detection break caching
        return False


def _is_gbooks_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith("books.google.com") or host.endswith("googleusercontent.com")


def cache_image(url: str, *, referer: str | None = None) -> str | None:
    """Download ``url`` once and return its permanent local ``/media/imgcache/..`` URL.

    Returns the local URL on success, ``PERMANENT_FAIL`` ("") when the image will never be
    fetchable (blocked/non-image/too-big/4xx — caller should drop the remote URL), or None
    on a transient failure (caller may keep the remote URL and retry later)."""
    if not is_remote(url):
        return url  # already local / nothing to do
    name = _name(url)
    existing = _existing_local(name)
    if existing:
        return existing
    fail_marker = _dir() / f"{name}.fail"
    if fail_marker.exists():
        return PERMANENT_FAIL
    try:
        assert_public_url(url)
    except BlockedAddress:
        fail_marker.write_bytes(b"")
        return PERMANENT_FAIL

    headers = {"Accept": "image/avif,image/webp,image/jpeg,image/png,*/*"}
    ref = referer or _referer_for(url)
    if ref:
        headers["Referer"] = ref
    try:
        r = _get_client().get(url, headers=headers)
    except httpx.HTTPError as exc:
        log.debug("image cache transient fail %s: %s", url, exc)
        return None  # transient → allow retry
    if r.status_code in (301, 302, 303, 307, 308):
        # We don't follow redirects (SSRF). Treat as permanent so we stop hammering it.
        fail_marker.write_bytes(b"")
        return PERMANENT_FAIL
    if r.status_code != 200:
        if 400 <= r.status_code < 500:
            fail_marker.write_bytes(b"")
            return PERMANENT_FAIL
        return None  # 5xx → transient
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    data = r.content
    if not ctype.startswith("image/") or not data or len(data) > _MAX_BYTES:
        fail_marker.write_bytes(b"")
        return PERMANENT_FAIL
    # Google Books serves a grey "image not available" placeholder (HTTP 200) for covers it lacks
    # at the requested size — reject it so callers fall back to a real (lower-res) cover.
    if _is_gbooks_host(url) and _is_gbooks_no_cover(data):
        fail_marker.write_bytes(b"")
        return PERMANENT_FAIL
    ext = _EXT_BY_MIME.get(ctype, "jpg")
    (_dir() / f"{name}.{ext}").write_bytes(data)
    return f"/media/{_SUBDIR}/{name}.{ext}"


def localize_html_images(html: str, base_url: str = "") -> str:
    """Rewrite every remote <img src> in chapter HTML to a permanently-cached local copy.
    Already-local srcs (/media, /covers, /api/img) and uncacheable ones are left as-is."""
    if not html or "<img" not in html:
        return html or ""

    def repl(m: re.Match) -> str:
        src = m.group(2)
        if base_url and not src.startswith(("http://", "https://", "/", "data:")):
            src = urljoin(base_url, src)
        if not is_remote(src):
            return m.group(0)
        local = cache_image(src)
        if local and local != PERMANENT_FAIL:
            return f"{m.group(1)}{local}{m.group(3)}"
        return m.group(0)  # keep original on failure (serve-time proxy may still handle it)

    return _IMG_SRC_RE.sub(repl, html)
