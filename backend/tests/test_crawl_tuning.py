"""Live-editable crawl tuning + its runtime side-effects."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.ingestion import crawl_tuning
from app.main import app
from app.models import AppSetting, User


@pytest.fixture
def client_admin():
    init_db()
    db = SessionLocal()
    db.execute(delete(AppSetting))
    db.execute(delete(User))
    db.commit()
    db.close()
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
        yield c


def test_defaults_are_moderate():
    init_db()
    db = SessionLocal()
    db.execute(delete(AppSetting)); db.commit()
    t = crawl_tuning.get_tuning(db)
    assert t == {"tick_seconds": 10, "chapters_per_tick": 3, "parallel_fetches": 4}
    db.close()


def test_set_and_clamp():
    init_db()
    db = SessionLocal()
    db.execute(delete(AppSetting)); db.commit()
    crawl_tuning.set_tuning(db, {"chapters_per_tick": 5})
    assert crawl_tuning.get_tuning(db)["chapters_per_tick"] == 5
    # out-of-range values are clamped, not rejected
    out = crawl_tuning.set_tuning(db, {"parallel_fetches": 999, "tick_seconds": 0})
    assert out["parallel_fetches"] == 32 and out["tick_seconds"] == 2
    # a partial update leaves other keys intact
    assert crawl_tuning.get_tuning(db)["chapters_per_tick"] == 5
    db.execute(delete(AppSetting)); db.commit(); db.close()


def test_set_tuning_resizes_fetcher():
    init_db()
    db = SessionLocal()
    db.execute(delete(AppSetting)); db.commit()
    from app.ingestion.engine import get_fetcher
    crawl_tuning.set_tuning(db, {"parallel_fetches": 7})
    assert get_fetcher()._concurrency == 7
    crawl_tuning.set_tuning(db, {"parallel_fetches": 3})
    assert get_fetcher()._concurrency == 3
    db.execute(delete(AppSetting)); db.commit(); db.close()


def test_crawl_tuning_endpoints(client_admin):
    r = client_admin.get("/api/index/crawl-tuning")
    assert r.status_code == 200
    assert r.json() == {"tick_seconds": 10, "chapters_per_tick": 3, "parallel_fetches": 4}

    r = client_admin.put("/api/index/crawl-tuning",
                         json={"tick_seconds": 6, "parallel_fetches": 6})
    assert r.status_code == 200
    body = r.json()
    assert body["tick_seconds"] == 6 and body["parallel_fetches"] == 6
    assert body["chapters_per_tick"] == 3  # untouched

    # persisted
    assert client_admin.get("/api/index/crawl-tuning").json()["tick_seconds"] == 6


def test_crawl_tuning_put_requires_admin():
    init_db()
    db = SessionLocal()
    db.execute(delete(User)); db.commit(); db.close()
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
        c.post("/api/users", json={"username": "joe", "password": "test1234", "role": "user"})
        c.post("/api/auth/logout")
        c.post("/api/auth/login", json={"username": "joe", "password": "test1234"})
        # GET is allowed for any user; PUT is admin-only
        assert c.get("/api/index/crawl-tuning").status_code == 200
        assert c.put("/api/index/crawl-tuning", json={"tick_seconds": 5}).status_code == 403
