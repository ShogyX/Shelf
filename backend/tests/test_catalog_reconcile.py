"""Catalog reconciliation: titles already crawled (their IndexedPage survives) whose CatalogWork
went missing are rebuilt from STORED page content — no network re-fetch — so they reappear in the
Index. Mirrors recovery from a catalog wipe."""
from __future__ import annotations

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import delete, select

import app.ingestion.adapters  # noqa: F401  — populate the adapter registry
from app.db import SessionLocal, init_db
from app.ingestion import catalog
from app.ingestion.extract import og_title, page_metadata
from app.models import AppSetting, CatalogWork, IndexedPage, IndexSite

from tests.test_extract import NOVELLUNAR_CHAPTER_HTML, NOVELLUNAR_NOVEL_HTML

_CURSOR_KEY = "catalog_reconcile_cursor"


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for model in (CatalogWork, IndexedPage, IndexSite):
        db.execute(delete(model))
    db.execute(delete(AppSetting).where(AppSetting.key == _CURSOR_KEY))
    db.commit()
    db.close()
    yield


def _site(db, domain="novellunar.com") -> IndexSite:
    site = IndexSite(root_url=f"https://{domain}/", domain=domain, status="done",
                     max_pages=50, max_depth=3)
    db.add(site); db.commit(); db.refresh(site)
    return site


def _stored_page(db, site, full_html: str, url: str) -> IndexedPage:
    """Create a 'fetched' IndexedPage the way the live crawl stores one: sanitized BODY-only HTML
    (no <head>) plus the metadata that was extracted from the full page at fetch time."""
    meta = page_metadata(full_html, url)
    body = str(BeautifulSoup(full_html, "lxml").body or full_html)  # drop <head>/og: tags
    page = IndexedPage(
        site_id=site.id, url=url, status="fetched",
        html=body, title=og_title(full_html) or None,
        description=meta["description"], author=meta["author"], cover_url=meta["cover_url"],
        site_name=meta["site_name"], page_type=meta["type"], depth=1, priority=2,
    )
    db.add(page); db.commit(); db.refresh(page)
    return page


def test_rebuilds_missing_entry_from_stored_page():
    db = SessionLocal()
    site = _site(db)
    url = "https://novellunar.com/novel/library-of-heavens-path-v1"
    _stored_page(db, site, NOVELLUNAR_NOVEL_HTML, url)
    # The catalog is empty (entry was wiped) — a normal crawl would never re-fetch this 'fetched'
    # page, so without reconciliation the title stays lost.
    assert db.scalar(select(CatalogWork.id)) is None

    res = catalog.reconcile_catalog_tick(db, limit=50)
    assert res["rebuilt"] == 1
    entry = db.scalar(select(CatalogWork))
    assert entry is not None
    assert entry.work_url == url
    assert "Library of Heaven" in entry.title          # title recovered from stored field, not <head>
    assert entry.norm_key == "library of heavens path"
    db.close()


def test_reconcile_is_idempotent_and_skips_cataloged():
    db = SessionLocal()
    site = _site(db)
    url = "https://novellunar.com/novel/library-of-heavens-path-v1"
    _stored_page(db, site, NOVELLUNAR_NOVEL_HTML, url)
    catalog.reconcile_catalog_tick(db, limit=50)
    entry = db.scalar(select(CatalogWork))
    stamp = entry.updated_at
    n = db.scalar(select(CatalogWork.id).where(CatalogWork.id == entry.id))

    # Cursor advanced past the page → a second sweep finds nothing new (no duplicate, no churn).
    res2 = catalog.reconcile_catalog_tick(db, limit=50)
    assert res2["scanned"] == 0
    assert db.scalar(select(CatalogWork.id).where(CatalogWork.id != n)) is None  # still exactly one
    db.refresh(entry)
    assert entry.updated_at == stamp  # untouched
    db.close()


def test_chapter_only_page_is_not_an_authoritative_seed():
    """A bare chapter page isn't rebuilt into a (badly-titled) work — only landing/TOC pages seed."""
    db = SessionLocal()
    site = _site(db)
    ch_url = "https://novellunar.com/novel/library-of-heavens-path-v1/chapter/1"
    _stored_page(db, site, NOVELLUNAR_CHAPTER_HTML, ch_url)
    res = catalog.reconcile_catalog_tick(db, limit=50)
    assert res["rebuilt"] == 0
    assert db.scalar(select(CatalogWork.id)) is None
    db.close()
