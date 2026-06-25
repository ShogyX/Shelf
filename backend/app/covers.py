"""Local cover-image storage, served at /covers/<file>.

Used for sources where the cover only exists inside an uploaded file (EPUB import)
rather than at a stable remote URL.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from .config import get_settings

_settings = get_settings()
log = logging.getLogger("shelf.covers")


def _safe_key(key: str, limit: int = 120) -> str:
    """A filesystem-safe filename stem for a storage key. Keys within the limit keep their readable,
    backward-compatible form; a longer key would COLLIDE on its shared prefix once truncated, so its
    tail is replaced with a hash of the FULL key to keep it unique (F4.4)."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in key)
    if len(safe) <= limit:
        return safe
    digest = hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return safe[: limit - len(digest) - 1] + "-" + digest


def covers_dir() -> Path:
    from . import storage
    override = storage.get("covers_dir")
    d = (Path(override) if override
         else Path(_settings.covers_dir) if _settings.covers_dir
         else (Path(__file__).resolve().parent.parent / "covers"))
    d.mkdir(parents=True, exist_ok=True)
    return d


_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    # NOT image/svg+xml — an SVG can carry inline <script> and is served from our own origin, so it's
    # a stored-XSS vector behind only the CSP (SEC-M2). Covers are raster; refuse SVG outright.
}


def save_cover(key: str, data: bytes, mime: str | None = None) -> str:
    """Persist cover bytes under a stable key, return its /covers/<file> URL."""
    from .imagecache import downscale_image
    data, shrunk = downscale_image(data)   # cap oversized originals (re-encodes to JPEG)
    ext = "jpg" if shrunk else _EXT_BY_MIME.get((mime or "").lower(), "jpg")
    path = covers_dir() / f"{_safe_key(key)}.{ext}"
    path.write_bytes(data)
    return f"/covers/{path.name}"


def shrink_oversized_covers(limit: int = 200) -> dict:
    """Backfill for P2-9: downscale already-stored oversized covers IN PLACE so the historical
    full-res files (a 2164×3264 ~3.6 MB original shipped into a ~250 px slot) stop bloating page
    loads. Bounded (``limit`` per call) + idempotent: once a file is ≤ COVER_MAX_EDGE it's skipped,
    so repeated runs converge then no-op. JPEG-only — re-encoding keeps the SAME filename + content
    type, so DB cover_url and the immutable cache stay valid (a smaller same-name file just means
    already-cached clients keep theirs; new requests get the lighter one). Atomic temp+replace so a
    crash mid-write can't truncate a live cover. Returns counts for the tick log."""
    from PIL import Image  # noqa: PLC0415 — kept local; only the (rare) backfill needs it

    from .imagecache import COVER_MAX_EDGE, downscale_image
    # ONLY the /covers dir — NOT the imgcache dir, which holds CHAPTER/COMIC PAGE images that must
    # keep their full resolution (downscaling them wrecks manga/webtoon reading pages).
    dirs = [covers_dir()]
    scanned = shrunk = saved = 0
    for d in dirs:
        if shrunk >= limit:
            break
        try:
            entries = sorted(d.iterdir())
        except Exception:  # noqa: BLE001
            continue
        for p in entries:
            if shrunk >= limit:
                break
            if not p.is_file() or p.suffix.lower() not in (".jpg", ".jpeg"):
                continue
            scanned += 1
            try:
                with Image.open(p) as im:          # header read only — cheap when nothing to do
                    if max(im.size) <= COVER_MAX_EDGE:
                        continue
            except Exception:  # noqa: BLE001 — unreadable/non-image → leave it
                continue
            try:
                orig = p.read_bytes()
                new, changed = downscale_image(orig)
                if not changed:
                    continue
                tmp = p.with_name(p.name + ".shrink.tmp")
                tmp.write_bytes(new)
                os.replace(tmp, p)                 # atomic; same filename → every URL still resolves
            except Exception:  # noqa: BLE001
                continue
            shrunk += 1
            saved += len(orig) - len(new)
    if shrunk:
        log.info("cover shrink: %d downscaled, %d scanned, %.1f MB saved", shrunk, scanned, saved / 1e6)
    return {"scanned": scanned, "shrunk": shrunk, "bytes_saved": saved}


def existing_cover(key: str) -> str | None:
    """The /covers/<file> URL for an already-stored cover under ``key``, or None. Lets a durable-cover
    localizer skip a re-download when the file is already present (dedup by stable key)."""
    safe = _safe_key(key)
    d = covers_dir()
    for ext in ("jpg", "png", "webp", "gif"):
        p = d / f"{safe}.{ext}"
        if p.is_file():
            return f"/covers/{p.name}"
    return None
