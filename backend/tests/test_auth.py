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


def test_password_change_and_demotion_revoke_sessions():
    """S4: a forced password reset / admin demotion must kill existing sessions — a stale or
    compromised session must not keep (admin) access after the credential or role changed."""
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "root", "password": "rootpw12"})
        admin.post("/api/users", json={"username": "bob", "password": "bobpass12", "role": "user"})
        with TestClient(app) as bob:
            bob.post("/api/auth/login", json={"username": "bob", "password": "bobpass12"})
            assert bob.get("/api/works").status_code == 200
            uid = next(u["id"] for u in admin.get("/api/users").json()
                       if u["username"] == "bob")
            # password change → bob's live session is revoked
            assert admin.patch(f"/api/users/{uid}",
                               json={"password": "newpass99"}).status_code == 200
            assert bob.get("/api/works").status_code == 401
            # …and he can log in with the new password
            bob.post("/api/auth/login", json={"username": "bob", "password": "newpass99"})
            assert bob.get("/api/works").status_code == 200

        # promotion → demotion: the admin-scoped session dies on demotion
        admin.post("/api/users", json={"username": "eve", "password": "evepass12",
                                       "role": "admin"})
        with TestClient(app) as eve:
            eve.post("/api/auth/login", json={"username": "eve", "password": "evepass12"})
            assert eve.get("/api/users").status_code == 200          # admin scope live
            eid = next(u["id"] for u in admin.get("/api/users").json()
                       if u["username"] == "eve")
            assert admin.patch(f"/api/users/{eid}", json={"role": "user"}).status_code == 200
            assert eve.get("/api/users").status_code == 401          # stale-admin session dead


def test_fail_log_sweep_bounds_memory():
    """S5: unique attacker-controlled usernames must not grow the throttle dict forever —
    the sweep drops every expired key, and the hard cap bounds the worst case."""
    import time
    import app.auth as a
    with a._fail_lock:
        a._fail_log.clear()
        a._last_sweep = 0.0
    for i in range(50):
        a.record_login_failure(f"user:ghost{i}", "ip:1.2.3.4")
    assert len(a._fail_log) == 51
    # Age everything past the window, force a sweep via the next query.
    from app.config import get_settings
    window = get_settings().login_window_seconds
    with a._fail_lock:
        for k in a._fail_log:
            a._fail_log[k] = [t - window - 5 for t in a._fail_log[k]]
        a._last_sweep = time.time() - window - 5
    a.login_retry_after("user:whoever")
    assert len(a._fail_log) == 0                       # ALL expired keys swept, not just queried

    # hard cap: at _MAX_FAIL_KEYS the oldest key is evicted instead of growing
    with a._fail_lock:
        a._fail_log.clear()
        a._last_sweep = time.time()                    # suppress sweep so the cap path is hit
    old_cap = a._MAX_FAIL_KEYS
    a._MAX_FAIL_KEYS = 10
    try:
        for i in range(15):
            a.record_login_failure(f"k{i}")
        assert len(a._fail_log) <= 10
    finally:
        a._MAX_FAIL_KEYS = old_cap
        with a._fail_lock:
            a._fail_log.clear()


def test_smtp_refuses_plaintext_auth():
    """S3: credentials must never be sent over an unencrypted SMTP connection."""
    from app import kindle

    class FakeServer:
        def __init__(self, *a, **k): ...
        def ehlo(self): ...
        def login(self, u, p):
            raise AssertionError("login must not be reached over cleartext")
        def send_message(self, m): ...
        def quit(self): ...

    cfg = kindle.SmtpConfig(host="mail.x", port=25, username="u", password="p",
                            sender="s@x", starttls=False, ssl=False)
    import smtplib
    orig = smtplib.SMTP
    smtplib.SMTP = FakeServer
    try:
        with pytest.raises(RuntimeError, match="unencrypted"):
            kindle.send_document(cfg, to_email="t@x", subject="s", body="b",
                                 attachment=b"x", filename="f.epub")
    finally:
        smtplib.SMTP = orig


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
