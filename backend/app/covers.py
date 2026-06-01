"""Local cover-image storage, served at /covers/<file>.

Used for sources where the cover only exists inside an uploaded file (EPUB import)
rather than at a stable remote URL.
"""
from __future__ import annotations

from pathlib import Path

from .config import get_settings

_settings = get_settings()


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
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in key)[:120]
    path = covers_dir() / f"{safe}.{ext}"
    path.write_bytes(data)
    return f"/covers/{path.name}"
