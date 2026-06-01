"""Local media storage for non-text reading media (comic page images).

Images extracted from CBZ/CBR archives are written under media_dir()/comics/<key>/
and served at /media/comics/<key>/<file>, mirroring how covers.py serves cover art.
"""
from __future__ import annotations

from pathlib import Path

from .config import get_settings

_settings = get_settings()


def media_dir() -> Path:
    d = (
        Path(_settings.media_dir)
        if getattr(_settings, "media_dir", "")
        else (Path(__file__).resolve().parent.parent / "media")
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def comic_dir(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in key)[:120]
    d = media_dir() / "comics" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def comic_url(key: str, filename: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in key)[:120]
    return f"/media/comics/{safe}/{filename}"
