"""Wave F #13 — the overseerr-style admin Wanted dashboard rails (requests tagged by user/status/lang)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import ledger
from app.main import app
from app.models import (
    CatalogWork, ContentRequest, ContentRequestRequester, ListSubscription, Subscription,
    User, UserSession, Work,
)


@pytest.fixture
def client():
    init_db()
    db = SessionLocal()
    for m in (ContentRequestRequester, ContentRequest, CatalogWork, ListSubscription,
              Subscription, Work, UserSession, User):
        db.execute(delete(m))
    db.commit()
    db.close()
    c = TestClient(app)
    c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
    c.post("/api/users", json={"username": "joe", "password": "test1234", "role": "user"})
    yield c


def _login(u):
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": u, "password": "test1234"})
    return c


def _seed(db):
    joe = db.scalar(select(User).where(User.username == "joe"))
    cw = CatalogWork(provider="openlibrary", provider_ref="r", domain="d", work_url="u1",
                     title="Sult", author="Hamsun", media_kind="text", norm_key="sult", language="no")
    db.add(cw); db.commit(); db.refresh(cw)
    ledger.note_request(db, cw, user_id=joe.id)                 # request tagged to joe
    db.add(Work(title="Owned Ebook", media_kind="text", hooked=True))
    db.add(Work(title="An Audiobook", media_kind="audio", local_path="/x.m4b"))
    db.add(ListSubscription(user_id=joe.id, provider="goodreads", list_ref="L1",
                            display_name="Joe's list", variant="ebook"))
    db.add(Subscription(user_id=joe.id, kind="author", key="hamsun",
                        display_name="Knut Hamsun", active=True, auto_request=True, auto_added=0))
    db.commit()
    return joe


def test_dashboard_admin_rails_are_tagged(client):
    db = SessionLocal()
    joe = _seed(db)
    db.close()

    r = _login("admin").get("/api/wanted/dashboard?scope=global")
    assert r.status_code == 200, r.text
    d = r.json()
    # Recent Requests — tagged by requester, status and language.
    assert any(x["title"] == "Sult" for x in d["recent_requests"])
    req = next(x for x in d["recent_requests"] if x["title"] == "Sult")
    assert req["language"] == "no"
    assert req["status"] and req["state"]
    assert "joe" in (req["requesters"] or [])
    # Recently added ebooks / audiobooks rails.
    assert any(x["title"] == "Owned Ebook" for x in d["recent_ebooks"])
    assert any(x["title"] == "An Audiobook" for x in d["recent_audiobooks"])
    # Tracked (imported) lists + tracking (followed series/authors) + per-user request breakdown.
    assert any(x["display_name"] == "Joe's list" for x in d["tracked_lists"])
    assert any(x["display_name"] == "Knut Hamsun" and x["kind"] == "author" for x in d["tracking"])
    assert any(u["user_id"] == joe.id for u in d["user_requests"])


def test_dashboard_me_scope_is_per_user(client):
    db = SessionLocal()
    _seed(db)
    db.close()
    # A normal user gets THEIR OWN dashboard (200), scoped to them, with NO per-user breakdown.
    r = _login("joe").get("/api/wanted/dashboard")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["user_requests"] == []                                   # breakdown is admin-global only
    assert any(x["title"] == "Sult" for x in d["recent_requests"])    # joe's own request
    assert any(x["display_name"] == "Knut Hamsun" for x in d["tracking"])  # joe's own follow
    # An admin asking scope=global still gets the whole-instance breakdown.
    g = _login("admin").get("/api/wanted/dashboard?scope=global").json()
    assert len(g["user_requests"]) >= 1
    # An admin's default (me) scope has no breakdown.
    m = _login("admin").get("/api/wanted/dashboard").json()
    assert m["user_requests"] == []
