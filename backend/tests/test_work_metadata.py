"""Wave 5 library display-metadata: provider field surfacing, enrich_work column writes,
catalog→Work sync at hook, and the bounded backfill tick."""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.integrations import metadata as M
from app.integrations import metadata_sync as MS
from app.ingestion.catalog import _sync_catalog_meta
from app.models import CatalogWork, Source, Work


class _Resp:
    def __init__(self, *, status=200, payload=None):
        self.status_code = status
        self._p = payload
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._p


def _src(db):
    src = db.scalar(select(Source).where(Source.key == "generic_feed"))
    if src is None:
        src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                     tos_permitted=True)
        db.add(src); db.commit()
    return src


def test_gbooks_fetch_surfaces_year_rating_publisher(monkeypatch):
    # Google Books fetch should now populate the first-class display fields used by the modal.
    vol = {"id": "gb9", "volumeInfo": {
        "title": "Dune", "authors": ["Frank Herbert"], "publishedDate": "1965-08-01",
        "publisher": "Chilton Books", "averageRating": 4.5, "ratingsCount": 1200,
        "pageCount": 412, "categories": ["Fiction / Science Fiction"],
        "industryIdentifiers": [{"identifier": "9780801950773"}]}}

    async def _get(self, url, **kw):
        return _Resp(payload=vol)
    monkeypatch.setattr(M.GoogleBooksProvider, "_get", _get)
    meta = asyncio.run(M.GoogleBooksProvider().fetch("gb9"))
    assert meta.year == 1965
    assert meta.rating == 9.0           # 4.5/5 → /10 convention
    assert meta.rating_count == 1200
    assert meta.publisher == "Chilton Books"
    assert meta.extra["page_count"] == 412


def test_enrich_work_writes_display_columns_upgrade_not_clobber():
    init_db()
    db = SessionLocal()
    src = _src(db)
    w = Work(source_id=src.id, source_work_ref="disp-1", title="Dune", hooked=True,
             status="ongoing", rating=8.8, rating_count=50)  # a rating already set (specialist)
    db.add(w); db.commit(); db.refresh(w)
    meta = M.ProviderMeta(ref="gb9", title="Dune", media_kind="text", unit_kind="pages",
                          year=1965, rating=9.0, rating_count=1200, publisher="Chilton",
                          genres=["Science Fiction", "Adventure"], total_units=412,
                          extra={"isbn": ["9780801950773"], "page_count": 412})
    MS.enrich_work(db, w, meta, provider_kind="googlebooks")
    assert w.rating == 8.8                 # existing rating preserved (upgrade-not-clobber)
    assert w.rating_count == 50            # count stays paired with the kept rating (not clobbered)
    assert w.year == 1965
    assert w.publisher == "Chilton"
    assert w.page_count == 412
    assert "Science Fiction" in (w.genres or [])
    assert w.identifiers["isbn"] == ["9780801950773"]
    assert w.identifiers["googlebooks"] == "gb9"
    assert w.meta_enriched_at is not None
    assert w.meta_source == "googlebooks"
    db.delete(w); db.commit(); db.close()


def test_sync_catalog_meta_copies_rating_year_genres():
    w = Work(title="T", rating=None, year=None)
    cw = CatalogWork(norm_key="t", title="T", work_url="http://x/t", domain="x",
                     rating=7.4, rating_count=33, year=2011, identity_key="isbn:9781111111111",
                     extra={"genres": [{"slug": "fantasy", "label": "Fantasy"}],
                            "enrich_ref": {"anilist": "555"}})
    _sync_catalog_meta(w, cw)
    assert w.rating == 7.4 and w.rating_count == 33 and w.year == 2011
    assert w.genres == ["Fantasy"]
    assert w.identifiers["isbn"] == ["9781111111111"]
    assert w.identifiers["anilist"] == "555"


def test_backfill_only_unenriched_and_stamps_miss(monkeypatch):
    from sqlalchemy import update
    init_db()
    db = SessionLocal()
    src = _src(db)
    # Isolate from works other tests left behind: stamp every pre-existing one enriched so only the
    # work this test creates is due for the sweep (the tick selects meta_enriched_at IS NULL).
    db.execute(update(Work).where(Work.meta_enriched_at.is_(None))
               .values(meta_enriched_at=MS.datetime.now(MS.UTC), meta_source="none"))
    db.commit()
    fresh = Work(source_id=src.id, source_work_ref="bf-fresh", title="Fresh", hooked=True)
    done = Work(source_id=src.id, source_work_ref="bf-done", title="Done", hooked=True,
                meta_enriched_at=MS.datetime.now(MS.UTC), meta_source="anilist")
    db.add_all([fresh, done]); db.commit()

    seen: list[int] = []

    async def _fake(db_, work, provs):
        seen.append(work.id)  # no provider actually matches → definitive miss
        return False
    monkeypatch.setattr(MS, "_enrich_work_meta", _fake)
    monkeypatch.setattr(MS, "_backfill_providers", lambda db_: [])
    res = asyncio.run(MS.backfill_work_metadata(db, limit=10))
    assert seen == [fresh.id]                         # only the un-enriched work was swept
    assert res["scanned"] == 1
    db.refresh(fresh)
    assert fresh.meta_enriched_at is not None         # miss is stamped so it won't re-sweep
    assert fresh.meta_source == "none"
    db.delete(fresh); db.delete(done); db.commit(); db.close()


def test_narrator_from_ffprobe_tags():
    from app.routers.delivery import _narrator_from_tags
    assert _narrator_from_tags(
        {"format": {"tags": {"album_artist": "Michael Kramer"}}}) == "Michael Kramer"
    assert _narrator_from_tags({"format": {"tags": {"NARRATOR": "Kate Reading"}}}) == "Kate Reading"
    assert _narrator_from_tags({"format": {"tags": {}}}) is None
    assert _narrator_from_tags(None) is None


def test_backfill_outage_leaves_work_due_for_retry(monkeypatch):
    from sqlalchemy import update
    init_db()
    db = SessionLocal()
    src = _src(db)
    db.execute(update(Work).where(Work.meta_enriched_at.is_(None))
               .values(meta_enriched_at=MS.datetime.now(MS.UTC), meta_source="none"))
    db.commit()
    w = Work(source_id=src.id, source_work_ref="bf-outage", title="Outage", hooked=True)
    db.add(w); db.commit()

    async def _down(db_, work, provs):
        return True  # every provider was transiently unavailable (API down / rate-limited)
    monkeypatch.setattr(MS, "_enrich_work_meta", _down)
    monkeypatch.setattr(MS, "_backfill_providers", lambda db_: [])
    asyncio.run(MS.backfill_work_metadata(db, limit=10))
    db.refresh(w)
    assert w.meta_enriched_at is None                 # NOT stamped → retried on the next tick
    db.delete(w); db.commit(); db.close()
