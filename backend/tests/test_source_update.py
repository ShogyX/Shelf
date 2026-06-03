"""Editing a source's budget takes effect live and lets a stuck web_index crawl continue:
the value persists, the live fetcher budget is re-synced, and budget-stranded pages + crawl
cooldowns are cleared so the index crawl resumes without a restart."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import adapters  # noqa: F401 — registers built-in adapters
from app.ingestion.base import registry
from app.ingestion.engine import ensure_source, get_fetcher
from app.main import app
from app.models import IndexedPage, IndexSite, Source, User


@pytest.fixture
def client_admin():
    init_db()
    s = SessionLocal()
    for m in (IndexedPage, IndexSite):
        s.execute(delete(m))
    s.execute(delete(User))
    s.commit()
    s.close()
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
        yield c


def test_budget_change_takes_effect_and_unsticks_crawl(client_admin):
    db = SessionLocal()
    src = ensure_source(db, registry.get("web_index"))
    src.max_daily_requests = 2000
    site = IndexSite(root_url="https://x.com", domain="x.com", status="active",
                     cooldown_until=datetime.now(UTC) + timedelta(hours=1), consecutive_errors=3)
    db.add(site)
    db.commit()
    db.refresh(site)
    db.add(IndexedPage(site_id=site.id, url="https://x.com/p", status="failed", attempts=3,
                       last_error="daily budget of 2000 requests exhausted"))
    src_id = src.id
    db.commit()
    db.close()

    r = client_admin.patch(f"/api/sources/{src_id}", json={"max_daily_requests": 0})
    assert r.status_code == 200
    assert r.json()["max_daily_requests"] == 0  # 0 = unlimited, persisted

    # Live fetcher budget re-synced.
    assert get_fetcher()._budget("web_index").max_daily_requests == 0

    db = SessionLocal()
    site = db.scalar(select(IndexSite).where(IndexSite.root_url == "https://x.com"))
    page = db.scalar(select(IndexedPage).where(IndexedPage.url == "https://x.com/p"))
    db.close()
    assert site.cooldown_until is None and site.consecutive_errors == 0  # cooldown lifted
    assert page.status == "pending" and page.attempts == 0               # budget page recovered
