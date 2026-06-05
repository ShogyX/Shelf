"""Live-editable crawl identity (User-Agent + contact) and its runtime side-effect."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.ingestion import operator_identity
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


def test_defaults_come_from_config():
    init_db()
    db = SessionLocal()
    db.execute(delete(AppSetting)); db.commit()
    s = get_settings()
    assert operator_identity.get_identity(db) == {
        "user_agent": s.user_agent,
        "contact_email": s.contact_email,
    }
    db.close()


def test_set_persists_trims_and_applies_to_live_fetcher():
    from app.ingestion.engine import get_fetcher
    init_db()
    db = SessionLocal()
    db.execute(delete(AppSetting)); db.commit()

    out = operator_identity.set_identity(
        db, {"user_agent": "Bot/2 (+https://me.example)", "contact_email": "  me@example.org \n"}
    )
    assert out == {"user_agent": "Bot/2 (+https://me.example)", "contact_email": "me@example.org"}
    # pushed onto the running fetcher (no restart) — these are the headers the next fetch sends
    f = get_fetcher()
    assert f.user_agent == "Bot/2 (+https://me.example)"
    assert f.contact_email == "me@example.org"
    # persisted + a partial update leaves the other field intact
    operator_identity.set_identity(db, {"contact_email": "ops@example.org"})
    got = operator_identity.get_identity(db)
    assert got == {"user_agent": "Bot/2 (+https://me.example)", "contact_email": "ops@example.org"}

    db.execute(delete(AppSetting)); db.commit(); db.close()


def test_blank_field_falls_back_to_current_not_empty_header():
    init_db()
    db = SessionLocal()
    db.execute(delete(AppSetting)); db.commit()
    operator_identity.set_identity(db, {"user_agent": "Keep/1", "contact_email": "keep@example.org"})
    # an all-whitespace value must not wipe the header to ""
    out = operator_identity.set_identity(db, {"contact_email": "   "})
    assert out["contact_email"] == "keep@example.org"
    db.execute(delete(AppSetting)); db.commit(); db.close()


def test_identity_endpoints(client_admin):
    s = get_settings()
    r = client_admin.get("/api/operator/identity")
    assert r.status_code == 200
    assert r.json() == {"user_agent": s.user_agent, "contact_email": s.contact_email}

    r = client_admin.put("/api/operator/identity", json={"contact_email": "admin@example.org"})
    assert r.status_code == 200
    body = r.json()
    assert body["contact_email"] == "admin@example.org"
    assert body["user_agent"] == s.user_agent  # untouched

    assert client_admin.get("/api/operator/identity").json()["contact_email"] == "admin@example.org"


def test_identity_put_requires_admin():
    init_db()
    db = SessionLocal()
    db.execute(delete(User)); db.commit(); db.close()
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
        c.post("/api/users", json={"username": "joe", "password": "test1234", "role": "user"})
        c.post("/api/auth/logout")
        c.post("/api/auth/login", json={"username": "joe", "password": "test1234"})
        # GET is allowed for any logged-in user; PUT is admin-only
        assert c.get("/api/operator/identity").status_code == 200
        assert c.put("/api/operator/identity", json={"contact_email": "x@y.z"}).status_code == 403
