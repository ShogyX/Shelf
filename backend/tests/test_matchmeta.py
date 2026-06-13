"""Shared match-metadata: type buckets, compatibility penalties, title variants, and the
read/fetch/persist path (network mocked)."""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.ingestion import matchmeta as mm
from app.models import CatalogWork


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    s.execute(delete(CatalogWork))
    s.commit()
    mm._attempted.clear()   # reset the in-process "recently fetched" guard so ids don't leak between tests
    yield s
    s.close()


def _cw(db, **over):
    base = dict(domain="d", work_url="u", title="Attack on Titan", author="Hajime Isayama",
                norm_key="attack-on-titan", media_kind="comic")
    base.update(over)
    cw = CatalogWork(**base)
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


def test_bucket_of():
    assert mm.bucket_of("Book") == mm.PROSE
    assert mm.bucket_of("Journal Article") == mm.ARTICLE
    assert mm.bucket_of("Comic Book") == mm.COMIC          # comic checked before the broad 'book'
    assert mm.bucket_of("Comics issue") == mm.COMIC         # real libgen badge — plural
    assert mm.bucket_of("Journal article") == mm.ARTICLE    # real libgen badge
    assert mm.bucket_of("manga") == mm.COMIC
    assert mm.bucket_of(None, media_kind="text") == mm.PROSE
    assert mm.bucket_of(None, media_kind="comic") == mm.COMIC
    assert mm.bucket_of("") == mm.UNKNOWN


def test_type_compat_penalize_never_zero():
    assert mm.type_compat(mm.PROSE, mm.PROSE) == 1.0
    assert mm.type_compat(mm.PROSE, mm.UNKNOWN) == 1.0     # unknown → no penalty (fallback)
    assert mm.type_compat(mm.UNKNOWN, mm.ARTICLE) == 1.0
    assert mm.type_compat(mm.PROSE, mm.ARTICLE) < 0.5      # article for a book → strong sink
    assert mm.type_compat(mm.PROSE, mm.COMIC) < 1.0
    assert mm.type_compat(mm.PROSE, mm.ARTICLE) > 0.0      # penalize, never drop to zero


def test_clean_search_title():
    assert mm.clean_search_title("Dune (Illustrated)") == "Dune"
    assert mm.clean_search_title("Dune: The Complete Edition") == "Dune"
    assert mm.clean_search_title("  Jane   Eyre ") == "Jane Eyre"


@pytest.mark.asyncio
async def test_get_work_meta_reads_persisted_without_fetch(db, monkeypatch):
    cw = _cw(db, extra={"alt_titles": ["Shingeki no Kyojin"], "content_type": "manga",
                        "match_meta_at": "2026-01-01T00:00:00"})

    async def _boom(_cw):
        raise AssertionError("must not fetch when already persisted")
    monkeypatch.setattr(mm, "_fetch_match_meta", _boom)

    meta = await mm.get_work_meta(db, cw)
    assert "Attack on Titan" in meta.titles and "Shingeki no Kyojin" in meta.titles
    assert meta.bucket == mm.COMIC


@pytest.mark.asyncio
async def test_get_work_meta_fetches_once_and_persists(db, monkeypatch):
    cw = _cw(db, extra=None)
    calls = {"n": 0}

    async def _fake(_cw):
        calls["n"] += 1
        return ["Shingeki no Kyojin", "AoT"], "comic"
    monkeypatch.setattr(mm, "_fetch_match_meta", _fake)

    meta = await mm.get_work_meta(db, cw)
    assert calls["n"] == 1
    assert "Shingeki no Kyojin" in meta.titles
    assert (cw.extra or {}).get("alt_titles") and (cw.extra or {}).get("match_meta_at")

    # A second call must NOT fetch again (persisted marker).
    meta2 = await mm.get_work_meta(db, cw)
    assert calls["n"] == 1 and "Shingeki no Kyojin" in meta2.titles


@pytest.mark.asyncio
async def test_get_work_meta_fetch_failure_is_safe(db, monkeypatch):
    import httpx
    cw = _cw(db, extra=None)

    async def _fail(_cw):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(mm, "_fetch_match_meta", _fail)

    meta = await mm.get_work_meta(db, cw)               # degrades to title-only, no exception
    assert meta.titles == ["Attack on Titan"] and meta.bucket == mm.COMIC
    assert not (cw.extra or {}).get("match_meta_at")    # unmarked → a later search can retry
