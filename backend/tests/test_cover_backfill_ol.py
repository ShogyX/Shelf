"""The OpenLibrary cover backfill fills crawled PROSE (Gutenberg/web book) covers — the big gap the
comic (AniList) + book-provider backfills miss — and advances a persistent cursor so it works DOWN the
catalog instead of re-scanning the same unclearable top rows forever."""
import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import catalog_enrichment as ce
from app.models import AppSetting, CatalogGroup


@pytest.mark.asyncio
async def test_ol_cover_backfill_fills_text_and_advances_cursor(monkeypatch):
    init_db()
    db = SessionLocal()
    db.execute(delete(CatalogGroup)); db.execute(delete(AppSetting)); db.commit()
    # Three prose groups missing a cover (descending popularity) + one comic (must be ignored).
    g_hi = CatalogGroup(norm_key="littlewomen", title="Little Women", media_bucket="text", popularity_norm=0.9)
    g_mid = CatalogGroup(norm_key="cityofgod", title="The City of God", media_bucket="text", popularity_norm=0.5)
    comic = CatalogGroup(norm_key="pluto", title="Pluto", media_bucket="comic", popularity_norm=0.99)
    db.add_all([g_hi, g_mid, comic]); db.commit()

    class _Hit:
        cover_url = "https://covers.openlibrary.org/b/id/1-M.jpg"
    async def _ol(client, *, title, author, limit):
        return [_Hit()] if "City of God" in title else []   # the TOP row (Little Women) is unfillable
    monkeypatch.setattr("app.ingestion.book_catalog._ol_search", _ol)
    monkeypatch.setattr("app.imagecache.cache_cover", lambda url, **k: "/covers/ol.jpg")

    # Run 1 (limit 1): scans the most-popular prose row — which has NO OL cover — and advances the cursor
    # past it instead of jamming there.
    out1 = await ce.backfill_openlibrary_covers(db, limit=1)
    db.refresh(g_hi); db.refresh(comic)
    assert out1["scanned"] == 1 and out1["filled"] == 0       # top row unfillable, nothing filled
    assert comic.cover_url in (None, "")                       # comic bucket untouched
    assert db.get(AppSetting, ce._COVER_CURSOR_KEY).value == 0.9   # cursor parked at the scanned row

    # Run 2 advances DOWN to the next prose row and fills it (NOT stuck re-scanning the unfillable top).
    out2 = await ce.backfill_openlibrary_covers(db, limit=1)
    db.refresh(g_mid)
    assert out2["filled"] == 1 and g_mid.cover_url == "/covers/ol.jpg"
    db.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_ol_cover_backfill_fills_text_and_advances_cursor(lambda *a, **k: None))  # type: ignore
    print("ok")
