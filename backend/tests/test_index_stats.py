"""Crawl observability: per-site + aggregate index stats."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.main import app
from app.models import CatalogWork, IndexedPage, IndexSite, User


@pytest.fixture
def client_admin():
    init_db()
    db = SessionLocal()
    for model in (CatalogWork, IndexedPage, IndexSite):
        db.execute(delete(model))
    db.execute(delete(User))
    db.commit()
    db.close()
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
        yield c


def _seed(db):
    now = datetime.now(UTC)
    # Site A: done, 2 fetched + 1 failed pages, 2 catalog titles.
    a = IndexSite(root_url="https://a.com", domain="a.com", status="done",
                  created_at=now - timedelta(seconds=120))
    # Site B: active, 1 fetched + 1 pending.
    b = IndexSite(root_url="https://b.com", domain="b.com", status="active",
                  created_at=now - timedelta(seconds=30))
    db.add_all([a, b])
    db.commit()
    db.refresh(a)
    db.refresh(b)
    db.add_all([
        IndexedPage(site_id=a.id, url="https://a.com/1", status="fetched", word_count=100,
                    fetched_at=now - timedelta(seconds=60)),
        IndexedPage(site_id=a.id, url="https://a.com/2", status="fetched", word_count=200,
                    fetched_at=now - timedelta(seconds=40)),
        IndexedPage(site_id=a.id, url="https://a.com/x", status="failed",
                    fetched_at=now - timedelta(seconds=50)),
        IndexedPage(site_id=b.id, url="https://b.com/1", status="fetched", word_count=50,
                    fetched_at=now - timedelta(seconds=10)),
        IndexedPage(site_id=b.id, url="https://b.com/2", status="pending"),
    ])
    db.add_all([
        CatalogWork(site_id=a.id, domain="a.com", work_url="https://a.com/novel/x",
                    norm_key="x", title="X"),
        CatalogWork(site_id=a.id, domain="a.com", work_url="https://a.com/novel/y",
                    norm_key="y", title="Y"),
    ])
    db.commit()
    return a.id, b.id


def test_index_stats_aggregate(client_admin):
    db = SessionLocal()
    _seed(db)
    db.close()
    s = client_admin.get("/api/index/stats").json()
    assert s["sites_total"] == 2
    assert s["sites_active"] == 1 and s["sites_done"] == 1
    assert s["pages_total"] == 5
    assert s["pages_fetched"] == 3 and s["pages_failed"] == 1 and s["pages_pending"] == 1
    assert s["titles_found"] == 2
    assert s["requests_made"] == 4  # fetched + failed
    assert s["words_indexed"] == 350
    assert s["time_spent_seconds"] > 0


def test_per_site_stats_fields(client_admin):
    db = SessionLocal()
    a_id, b_id = _seed(db)
    db.close()
    sites = {x["id"]: x for x in client_admin.get("/api/index/sites").json()}
    a = sites[a_id]
    assert a["titles_found"] == 2
    assert a["requests"] == 3        # 2 fetched + 1 failed
    assert a["duration_seconds"] > 0
    # Active site's timer runs to "now" (≥ its 30s age).
    assert sites[b_id]["duration_seconds"] >= 25
