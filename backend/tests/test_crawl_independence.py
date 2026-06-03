"""Independent crawls: index_tick runs each site concurrently in its own session and paces each
on its OWN per-domain rate budget — so sites never share one budget or block one another."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, func, select

from app.db import SessionLocal, init_db
from app.ingestion import adapters  # noqa: F401 — registers built-in adapters (web_index)
from app.ingestion import indexer
from app.ingestion.base import registry
from app.ingestion.engine import ensure_source
from app.models import CatalogWork, IndexedPage, IndexSite


class _Resp:
    def __init__(self, status=200, text="<html><body><p>hi there friend</p></body></html>"):
        self.status_code = status
        self.text = text
        self.headers: dict[str, str] = {}


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    for m in (CatalogWork, IndexedPage, IndexSite):
        s.execute(delete(m))
    s.commit()
    ensure_source(s, registry.get("web_index"))  # enable source so the tick runs
    yield s
    s.close()


async def test_index_tick_crawls_each_site_with_its_own_rate_key(db, monkeypatch):
    a = IndexSite(root_url="https://a.com", domain="a.com", status="active", max_depth=3)
    b = IndexSite(root_url="https://b.com", domain="b.com", status="active", max_depth=3)
    db.add_all([a, b])
    db.commit()
    db.refresh(a)
    db.refresh(b)
    db.add_all([
        IndexedPage(site_id=a.id, url="https://a.com/p", depth=0, status="pending"),
        IndexedPage(site_id=b.id, url="https://b.com/p", depth=0, status="pending"),
    ])
    db.commit()

    seen: list[tuple[str, str | None]] = []

    class _Fetcher:
        async def get_html(self, source_key, url, **kw):
            seen.append((url, kw.get("rate_key")))
            return _Resp()

    monkeypatch.setattr(indexer, "get_fetcher", lambda: _Fetcher())

    await indexer.index_tick()
    await indexer._drain_crawl_tasks()

    # Both sites were crawled in the one tick, EACH paced on its own per-domain bucket.
    by_url = dict(seen)
    assert by_url["https://a.com/p"] == "web_index:a.com"
    assert by_url["https://b.com/p"] == "web_index:b.com"

    s = SessionLocal()
    statuses = dict(s.execute(
        select(IndexedPage.status, func.count(IndexedPage.id)).group_by(IndexedPage.status)
    ).all())
    s.close()
    assert statuses.get("fetched", 0) == 2  # both pages fetched independently


async def test_one_site_failing_does_not_stop_the_other(db, monkeypatch):
    """A site whose fetch raises must not prevent the other site from being crawled (each runs in
    its own isolated session)."""
    a = IndexSite(root_url="https://bad.com", domain="bad.com", status="active", max_depth=3)
    b = IndexSite(root_url="https://good.com", domain="good.com", status="active", max_depth=3)
    db.add_all([a, b])
    db.commit()
    db.refresh(a)
    db.refresh(b)
    db.add_all([
        IndexedPage(site_id=a.id, url="https://bad.com/p", depth=0, status="pending"),
        IndexedPage(site_id=b.id, url="https://good.com/p", depth=0, status="pending"),
    ])
    db.commit()

    class _Fetcher:
        async def get_html(self, source_key, url, **kw):
            if "bad.com" in url:
                raise RuntimeError("boom")
            return _Resp()

    monkeypatch.setattr(indexer, "get_fetcher", lambda: _Fetcher())

    await indexer.index_tick()
    await indexer._drain_crawl_tasks()

    s = SessionLocal()
    good = s.scalar(select(IndexedPage).where(IndexedPage.url == "https://good.com/p"))
    s.close()
    assert good.status == "fetched"  # the healthy site still got crawled despite the other failing
