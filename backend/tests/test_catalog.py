"""Smart catalog: cataloging discovered works + cross-site dedup/grouping + hook gate."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

import app.ingestion.adapters  # noqa: F401  — populate the adapter registry
from app.db import SessionLocal, init_db
from app.ingestion import catalog
from app.ingestion.engine import ComplianceError
from app.models import CatalogWork, IndexedPage, IndexSite

# Reuse the regression fixtures so catalog + extract stay in lock-step.
from tests.test_extract import NOVELLUNAR_CHAPTER_HTML, NOVELLUNAR_NOVEL_HTML


@pytest.fixture(autouse=True)
def _clean_catalog():
    """Each test starts with an empty catalog (the test DB is shared across tests)."""
    init_db()
    db = SessionLocal()
    for model in (CatalogWork, IndexedPage, IndexSite):
        db.execute(delete(model))
    db.commit()
    db.close()
    yield


def _site(db, domain="novellunar.com") -> IndexSite:
    site = IndexSite(root_url=f"https://{domain}/", domain=domain, status="active",
                     max_pages=50, max_depth=3)
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def test_upsert_catalogs_a_novel_landing_page():
    init_db()
    db = SessionLocal()
    site = _site(db)
    url = "https://novellunar.com/novel/library-of-heavens-path-v1"
    entry = catalog.upsert_from_page(db, site, NOVELLUNAR_NOVEL_HTML, url)
    assert entry is not None
    assert entry.work_url == url
    assert "Library of Heaven" in entry.title
    assert entry.chapters_advertised == 2271
    assert entry.chapters_listed >= 4
    assert entry.synopsis and "Zhang Xuan" in entry.synopsis
    assert entry.cover_url and entry.cover_url.endswith("lohp.webp")
    assert entry.norm_key == "library of heavens path"
    db.close()


def test_chapter_page_attributes_to_parent_work():
    init_db()
    db = SessionLocal()
    site = _site(db)
    ch_url = "https://novellunar.com/novel/library-of-heavens-path-v1/chapter/1"
    entry = catalog.upsert_from_page(db, site, NOVELLUNAR_CHAPTER_HTML, ch_url)
    assert entry is not None
    # The catalog entry is keyed by the WORK url, not the chapter url.
    assert entry.work_url == "https://novellunar.com/novel/library-of-heavens-path-v1"
    # Only one entry exists for the work even though we fed a chapter page.
    assert db.scalar(select(CatalogWork).where(CatalogWork.site_id == site.id)) is entry
    db.close()


def test_non_literature_pages_are_not_catalogued():
    init_db()
    db = SessionLocal()
    site = _site(db, "example.org")
    assert catalog.upsert_from_page(
        db, site, "<html><body><p>hi</p></body></html>", "https://example.org/account/login"
    ) is None
    assert catalog.upsert_from_page(
        db, site, "<html><body>browse</body></html>", "https://example.org/browse/popular"
    ) is None
    assert db.scalar(select(CatalogWork)) is None
    db.close()


def test_landing_page_enriches_a_chapter_seeded_entry():
    init_db()
    db = SessionLocal()
    site = _site(db)
    ch_url = "https://novellunar.com/novel/library-of-heavens-path-v1/chapter/1"
    catalog.upsert_from_page(db, site, NOVELLUNAR_CHAPTER_HTML, ch_url)
    landing = "https://novellunar.com/novel/library-of-heavens-path-v1"
    entry = catalog.upsert_from_page(db, site, NOVELLUNAR_NOVEL_HTML, landing)
    # Same row, now enriched with the advertised count + synopsis from the landing page.
    assert db.scalar(select(CatalogWork)) is entry
    assert entry.chapters_advertised == 2271
    assert entry.synopsis
    db.close()


def test_group_rows_dedups_same_title_across_sites():
    init_db()
    db = SessionLocal()
    s1 = _site(db, "novellunar.com")
    s2 = _site(db, "othersite.net")
    catalog.upsert_from_page(db, s1, NOVELLUNAR_NOVEL_HTML,
                             "https://novellunar.com/novel/library-of-heavens-path-v1")
    # A second source with the same work, different cosmetic title.
    other = NOVELLUNAR_NOVEL_HTML.replace(
        "Library of Heaven's Path Novel", "The Library of Heaven's Path (Web Novel)"
    )
    catalog.upsert_from_page(db, s2, other, "https://othersite.net/book/lohp")

    rows = catalog.find_rows(db)
    groups = catalog.group_rows(rows)
    assert len(groups) == 1, [g["title"] for g in groups]
    assert len(groups[0]["sources"]) == 2
    domains = {s["domain"] for s in groups[0]["sources"]}
    assert domains == {"novellunar.com", "othersite.net"}
    db.close()


def test_catalog_search_matches_synopsis_and_title():
    init_db()
    db = SessionLocal()
    site = _site(db)
    catalog.upsert_from_page(db, site, NOVELLUNAR_NOVEL_HTML,
                             "https://novellunar.com/novel/library-of-heavens-path-v1")
    assert catalog.find_rows(db, q="heaven")          # title
    assert catalog.find_rows(db, q="Zhang Xuan")      # synopsis
    assert not catalog.find_rows(db, q="nonexistent zzzzz")
    db.close()


def test_delete_work_clears_catalog_and_page_hooked_pointers():
    from app.models import IndexedPage, Work
    from app.routers.works import delete_work

    init_db()
    db = SessionLocal()
    site = _site(db)
    work = Work(title="Hooked Title", hooked=True)
    db.add(work)
    db.commit()
    db.refresh(work)
    cw = CatalogWork(site_id=site.id, domain=site.domain, work_url="https://x/n/1",
                     title="Hooked Title", norm_key="hooked title",
                     hooked_work_id=work.id, health="ok")
    page = IndexedPage(site_id=site.id, url="https://x/p/1", status="fetched",
                       hooked_work_id=work.id)
    db.add_all([cw, page])
    db.commit()

    delete_work(work.id, db)

    db.refresh(cw)
    db.refresh(page)
    assert cw.hooked_work_id is None and cw.health == "unknown"
    assert page.hooked_work_id is None
    db.close()


@pytest.mark.asyncio
async def test_hook_entry_requires_enabled_adaptive_source():
    init_db()
    db = SessionLocal()
    site = _site(db)
    entry = catalog.upsert_from_page(db, site, NOVELLUNAR_NOVEL_HTML,
                                     "https://novellunar.com/novel/library-of-heavens-path-v1")
    # generic_feed defaults to tos_permitted=False → hooking is refused until enabled.
    with pytest.raises(ComplianceError):
        await catalog.hook_entry(db, entry)
    db.close()
