"""Wave D #14 — admin-controlled content languages (what Shelf grabs/stocks) + visibility."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app import config_store
from app.db import SessionLocal, init_db
from app.main import app
from app.models import User, UserSession


@pytest.fixture
def client():
    init_db()
    db = SessionLocal()
    for m in (UserSession, User):
        db.execute(delete(m))
    db.commit()
    config_store.update(db, {"content_languages": "en"})
    db.close()
    c = TestClient(app)
    c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
    c.post("/api/users", json={"username": "joe", "password": "test1234", "role": "user"})
    yield c
    db = SessionLocal()
    config_store.update(db, {"content_languages": "en"})  # don't leak into other tests
    db.close()


def _login(u):
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": u, "password": "test1234"})
    return c


def test_get_content_languages_visible_to_all(client):
    joe = _login("joe")
    r = joe.get("/api/settings/content-languages")
    assert r.status_code == 200
    data = r.json()
    assert {l["code"] for l in data["supported"]} == {"en", "no"}
    assert data["enabled"] == ["en"]


def test_set_content_languages_is_admin_only_and_validated(client):
    joe = _login("joe")
    assert joe.put("/api/settings/content-languages",
                   json={"languages": ["en", "no"]}).status_code == 403
    admin = _login("admin")
    r = admin.put("/api/settings/content-languages", json={"languages": ["no", "en", "bogus"]})
    assert r.status_code == 200
    assert set(r.json()["enabled"]) == {"en", "no"}          # unsupported 'bogus' dropped
    # clearing the selection falls back to English (never 'all'/unrestricted)
    r2 = admin.put("/api/settings/content-languages", json={"languages": []})
    assert r2.json()["enabled"] == ["en"]
