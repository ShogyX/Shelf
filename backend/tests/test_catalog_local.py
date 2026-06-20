"""Resolving locally-imported works into the catalog (metadata match + surface in discovery)."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import book_catalog
from app.models import CatalogWork, Work


def _reset(db):
    for m in (CatalogWork, Work):
        db.execute(delete(m))
    db.commit()


def _local_work(db, title, author=None):
    w = Work(title=title, author=author, media_kind="text", local_path=f"/lib/{title}.epub")
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


@pytest.mark.asyncio
async def test_links_provider_match(monkeypatch):
    init_db()
    db = SessionLocal()
    _reset(db)
    w = _local_work(db, "Dune", "Frank Herbert")
    db.add(CatalogWork(provider="openlibrary", domain="openlibrary.org", work_url="/works/Dune",
                       norm_key="dune", title="Dune", author="Frank Herbert", media_kind="text",
                       cover_url="http://cover/dune.jpg"))
    db.commit()

    async def noop(db_, q, **k):
        return 0  # the catalog row already exists; don't hit the network
    monkeypatch.setattr(book_catalog, "resolve_live", noop)

    assert await book_catalog.resolve_local_to_catalog(db, w) is True
    cw = db.scalar(select(CatalogWork).where(CatalogWork.norm_key == "dune"))
    assert cw.hooked_work_id == w.id
    db.refresh(w)
    assert w.cover_url == "http://cover/dune.jpg"  # backfilled from the match
    # Idempotent: already catalogued → no-op.
    assert await book_catalog.resolve_local_to_catalog(db, w) is False


@pytest.mark.asyncio
async def test_creates_local_entry_when_no_provider_match(monkeypatch):
    init_db()
    db = SessionLocal()
    _reset(db)
    w = _local_work(db, "Some Obscure Local Book", "Nobody")

    async def noop(db_, q, **k):
        return 0
    monkeypatch.setattr(book_catalog, "resolve_live", noop)

    assert await book_catalog.resolve_local_to_catalog(db, w) is True
    cw = db.scalar(select(CatalogWork).where(CatalogWork.hooked_work_id == w.id))
    assert cw is not None and cw.provider == "local" and cw.work_url == f"local:{w.id}"


@pytest.mark.asyncio
async def test_wrong_author_not_mislinked(monkeypatch):
    init_db()
    db = SessionLocal()
    _reset(db)
    w = _local_work(db, "Ambiguous", "Alice Realwriter")
    db.add(CatalogWork(provider="openlibrary", domain="d", work_url="u", norm_key="ambiguous",
                       title="Ambiguous", author="Bob Studyguide", media_kind="text"))
    db.commit()

    async def noop(db_, q, **k):
        return 0
    monkeypatch.setattr(book_catalog, "resolve_live", noop)

    assert await book_catalog.resolve_local_to_catalog(db, w) is True
    cw = db.scalar(select(CatalogWork).where(CatalogWork.hooked_work_id == w.id))
    # Did NOT hijack the wrong-author edition; created a local entry instead.
    assert cw.provider == "local"
