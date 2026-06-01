"""Auth: setup, login, route gating, per-user settings isolation, admin user mgmt."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.main import app
from app.models import ReadingState, User, UserSession, UserSettings


@pytest.fixture(autouse=True)
def _clean_auth():
    """Each auth test starts from a fresh (no-users) instance + reset throttling."""
    import app.auth as _a
    init_db()
    db = SessionLocal()
    for model in (UserSession, ReadingState, UserSettings, User):
        db.execute(delete(model))
    db.commit()
    db.close()
    with _a._fail_lock:
        _a._fail_log.clear()
    yield


def test_auth_flow_and_gating():
    with TestClient(app) as c:
        # Unauthenticated API access is rejected.
        assert c.get("/api/works").status_code == 401  # noqa: E501

        # Fresh instance reports it needs setup.
        me = c.get("/api/auth/me").json()
        assert me["needs_setup"] is True and me["authenticated"] is False

        # Password policy: too-short is rejected.
        assert c.post("/api/auth/setup", json={"username": "a", "password": "short"}).status_code == 422  # noqa: E501

        # First admin via setup → logs us in (cookie set on the client).
        r = c.post("/api/auth/setup", json={"username": "alice", "password": "hunter2pw"})
        assert r.status_code == 200 and r.json()["role"] == "admin"

        me = c.get("/api/auth/me").json()
        assert me["authenticated"] is True and me["user"]["username"] == "alice"
        assert me["needs_setup"] is False

        # Now authenticated requests succeed.
        assert c.get("/api/works").status_code == 200  # noqa: E501

        # Setup can't be run twice.
        assert c.post("/api/auth/setup", json={"username": "x", "password": "yyyyyyyy"}).status_code == 409  # noqa: E501

        # logout clears the session.
        assert c.post("/api/auth/logout").status_code == 200  # noqa: E501
        assert c.get("/api/works").status_code == 401  # noqa: E501


def test_admin_user_management_and_per_user_settings():
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "root", "password": "rootpw12"})
        # admin creates a normal user
        r = admin.post("/api/users",
                       json={"username": "reader", "password": "readerpw", "role": "user"})
        assert r.status_code == 200 and r.json()["role"] == "user"
        assert len(admin.get("/api/users").json()) == 2

        # admin sets a distinctive theme
        admin.put("/api/settings", json={"theme": "gruvbox-dark"})
        assert admin.get("/api/settings").json()["theme"] == "gruvbox-dark"

        # the reader logs in on a separate client → their settings are independent
        with TestClient(app) as reader:
            reader.post("/api/auth/login", json={"username": "reader", "password": "readerpw"})
            assert reader.get("/api/settings").json()["theme"] != "gruvbox-dark"
            reader.put("/api/settings", json={"theme": "nord"})
            assert reader.get("/api/settings").json()["theme"] == "nord"

            # a normal user cannot manage users
            assert reader.get("/api/users").status_code == 403  # noqa: E501

        # admin's theme is unchanged by the reader's edit
        assert admin.get("/api/settings").json()["theme"] == "gruvbox-dark"

        # cannot delete the last admin / self
        me_id = admin.get("/api/auth/me").json()["user"]["id"]
        assert admin.delete(f"/api/users/{me_id}").status_code == 400  # noqa: E501


def test_bad_login_rejected():
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "adminpw1"})
        c.post("/api/auth/logout")
        assert c.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401  # noqa: E501
        assert c.post("/api/auth/login", json={"username": "ghost", "password": "x"}).status_code == 401  # noqa: E501


def test_login_brute_force_lockout():
    from app.config import get_settings
    n = get_settings().login_max_attempts
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "boss", "password": "bosspass1"})
        c.post("/api/auth/logout")
        # Exhaust the attempt budget with wrong passwords → eventually 429 (locked out).
        codes = [c.post("/api/auth/login",
                        json={"username": "boss", "password": "nope"}).status_code
                 for _ in range(n + 2)]
        assert 429 in codes, codes
        # Even the CORRECT password is refused while locked out.
        assert c.post("/api/auth/login",
                      json={"username": "boss", "password": "bosspass1"}).status_code == 429


def test_security_headers_and_docs_disabled():
    with TestClient(app) as c:
        r = c.get("/api/health")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert "Content-Security-Policy" in r.headers
        # Interactive docs are off by default in production.
        assert c.get("/docs").status_code in (404, 401)
        assert c.get("/openapi.json").status_code in (404, 401)


def test_secure_cookie_behind_https_proxy():
    from app.config import get_settings
    s = get_settings()
    s.trust_proxy = True
    try:
        with TestClient(app) as c:
            r = c.post("/api/auth/setup",
                       json={"username": "p", "password": "longenough"},
                       headers={"X-Forwarded-Proto": "https"})
            assert r.status_code == 200
            setc = r.headers.get("set-cookie", "").lower()
            assert "secure" in setc and "httponly" in setc and "samesite=lax" in setc
    finally:
        s.trust_proxy = False


def test_setup_token_gate(monkeypatch):
    from app.config import get_settings
    get_settings().setup_token = "s3cr3t-token"
    try:
        with TestClient(app) as c:
            # Wrong/missing token is refused even though no users exist.
            assert c.post("/api/auth/setup",
                          json={"username": "a", "password": "longenough"}).status_code == 403  # noqa: E501
            ok = c.post("/api/auth/setup",
                        json={"username": "a", "password": "longenough", "token": "s3cr3t-token"})
            assert ok.status_code == 200  # noqa: E501
    finally:
        get_settings().setup_token = ""
