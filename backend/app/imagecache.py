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
import threading
from urllib.parse import urljoin, urlparse

import httpx
from . import telemetry

from .ingestion.netguard import BlockedAddress, _pin_to_ip, assert_public_url
from .media import media_dir

log = logging.getLogger("shelf.imagecache")

_SUBDIR = "imgcache"
_MAX_BYTES = 25 * 1024 * 1024
_EXT_BY_MIME = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/gif": "gif",
    "image/webp": "webp", "image/avif": "avif", "image/bmp": "bmp",
    # NOT svg — same-origin stored-XSS vector (SEC-M2); raster only.
}
_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', re.I)

# Tri-state sentinel: caching permanently failed (caller should stop pointing at the URL).
PERMANENT_FAIL = ""

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> httpx.Client:
    global _client
    # cache_image/cache_cover run from asyncio.to_thread (multiple worker threads), so guard the
    # lazy init: without the lock, two threads racing the first miss each build a client and the
    # loser's connection pool leaks.
    if _client is None or _client.is_closed:
        with _client_lock:
            if _client is None or _client.is_closed:
                # follow_redirects OFF so a redirect can't escape the SSRF check to an internal host.
                _client = telemetry.instrument_sync("image",
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


def sweep(max_bytes: int, pinned: set[str] | None = None) -> dict:
    """Bound the on-disk image cache: when it exceeds ``max_bytes``, delete least-recently-used
    images until back under the cap. Most cached images are content-addressed and RE-FETCHABLE on
    miss (cache_image re-downloads), so eviction is safe — it just trades disk for an occasional
    re-fetch. BUT a cover whose ``cover_url`` was rewritten to its local cache path is served as a
    STATIC file (not via cache_image), so evicting it would 404 permanently with no re-fetch — the
    caller passes those filenames in ``pinned`` so they are never evicted. Without the cap the cache
    grows forever (every cover + every remote chapter <img>, up to 25 MB each). ``.fail`` markers are
    tiny and left in place. Returns {removed, freed_mb, total_mb}."""
    pinned = pinned or set()
    d = _dir()
    files = []
    total = 0
    try:
        for p in d.iterdir():
            if p.suffix == ".fail" or not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            total += st.st_size
            if p.name in pinned:
                continue                   # referenced by a cover_url → not re-fetchable, never evict
            files.append((st.st_atime, st.st_size, p))
    except OSError:
        return {"removed": 0, "freed_mb": 0, "total_mb": 0}
    if total <= max_bytes:
        return {"removed": 0, "freed_mb": 0, "total_mb": round(total / 1048576)}
    files.sort()                       # oldest access first → evict LRU
    removed = freed = 0
    for _atime, size, p in files:
        if total - freed <= max_bytes:
            break
        try:
            p.unlink()
            freed += size
            removed += 1
        except OSError:
            pass
    log.info("imgcache sweep: removed %d file(s), freed %d MB (was %d MB, cap %d MB)",
             removed, freed // 1048576, total // 1048576, max_bytes // 1048576)
    return {"removed": removed, "freed_mb": round(freed / 1048576),
            "total_mb": round((total - freed) / 1048576)}


def _existing_local(name: str) -> str | None:
    d = _dir()
    for ext in ("jpg", "png", "webp", "gif", "avif", "bmp"):
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
    return (host == "books.google.com" or host.endswith(".books.google.com")) or (host == "googleusercontent.com" or host.endswith(".googleusercontent.com"))


def _is_ol_cover_host(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() == "covers.openlibrary.org"


def _is_blank_cover(data: bytes) -> bool:
    """True if ``data`` is a degenerate 'no cover' placeholder — a ~1-pixel image or a single flat
    colour. Open Library's keyless cover CDN serves such a blank at HTTP 200 for a missing cover
    (when ``?default=false`` isn't sent), which would otherwise be localized as a permanent blank.
    Conservative: a real cover is never this tiny or this uniform."""
    try:
        import io

        from PIL import Image
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        if w <= 2 or h <= 2:
            return True
        # getcolors returns None once there are more than `maxcolors` distinct colours (any real
        # cover), so a result of ≤1 colour means a flat fill.
        colors = im.convert("RGB").resize((32, 32)).getcolors(maxcolors=4)
        return colors is not None and len(colors) <= 1
    except Exception:  # noqa: BLE001 — never let detection break caching
        return False


def _fetch_image(url: str, referer: str | None, *, _depth: int = 0, host_ok=None) -> tuple[bytes, str, str] | str | None:
    """Fetch + validate a remote image. Returns ``(data, ext, ctype)`` on success, ``PERMANENT_FAIL``
    ("") when it will never be fetchable (blocked/non-image/too-big/4xx/placeholder), or None on a
    transient failure. No storage — caller decides where.

    Redirects ARE followed, but manually and SSRF-safely: each hop re-runs ``assert_public_url`` (so a
    redirect can't bounce us onto a private address) and the chain is depth-bounded. Legitimate cover
    CDNs redirect — e.g. Open Library's ``covers.openlibrary.org`` 302s to ``archive.org`` storage —
    and the old no-redirect policy rejected every such cover as a permanent failure."""
    try:
        ips = assert_public_url(url)
    except BlockedAddress:
        return PERMANENT_FAIL
    headers = {"Accept": "image/avif,image/webp,image/jpeg,image/png,*/*"}
    ref = referer or _referer_for(url)
    if ref:
        headers["Referer"] = ref
    # Pin the connection to the exact IP just validated so a DNS rebind at connect time can't swap
    # in an internal address (S6 — same discipline as netguard.safe_get / the crawler's fetcher).
    pinned_url, host_header, ext = _pin_to_ip(url, ips[0])
    try:
        r = _get_client().get(pinned_url, headers={**headers, **host_header}, extensions=ext)
    except httpx.HTTPError as exc:
        log.debug("image cache transient fail %s: %s", url, exc)
        return None  # transient → allow retry
    if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get("location")
        if not loc or _depth >= 3:
            return PERMANENT_FAIL  # missing/looping redirect → stop hammering it
        target = urljoin(url, loc)
        if host_ok is not None and not host_ok(target):  # SEC-M1: don't follow a redirect off the allowlist
            return PERMANENT_FAIL
        return _fetch_image(target, referer, _depth=_depth + 1, host_ok=host_ok)  # next hop re-validated
    if r.status_code != 200:
        return PERMANENT_FAIL if 400 <= r.status_code < 500 else None  # 4xx permanent, 5xx transient
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    data = r.content
    # Reject SVG explicitly (not just drop it from the ext map) — an SVG can carry inline <script> and
    # is served from our own origin, so storing it (even mislabeled) is a needless same-origin XSS
    # vector behind only the CSP (SEC-M2). Covers/comic images are raster.
    if not ctype.startswith("image/") or ctype == "image/svg+xml" or not data or len(data) > _MAX_BYTES:
        return PERMANENT_FAIL
    # Google Books serves a grey "image not available" placeholder (HTTP 200) for covers it lacks.
    if _is_gbooks_host(url) and _is_gbooks_no_cover(data):
        return PERMANENT_FAIL
    # Open Library's keyless cover CDN serves a blank placeholder (HTTP 200) for a missing cover.
    if _is_ol_cover_host(url) and _is_blank_cover(data):
        return PERMANENT_FAIL
    return data, _EXT_BY_MIME.get(ctype, "jpg"), ctype


# Cover originals can be huge (e.g. 2164×3264, ~3.6 MB) yet never display larger than a few hundred
# px (grid thumbnails ~166 px, the hero a few hundred). Downscale to a sane long-edge so we don't
# ship full-res art into thumbnail/hero slots — ~5–8× smaller with no visible quality loss.
COVER_MAX_EDGE = 1200


def downscale_image(data: bytes, *, max_edge: int = COVER_MAX_EDGE, quality: int = 85) -> tuple[bytes, bool]:
    """Return (bytes, recompressed). If ``data`` decodes to an image whose long edge exceeds
    ``max_edge``, return a downscaled JPEG; otherwise — a small image, an animated GIF, a non-image,
    a decode error, or a re-encode that isn't actually smaller — return the ORIGINAL bytes unchanged.
    Best-effort: a cover stays the same picture, just lighter."""
    import io as _io
    try:
        from PIL import Image
    except Exception:  # pragma: no cover — Pillow always present, but never let this break a save
        return data, False
    try:
        im = Image.open(_io.BytesIO(data))
        if getattr(im, "is_animated", False):
            return data, False  # don't flatten an animated GIF to one frame
        im.load()
    except Exception:
        return data, False
    w, h = im.size
    if max(w, h) <= max_edge:
        return data, False
    scale = max_edge / max(w, h)
    try:
        small = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        if small.mode not in ("RGB", "L"):
            small = small.convert("RGB")
        out = _io.BytesIO()
        small.save(out, format="JPEG", quality=quality, optimize=True)
    except Exception:
        return data, False
    b = out.getvalue()
    return (b, True) if len(b) < len(data) else (data, False)


def cache_image(url: str, *, referer: str | None = None, host_ok=None) -> str | None:
    """Download ``url`` once and return its permanent local ``/media/imgcache/..`` URL.

    Returns the local URL on success, ``PERMANENT_FAIL`` ("") when the image will never be
    fetchable (blocked/non-image/too-big/4xx — caller should drop the remote URL), or None
    on a transient failure (caller may keep the remote URL and retry later).

    ``host_ok`` (optional): a predicate re-checked on every redirect hop, so a caller (e.g. /api/cover)
    can stop an allowlisted host from redirecting the fetch off-allowlist (SEC-M1)."""
    if not is_remote(url):
        return url  # already local / nothing to do
    name = _name(url)
    existing = _existing_local(name)
    if existing:
        return existing
    fail_marker = _dir() / f"{name}.fail"
    if fail_marker.exists():
        return PERMANENT_FAIL
    res = _fetch_image(url, referer, host_ok=host_ok)
    if res is None:
        return None
    if res == PERMANENT_FAIL:
        fail_marker.write_bytes(b"")
        return PERMANENT_FAIL
    data, ext, _ctype = res
    # NB: do NOT downscale here — cache_image serves CHAPTER/COMIC PAGE images (via
    # localize_html_images), not covers. Downscaling would wreck manga/webtoon reading pages.
    # Cover downscaling lives only on the cover path (cache_cover → covers.save_cover).
    (_dir() / f"{name}.{ext}").write_bytes(data)
    return f"/media/{_SUBDIR}/{name}.{ext}"


def cache_cover(url: str, *, referer: str | None = None) -> str | None:
    """Like :func:`cache_image`, but stores into the DURABLE ``/covers/`` directory rather than the
    LRU-swept ``imgcache``. Covers are bounded (one per work) and must PERSIST — under the imgcache
    cap they were evicted by chapter-image churn and (since localization overwrites the remote URL)
    could never be re-fetched, leaving permanent blank covers. /covers/ is never swept. Same return
    contract as ``cache_image`` (local URL / PERMANENT_FAIL / None). Deduped by the url hash."""
    if not is_remote(url):
        return url
    from . import covers
    name = _name(url)
    existing = covers.existing_cover(name)
    if existing:
        return existing
    fail_marker = _dir() / f"{name}.coverfail"   # separate marker so a cover retry isn't blocked by
    if fail_marker.exists():                      # an imgcache .fail for the same URL, and vice-versa
        return PERMANENT_FAIL
    res = _fetch_image(url, referer)
    if res is None:
        return None
    if res == PERMANENT_FAIL:
        fail_marker.write_bytes(b"")
        return PERMANENT_FAIL
    data, _ext, ctype = res
    # Covers are portrait (~2:3). A clearly LANDSCAPE image here is a banner / logo / wrong asset that
    # letterboxes badly in a cover slot — drop it (work falls back to a placeholder / re-source).
    if _too_wide_for_cover(data):
        fail_marker.write_bytes(b"")
        return PERMANENT_FAIL
    return covers.save_cover(name, data, ctype)


def _too_wide_for_cover(data: bytes) -> bool:
    """True only for a clearly landscape image (aspect > 1.3) — a banner/logo, never a real cover.
    Header-only read; any decode error → False (keep it, don't over-reject)."""
    import io as _io
    try:
        from PIL import Image
        w, h = Image.open(_io.BytesIO(data)).size
    except Exception:  # noqa: BLE001
        return False
    return h > 0 and (w / h) > 1.3


_CTYPE_BY_EXT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                 "webp": "image/webp", "gif": "image/gif"}  # no svg (SEC-M2: served same-origin)


def migrate_imgcache_cover(local_url: str) -> str | None:
    """Salvage a cover that was localized into the LRU-swept ``/media/imgcache/`` store back into the
    DURABLE ``/covers/`` store — purely by moving the on-disk file, no network. Returns the new
    ``/covers/`` URL, or None if the imgcache file is already EVICTED (the caller must re-source the
    cover, since the original remote URL was overwritten when it was first localized). Used to heal
    legacy rows whose covers would otherwise vanish on the next sweep."""
    if not local_url or "/imgcache/" not in local_url:
        return None
    from . import covers
    fname = local_url.rsplit("/", 1)[-1]               # <name>.<ext>
    src = _dir() / fname
    if not src.is_file():
        return None                                    # evicted — nothing on disk to salvage
    stem, _, ext = fname.rpartition(".")
    try:
        data = src.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    return covers.save_cover(stem or fname, data, _CTYPE_BY_EXT.get(ext.lower(), "image/jpeg"))


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
