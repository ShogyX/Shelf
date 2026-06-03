"""Source removal/re-add lifecycle: removing a source keeps its indexed material (soft delete),
re-adding resumes WITHOUT re-crawling what was already indexed, and only an explicit purge (or
the opt-in update_indexed) touches the stored content."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.db import SessionLocal, init_db
from app.ingestion import adapters  # noqa: F401 — registers built-in adapters (web_index)
from app.ingestion import indexer
from app.ingestion.base import registry
from app.ingestion.engine import ensure_source
from app.main import app
from app.models import CatalogWork, IndexedPage, IndexSite, User


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    for m in (CatalogWork, IndexedPage, IndexSite):
        s.execute(delete(m))
    s.execute(delete(User))  # let each client_admin re-run one-time /auth/setup cleanly
    s.commit()
    ensure_source(s, registry.get("web_index"))  # enable the source so start_index's gate passes
    yield s
    s.close()


@pytest.fixture
def client_admin(db):
    db.close()  # the client opens its own sessions
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
        yield c


def _indexed_site(db, *, status="active"):
    """A site with one fetched root page + one pending child + a catalog entry."""
    site = IndexSite(root_url="https://x.com", domain="x.com", status=status, max_depth=8)
    db.add(site)
    db.commit()
    db.refresh(site)
    db.add_all([
        IndexedPage(site_id=site.id, url="https://x.com", depth=0, status="fetched",
                    word_count=120, title="Home"),
        IndexedPage(site_id=site.id, url="https://x.com/p2", depth=1, status="pending"),
        IndexedPage(site_id=site.id, url="https://x.com/dead", depth=1, status="failed"),
    ])
    db.add(CatalogWork(site_id=site.id, domain="x.com", work_url="https://x.com/novel/a",
                       norm_key="a", title="A"))
    db.commit()
    return site


def _counts(db, site_id):
    pages = db.scalar(
        select(func.count(IndexedPage.id)).where(IndexedPage.site_id == site_id)
    ) or 0
    cats = db.scalar(
        select(func.count(CatalogWork.id)).where(CatalogWork.site_id == site_id)
    ) or 0
    return pages, cats


# ---- start_index: re-add resumes without repeating, unless update_indexed -------------------
def test_readd_resumes_without_refetching(db):
    """Re-adding a removed/finished site reactivates it but leaves already-fetched pages alone —
    only the still-pending frontier remains to be crawled (no repeat)."""
    site = _indexed_site(db, status="removed")
    out = indexer.start_index(db, "https://x.com")
    db.refresh(site)
    assert out.id == site.id            # reused the existing site, not a fresh crawl
    assert site.status == "active"
    statuses = dict(db.execute(
        select(IndexedPage.status, func.count(IndexedPage.id))
        .where(IndexedPage.site_id == site.id).group_by(IndexedPage.status)
    ).all())
    # The fetched page stays fetched (not re-queued); the pending one is still queued.
    assert statuses.get("fetched") == 1
    assert statuses.get("pending") == 1


def test_readd_with_update_indexed_requeues_everything(db):
    """update_indexed=True re-queues every already-processed page (fetched/failed/skipped) so the
    crawl re-fetches and refreshes them."""
    site = _indexed_site(db, status="removed")
    indexer.start_index(db, "https://x.com", update_indexed=True)
    statuses = dict(db.execute(
        select(IndexedPage.status, func.count(IndexedPage.id))
        .where(IndexedPage.site_id == site.id).group_by(IndexedPage.status)
    ).all())
    # fetched + failed + the already-pending one are all pending now → full re-crawl/refresh.
    assert statuses.get("pending") == 3
    assert "fetched" not in statuses and "failed" not in statuses


# ---- delete endpoint: soft remove keeps content, purge deletes it ---------------------------
def test_delete_soft_removes_and_keeps_content(client_admin):
    db = SessionLocal()
    site = _indexed_site(db)
    sid = site.id
    db.close()

    r = client_admin.request("DELETE", f"/api/index/sites/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["purged"] is False and body["removed"] == sid

    db = SessionLocal()
    refreshed = db.get(IndexSite, sid)
    pages, cats = _counts(db, sid)
    db.close()
    assert refreshed is not None and refreshed.status == "removed"  # site kept, just stopped
    assert pages == 3 and cats == 1                                 # all indexed material kept


def test_delete_purge_removes_everything(client_admin):
    db = SessionLocal()
    site = _indexed_site(db)
    sid = site.id
    db.close()

    r = client_admin.request("DELETE", f"/api/index/sites/{sid}", params={"purge": "true"})
    assert r.status_code == 200
    assert r.json()["purged"] is True

    db = SessionLocal()
    refreshed = db.get(IndexSite, sid)
    pages, cats = _counts(db, sid)
    db.close()
    assert refreshed is None              # site row gone
    assert pages == 0 and cats == 0       # pages + catalog entries purged


async def test_removed_site_is_skipped_by_index_tick(db, monkeypatch):
    """A soft-removed site must not be crawled, and (unlike a 'done' site) must not be revived by
    index_tick's self-heal even though it still has pending pages."""
    site = _indexed_site(db, status="removed")

    class _Fetcher:
        def __init__(self):
            self.calls: list[str] = []

        async def get_html(self, source_key, url, **kw):
            self.calls.append(url)
            return type("R", (), {"status_code": 200, "text": "<html></html>", "headers": {}})()

    fetcher = _Fetcher()
    monkeypatch.setattr(indexer, "get_fetcher", lambda: fetcher)
    await indexer.index_tick()
    await indexer._drain_crawl_tasks()
    db.refresh(site)
    assert site.status == "removed"   # not revived, not crawled
    assert fetcher.calls == []        # its pending page was never fetched
