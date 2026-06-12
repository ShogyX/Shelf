"""Local cover-image storage, served at /covers/<file>.

Used for sources where the cover only exists inside an uploaded file (EPUB import)
rather than at a stable remote URL.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .config import get_settings

_settings = get_settings()


def _safe_key(key: str, limit: int = 120) -> str:
    """A filesystem-safe filename stem for a storage key. Keys within the limit keep their readable,
    backward-compatible form; a longer key would COLLIDE on its shared prefix once truncated, so its
    tail is replaced with a hash of the FULL key to keep it unique (F4.4)."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in key)
    if len(safe) <= limit:
        return safe
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return safe[: limit - len(digest) - 1] + "-" + digest


def covers_dir() -> Path:
    d = Path(_settings.covers_dir) if _settings.covers_dir else (
        Path(__file__).resolve().parent.parent / "covers"
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}


def save_cover(key: str, data: bytes, mime: str | None = None) -> str:
    """Persist cover bytes under a stable key, return its /covers/<file> URL."""
    ext = _EXT_BY_MIME.get((mime or "").lower(), "jpg")
    path = covers_dir() / f"{_safe_key(key)}.{ext}"
    path.write_bytes(data)
    return f"/covers/{path.name}"
