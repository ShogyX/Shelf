"""Local media storage for non-text reading media (comic page images).

Images extracted from CBZ/CBR archives are written under media_dir()/comics/<key>/
and served at /media/comics/<key>/<file>, mirroring how covers.py serves cover art.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .config import get_settings

_settings = get_settings()


def _safe(key: str, limit: int = 120) -> str:
    """A filesystem-safe path segment for a storage key. Keys within the limit keep their readable,
    backward-compatible form; a longer key would COLLIDE on its shared prefix once truncated, so its
    tail is replaced with a hash of the FULL key to keep it unique (F4.4). All comic/book dir+url
    helpers route through this so a key's directory and its URLs always agree."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in key)
    if len(safe) <= limit:
        return safe
    digest = hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return safe[: limit - len(digest) - 1] + "-" + digest


def media_dir() -> Path:
    # Admin override (Settings → Storage) wins; else the env/config value; else the built-in default.
    from . import storage
    override = storage.get("media_dir")
    d = (
        Path(override) if override
        else Path(_settings.media_dir) if getattr(_settings, "media_dir", "")
        else (Path(__file__).resolve().parent.parent / "media")
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def comic_dir(key: str) -> Path:
    d = media_dir() / "comics" / _safe(key)
    d.mkdir(parents=True, exist_ok=True)
    return d


def comic_url(key: str, filename: str) -> str:
    return f"/media/comics/{_safe(key)}/{filename}"


def book_dir(key: str) -> Path:
    """Storage for a text book's inline images (illustrated EPUBs), so the reader can load
    them from /media instead of unresolvable EPUB-internal paths."""
    d = media_dir() / "books" / _safe(key)
    d.mkdir(parents=True, exist_ok=True)
    return d


def book_url(key: str, filename: str) -> str:
    return f"/media/books/{_safe(key)}/{filename}"


def descramble_dir(work_id: int, chapter_id: int) -> Path:
    """Storage for browser-captured descrambled comic pages (one dir per chapter)."""
    d = media_dir() / "descrambled" / str(int(work_id)) / str(int(chapter_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def descramble_url(work_id: int, chapter_id: int, filename: str) -> str:
    return f"/media/descrambled/{int(work_id)}/{int(chapter_id)}/{filename}"
