"""Self-registration (mode-gated), password recovery, admin approval, per-title default shelf."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import config_store, kindle
from app.db import SessionLocal, init_db
from app.main import app
from app.models import (
    Bookshelf,
    LibraryItem,
    PasswordResetToken,
    User,
    UserSession,
    UserSettings,
    Work,
)


@pytest.fixture(autouse=True)
def _clean():
    """Fresh no-users instance, reset throttle, and force registration_mode back to the default
    (config_store persists overrides in a row + process cache, so it must be cleared per test)."""
    import app.auth as _a
    init_db()
    db = SessionLocal()
    for model in (PasswordResetToken, UserSession, LibraryItem, Bookshelf,
                  UserSettings, User, Work):
        db.execute(delete(model))
    db.commit()
    config_store.update(db, {"registration_mode": "closed"})
    db.close()
    with _a._fail_lock:
        _a._fail_log.clear()
    import app.static_auth as _sa
    _sa._cache.clear()
    yield
    # Don't leak an "open"/"approval" override into later tests sharing the process-global cache.
    db = SessionLocal()
    config_store.update(db, {"registration_mode": "closed"})
    db.close()


def _set_mode(mode: str) -> None:
    db = SessionLocal()
    config_store.update(db, {"registration_mode": mode})
    db.close()


# ---------------------------------------------------------------- registration modes
def test_registration_mode_endpoint_public():
    with TestClient(app) as c:
        assert c.get("/api/auth/registration-mode").json() == {"mode": "closed"}
        _set_mode("open")
        assert c.get("/api/auth/registration-mode").json() == {"mode": "open"}


def test_register_closed_is_403():
    with TestClient(app) as c:
        r = c.post("/api/auth/register",
                   json={"username": "newbie", "email": "n@x.com", "password": "longenough"})
        assert r.status_code == 403


def test_register_open_logs_in_active():
    _set_mode("open")
    with TestClient(app) as c:
        r = c.post("/api/auth/register",
                   json={"username": "newbie", "email": "n@x.com", "password": "longenough"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["user"]["username"] == "newbie" and body["user"]["role"] == "user"
        assert body["user"]["approval_status"] == "approved"
        # The session cookie was set → authenticated immediately.
        me = c.get("/api/auth/me").json()
        assert me["authenticated"] is True and me["user"]["username"] == "newbie"
        assert c.get("/api/works").status_code == 200


def test_register_approval_is_pending_and_cannot_login():
    _set_mode("approval")
    with TestClient(app) as c:
        r = c.post("/api/auth/register",
                   json={"username": "waiter", "email": "w@x.com", "password": "longenough"})
        assert r.status_code == 200 and r.json() == {"status": "pending", "user": None}
        # No session was set.
        assert c.get("/api/auth/me").json()["authenticated"] is False
        # And the pending user cannot log in yet.
        login = c.post("/api/auth/login", json={"username": "waiter", "password": "longenough"})
        assert login.status_code == 403


def test_register_duplicate_username_and_email():
    _set_mode("open")
    with TestClient(app) as c:
        c.post("/api/auth/register",
               json={"username": "dup", "email": "dup@x.com", "password": "longenough"})
        # same username
        r1 = c.post("/api/auth/register",
                    json={"username": "dup", "email": "other@x.com", "password": "longenough"})
        assert r1.status_code == 409
        # same email (different username)
        r2 = c.post("/api/auth/register",
                    json={"username": "dup2", "email": "dup@x.com", "password": "longenough"})
        assert r2.status_code == 409


def test_register_password_too_short():
    _set_mode("open")
    with TestClient(app) as c:
        r = c.post("/api/auth/register",
                   json={"username": "shorty", "email": "s@x.com", "password": "short"})
        assert r.status_code == 400


def test_register_bad_email():
    _set_mode("open")
    with TestClient(app) as c:
        r = c.post("/api/auth/register",
                   json={"username": "bad", "email": "not-an-email", "password": "longenough"})
        assert r.status_code == 422


# ---------------------------------------------------------------- forgot / reset password
def _mock_smtp(monkeypatch):
    """Make the forgot-password path believe SMTP is configured and capture send_message calls."""
    sent: list[tuple] = []
    monkeypatch.setattr(kindle, "smtp_configured", lambda cfg: True)
    monkeypatch.setattr(kindle, "app_smtp", lambda db: kindle.SmtpConfig(host="m", sender="s@x"))
    monkeypatch.setattr(kindle, "send_message",
                        lambda cfg, to, subject, body: sent.append((to, subject, body)))
    # A trusted public origin is now required to build the reset link (Host-poisoning guard).
    from app.routers import auth as _auth
    monkeypatch.setattr(_auth.settings, "public_base_url", "https://shelf.test", raising=False)
    return sent


def test_forgot_unknown_identifier_no_enumeration(monkeypatch):
    sent = _mock_smtp(monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/auth/forgot-password", json={"identifier": "ghost@nowhere.com"})
        assert r.status_code == 200 and r.json() == {"ok": True}
    # No token created, no email sent — but the response is identical to the known-account path.
    db = SessionLocal()
    assert db.scalar(select(PasswordResetToken.id)) is None
    db.close()
    assert sent == []


def test_forgot_known_email_creates_token_and_sends(monkeypatch):
    sent = _mock_smtp(monkeypatch)
    _set_mode("open")
    with TestClient(app) as c:
        c.post("/api/auth/register",
               json={"username": "rec", "email": "rec@x.com", "password": "longenough"})
        r = c.post("/api/auth/forgot-password", json={"identifier": "rec@x.com"})
        assert r.status_code == 200 and r.json() == {"ok": True}
    db = SessionLocal()
    tok = db.scalar(select(PasswordResetToken))
    assert tok is not None and tok.used_at is None
    db.close()
    assert len(sent) == 1
    to, _subject, body = sent[0]
    # The link host comes from the trusted public_base_url, NOT the request Host (poisoning guard).
    assert to == "rec@x.com" and "https://shelf.test/reset?token=" in body


def test_forgot_without_trusted_base_sends_nothing(monkeypatch):
    """Host-poisoning guard: with no public_base_url and an unrestricted allowed_hosts, the reset
    link host can't be trusted — so NO token is minted and NO email is sent (still a generic 200)."""
    sent: list[tuple] = []
    monkeypatch.setattr(kindle, "smtp_configured", lambda cfg: True)
    monkeypatch.setattr(kindle, "app_smtp", lambda db: kindle.SmtpConfig(host="m", sender="s@x"))
    monkeypatch.setattr(kindle, "send_message",
                        lambda cfg, to, subject, body: sent.append((to, subject, body)))
    from app.routers import auth as _auth
    monkeypatch.setattr(_auth.settings, "public_base_url", "", raising=False)
    monkeypatch.setattr(_auth.settings, "allowed_hosts", ["*"], raising=False)
    _set_mode("open")
    with TestClient(app) as c:
        c.post("/api/auth/register",
               json={"username": "rec", "email": "rec@x.com", "password": "longenough"})
        r = c.post("/api/auth/forgot-password", json={"identifier": "rec@x.com"})
        assert r.status_code == 200 and r.json() == {"ok": True}
    db = SessionLocal()
    assert db.scalar(select(PasswordResetToken.id)) is None   # fail-closed: no token minted
    db.close()
    assert sent == []


def test_reset_password_happy_path_revokes_sessions(monkeypatch):
    _mock_smtp(monkeypatch)
    _set_mode("open")
    with TestClient(app) as c:
        c.post("/api/auth/register",
               json={"username": "rec", "email": "rec@x.com", "password": "oldpassword"})
        assert c.get("/api/works").status_code == 200  # logged-in session live
        c.post("/api/auth/forgot-password", json={"identifier": "rec@x.com"})
        db = SessionLocal()
        token = db.scalar(select(PasswordResetToken)).token
        db.close()
        r = c.post("/api/auth/reset-password",
                   json={"token": token, "password": "brandnewpw"})
        assert r.status_code == 200 and r.json() == {"ok": True}
        # The old session was revoked.
        assert c.get("/api/works").status_code == 401
    # token is marked used + password actually changed (new password logs in).
    db = SessionLocal()
    assert db.scalar(select(PasswordResetToken)).used_at is not None
    db.close()
    with TestClient(app) as c2:
        assert c2.post("/api/auth/login",
                       json={"username": "rec", "password": "brandnewpw"}).status_code == 200


def test_reset_invalid_token():
    with TestClient(app) as c:
        r = c.post("/api/auth/reset-password",
                   json={"token": "nope-not-real", "password": "longenough"})
        assert r.status_code == 400


def test_reset_expired_token():
    # Seed a user + an already-expired token.
    db = SessionLocal()
    u = User(username="exp", email="exp@x.com", password_hash="x", role="user")
    db.add(u); db.commit(); db.refresh(u)
    db.add(PasswordResetToken(
        user_id=u.id, token="expired-tok",
        expires_at=datetime.now(UTC) - timedelta(hours=2),
    ))
    db.commit(); db.close()
    with TestClient(app) as c:
        r = c.post("/api/auth/reset-password",
                   json={"token": "expired-tok", "password": "longenough"})
        assert r.status_code == 400


def test_reset_reused_token(monkeypatch):
    _mock_smtp(monkeypatch)
    _set_mode("open")
    with TestClient(app) as c:
        c.post("/api/auth/register",
               json={"username": "rec", "email": "rec@x.com", "password": "oldpassword"})
        c.post("/api/auth/forgot-password", json={"identifier": "rec@x.com"})
        db = SessionLocal()
        token = db.scalar(select(PasswordResetToken)).token
        db.close()
        assert c.post("/api/auth/reset-password",
                      json={"token": token, "password": "firstreset"}).status_code == 200
        # Reusing the same token is rejected.
        assert c.post("/api/auth/reset-password",
                      json={"token": token, "password": "secondreset"}).status_code == 400


# ---------------------------------------------------------------- admin approve / reject
def test_admin_approve_then_login_works():
    _set_mode("approval")
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "root", "password": "rootpw12"})
        with TestClient(app) as anon:
            anon.post("/api/auth/register",
                      json={"username": "pend", "email": "p@x.com", "password": "pendpass1"})
        uid = next(u["id"] for u in admin.get("/api/users").json() if u["username"] == "pend")
        # Listed with pending status + email.
        pend = next(u for u in admin.get("/api/users").json() if u["username"] == "pend")
        assert pend["approval_status"] == "pending" and pend["email"] == "p@x.com"
        # Still can't log in before approval.
        with TestClient(app) as u:
            assert u.post("/api/auth/login",
                          json={"username": "pend", "password": "pendpass1"}).status_code == 403
        assert admin.post(f"/api/users/{uid}/approve").status_code == 200
        # Now login works.
        with TestClient(app) as u:
            assert u.post("/api/auth/login",
                          json={"username": "pend", "password": "pendpass1"}).status_code == 200


def test_admin_reject_removes_user():
    _set_mode("approval")
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "root", "password": "rootpw12"})
        with TestClient(app) as anon:
            anon.post("/api/auth/register",
                      json={"username": "spam", "email": "s@x.com", "password": "spampass1"})
        uid = next(u["id"] for u in admin.get("/api/users").json() if u["username"] == "spam")
        assert admin.post(f"/api/users/{uid}/reject").status_code == 200
        assert all(u["username"] != "spam" for u in admin.get("/api/users").json())


# ---------------------------------------------------------------- per-title default shelf
def _seed_member_work_and_shelf(owner_id: int, *, shelf_owner_id: int | None = None):
    """Create a Work in owner's library + a Bookshelf owned by shelf_owner (defaults to owner)."""
    db = SessionLocal()
    w = Work(title="A Title"); db.add(w); db.commit(); db.refresh(w)
    db.add(LibraryItem(user_id=owner_id, work_id=w.id))
    shelf = Bookshelf(user_id=shelf_owner_id or owner_id, name="Shelf")
    db.add(shelf); db.commit(); db.refresh(w); db.refresh(shelf)
    wid, sid = w.id, shelf.id
    db.close()
    return wid, sid


def test_default_shelf_set_get_roundtrip_and_ownership():
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "root", "password": "rootpw12"})
        uid = admin.get("/api/auth/me").json()["user"]["id"]
        wid, sid = _seed_member_work_and_shelf(uid)
        # set
        r = admin.put(f"/api/works/{wid}/default-shelf", json={"shelf_id": sid})
        assert r.status_code == 200 and r.json()["default_shelf_id"] == sid
        # round-trips on the work-detail GET
        assert admin.get(f"/api/works/{wid}").json()["default_shelf_id"] == sid
        # clear
        assert admin.put(f"/api/works/{wid}/default-shelf",
                         json={"shelf_id": None}).json()["default_shelf_id"] is None
        assert admin.get(f"/api/works/{wid}").json()["default_shelf_id"] is None

        # a shelf owned by ANOTHER user is rejected (404, not silently stored).
        admin.post("/api/users", json={"username": "other", "password": "otherpw1", "role": "user"})
        other_id = next(u["id"] for u in admin.get("/api/users").json()
                        if u["username"] == "other")
        _, foreign_sid = _seed_member_work_and_shelf(uid, shelf_owner_id=other_id)
        assert admin.put(f"/api/works/{wid}/default-shelf",
                         json={"shelf_id": foreign_sid}).status_code == 404
