"""Security: send-to-kindle relays via the GLOBAL admin SMTP server, so an arbitrary recipient
would be an authenticated open relay. The recipient must be a Kindle delivery domain or one of the
requesting user's own saved addresses."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.main import app
from app.models import (
    AppSetting,
    LibraryItem,
    User,
    UserSession,
    UserSettings,
    Work,
)


@pytest.fixture
def client(monkeypatch):
    init_db()
    db = SessionLocal()
    for m in (LibraryItem, Work, UserSession, UserSettings, User):
        db.execute(delete(m))
    db.execute(delete(AppSetting))
    db.commit()
    db.close()

    c = TestClient(app)
    c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})

    db = SessionLocal()
    uid = db.scalar(select(User.id).where(User.username == "admin"))
    # Give the user a personal email + a global SMTP server so delivery is "configured".
    user = db.get(User, uid)
    user.email = "me@example.com"
    db.add(AppSetting(key="global_smtp", value={
        "smtp_host": "smtp.example.com", "smtp_from": "shelf@example.com",
    }))
    w = Work(title="A Book", media_kind="text")
    db.add(w)
    db.commit()
    db.refresh(w)
    db.add(LibraryItem(user_id=uid, work_id=w.id))
    db.commit()
    wid = w.id
    db.close()

    sent: list[str] = []
    import app.routers.delivery as delivery
    monkeypatch.setattr(delivery, "send_document",
                        lambda cfg, **kw: sent.append(kw["to_email"]))
    # Recipient validation happens before EPUB assembly; stub the build so the test stays focused.
    monkeypatch.setattr(delivery, "_make_epub", lambda db, work, start, limit: (b"epub", "a.epub", 1))
    return c, wid, sent


def test_arbitrary_recipient_is_rejected(client):
    c, wid, sent = client
    r = c.post(f"/api/works/{wid}/send-to-kindle", json={"to": "attacker@evil.com"})
    assert r.status_code == 400
    assert sent == []


def test_kindle_address_is_allowed(client):
    c, wid, sent = client
    r = c.post(f"/api/works/{wid}/send-to-kindle", json={"to": "reader@kindle.com"})
    assert r.status_code == 200, r.text
    assert sent == ["reader@kindle.com"]


def test_free_kindle_address_is_allowed(client):
    c, wid, sent = client
    r = c.post(f"/api/works/{wid}/send-to-kindle", json={"to": "reader@free.kindle.com"})
    assert r.status_code == 200, r.text
    assert sent == ["reader@free.kindle.com"]


def test_users_own_email_is_allowed(client):
    c, wid, sent = client
    r = c.post(f"/api/works/{wid}/send-to-kindle", json={"to": "me@example.com"})
    assert r.status_code == 200, r.text
    assert sent == ["me@example.com"]
