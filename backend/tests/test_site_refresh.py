"""Finished-site refresh: every 'done' generic web-index site is periodically re-crawled to
discover newly-published titles (parity with comix.to's API re-page), re-scanning only its
listing/browse surface — never re-fetching already-crawled work pages."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion.indexer import refresh_finished_sites
from app.models import IndexedPage, IndexSite


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for m in (IndexedPage, IndexSite):
        db.execute(delete(m))
    db.commit()
    db.close()
    yield


def _site(db, domain="novellunar.com", status="done") -> IndexSite:
    site = IndexSite(root_url=f"https://{domain}/", domain=domain, status=status,
                     max_pages=500, max_depth=4)
    db.add(site); db.commit(); db.refresh(site)
    return site


def _page(db, site, url, *, priority, depth, fetched_ago_h, status="fetched") -> IndexedPage:
    p = IndexedPage(
        site_id=site.id, url=url, status=status, priority=priority, depth=depth,
        fetched_at=datetime.now(UTC) - timedelta(hours=fetched_ago_h),
    )
    db.add(p); db.commit(); db.refresh(p)
    return p


def test_refreshes_listing_surface_not_work_pages():
    db = SessionLocal()
    site = _site(db)
    root = _page(db, site, "https://novellunar.com/", priority=0, depth=0, fetched_ago_h=48)
    listing = _page(db, site, "https://novellunar.com/browse?p=3", priority=1, depth=2, fetched_ago_h=48)
    work = _page(db, site, "https://novellunar.com/novel/some-novel", priority=2, depth=1, fetched_ago_h=48)

    n = refresh_finished_sites(db, datetime.now(UTC))
    assert n == 1
    db.refresh(site); db.refresh(root); db.refresh(listing); db.refresh(work)
    assert site.status == "active"                 # re-activated to drain the re-queued pages
    assert root.status == "pending"                # root re-scanned (entry point)
    assert listing.status == "pending"             # deep listing/pagination re-scanned for new links
    assert work.status == "fetched"                # already-crawled work NOT re-fetched (discovery only)
    db.close()


def test_skips_recently_crawled_site():
    db = SessionLocal()
    site = _site(db)
    _page(db, site, "https://novellunar.com/browse", priority=1, depth=1, fetched_ago_h=1)
    assert refresh_finished_sites(db, datetime.now(UTC)) == 0   # newest fetch < 12h → not due
    db.refresh(site)
    assert site.status == "done"
    db.close()


def test_skips_api_catalog_site():
    db = SessionLocal()
    site = _site(db, domain="comix.to")
    _page(db, site, "https://comix.to/browse", priority=1, depth=1, fetched_ago_h=72)
    # comix.to re-discovers via its own API re-page, not the HTML refresh.
    assert refresh_finished_sites(db, datetime.now(UTC)) == 0
    db.refresh(site)
    assert site.status == "done"
    db.close()


def test_applies_to_every_generic_source():
    db = SessionLocal()
    for dom in ("www.webtoons.com", "www.gutenberg.org", "royalroad.com", "j-novel.club"):
        site = _site(db, domain=dom)
        _page(db, site, f"https://{dom}/browse", priority=1, depth=1, fetched_ago_h=48)
    assert refresh_finished_sites(db, datetime.now(UTC)) == 4   # all generic sources, uniformly
    db.close()
