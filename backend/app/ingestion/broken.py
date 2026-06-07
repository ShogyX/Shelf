"""Broken-release registry.

A release (NZB) can fail two ways: SABnzbd can't assemble it (corrupt / missing par2 blocks), or
it downloads fine but post-download verification finds it's the wrong book. Either way we record a
stable identity for it so the matcher never offers — and the orchestrator never re-grabs — that same
dead link again. This is what lets the fetcher try candidate after candidate without looping.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import BrokenRelease


def release_key(release) -> str | None:
    """A stable identity for a release: the indexer GUID when present, else a hash of its download
    URL. Accepts a prowlarr.Release, a candidate dict, or any object exposing .guid/.download_url."""
    def _get(name: str):
        if isinstance(release, dict):
            return release.get(name)
        return getattr(release, name, None)

    # A candidate dict carries its precomputed identity (computed once at search time) — use it so
    # the same release marks/filters under one key everywhere.
    if isinstance(release, dict) and release.get("key"):
        return str(release["key"])
    guid = _get("guid")
    if guid:
        return f"guid:{str(guid)[:240]}"
    url = _get("download_url") or _get("url")
    if url:
        return "url:" + hashlib.sha1(str(url).encode("utf-8")).hexdigest()
    return None


def is_broken(db: Session, release) -> bool:
    key = release_key(release)
    if not key:
        return False
    return db.scalar(select(BrokenRelease.id).where(BrokenRelease.release_key == key)) is not None


def broken_keys(db: Session) -> set[str]:
    """All recorded broken keys (one query, for filtering a search batch)."""
    return set(db.scalars(select(BrokenRelease.release_key)).all())


def mark_broken(db: Session, release, *, reason: str | None = None) -> None:
    """Record `release` as broken (idempotent). Does not raise on a race or a keyless release."""
    key = release_key(release)
    if not key:
        return
    if db.scalar(select(BrokenRelease.id).where(BrokenRelease.release_key == key)) is not None:
        return
    title = release.get("title") if isinstance(release, dict) else getattr(release, "title", None)
    db.add(BrokenRelease(
        release_key=key, release_title=(str(title)[:1024] if title else None),
        reason=(str(reason)[:255] if reason else None),
    ))
    try:
        db.commit()
    except IntegrityError:           # raced with another tick recording the same key
        db.rollback()
