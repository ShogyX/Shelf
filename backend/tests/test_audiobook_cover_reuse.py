"""Audiobooks reuse their matching ebook's cover (same normalized title); the UI tags the card with an
'Audio' badge so the shared image is unambiguous. Covers ebook_cover_for() + backfill_audiobook_covers()."""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.ingestion.catalog_enrichment import backfill_audiobook_covers, ebook_cover_for
from app.ingestion.extract import norm_title
from app.models import CatalogGroup, CatalogWork, Source, Work


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for m in (CatalogGroup, CatalogWork, Work, Source):
        db.execute(delete(m))
    db.commit()
    db.close()
    yield


def _ebook_cw(db, title="The Hobbit", cover="https://img/hobbit.jpg"):
    cw = CatalogWork(provider="googlebooks", provider_ref="r", domain="d", work_url="u",
                     title=title, author="Tolkien", media_kind="text",
                     norm_key=norm_title(title), cover_url=cover)
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


def _audiobook(db, title="The Hobbit", cover=None):
    src = db.scalar(__import__("sqlalchemy").select(Source).where(Source.key == "local"))
    if src is None:
        src = Source(key="local", display_name="Local", adapter_key="local", tos_permitted=True)
        db.add(src); db.commit(); db.refresh(src)
    w = Work(source_id=src.id, source_work_ref=f"audiobook:{title}", title=title,
             media_kind="audio", local_path="/audiobooks/x", cover_url=cover)
    db.add(w); db.commit(); db.refresh(w)
    return w


def test_ebook_cover_for_prefers_then_falls_back():
    db = SessionLocal()
    _ebook_cw(db)
    nk = norm_title("The Hobbit")
    # a usable `prefer` wins outright (the acquired CatalogWork's own cover)
    assert ebook_cover_for(db, nk, "https://good/x.jpg") == "https://good/x.jpg"
    # blocked `prefer` (blank / evicted imgcache / comix CDN) → fall back to the cluster cover
    assert ebook_cover_for(db, nk, "") == "https://img/hobbit.jpg"
    assert ebook_cover_for(db, nk, "/media/imgcache/ab.jpg") == "https://img/hobbit.jpg"
    assert ebook_cover_for(db, nk, "http://comix.to/p.jpg") == "https://img/hobbit.jpg"
    # no cluster + no usable prefer → None (UI draws a generated cover)
    assert ebook_cover_for(db, "no-such-title", None) is None
    db.close()


def test_group_cover_preferred_over_catalogwork():
    db = SessionLocal()
    _ebook_cw(db, cover="https://img/cw.jpg")
    nk = norm_title("The Hobbit")
    db.add(CatalogGroup(norm_key=nk, media_bucket="text", title="The Hobbit",
                        cover_url="https://img/group.jpg"))
    db.commit()
    assert ebook_cover_for(db, nk) == "https://img/group.jpg"   # rolled-up group cover wins
    db.close()


def test_backfill_fills_audiobook_from_ebook():
    db = SessionLocal()
    _ebook_cw(db)
    audio = _audiobook(db, cover=None)
    res = backfill_audiobook_covers(db)
    assert res["filled"] == 1
    db.refresh(audio)
    assert audio.cover_url == "https://img/hobbit.jpg"
    # idempotent: a second pass fills nothing (already covered)
    assert backfill_audiobook_covers(db)["filled"] == 0
    db.close()


def test_backfill_skips_when_no_ebook_match():
    db = SessionLocal()
    audio = _audiobook(db, title="Some Orphan Audiobook", cover=None)
    assert backfill_audiobook_covers(db)["filled"] == 0
    db.refresh(audio)
    assert audio.cover_url is None       # no match → left for the generated cover
    db.close()
