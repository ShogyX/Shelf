"""Auth: setup, login, route gating, per-user settings isolation, admin user mgmt."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.main import app
from app.models import ReadingState, User, UserSession, UserSettings


@pytest.fixture(autouse=True)
def _clean_auth():
    """Each auth test starts from a fresh (no-users) instance + reset throttling."""
    import app.auth as _a
    from app.safety import require_destructive_ok
    require_destructive_ok("test_auth table reset")  # must never run against the prod DB
    init_db()
    db = SessionLocal()
    for model in (UserSession, ReadingState, UserSettings, User):
        db.execute(delete(model))
    db.commit()
    db.close()
    with _a._fail_lock:
        _a._fail_log.clear()
    import app.static_auth as _sa
    _sa._cache.clear()
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


def test_setup_dedupes_legacy_null_user_reading_states():
    """F4.1: first-admin setup claims legacy global (NULL-user) reading_states. If a work has
    duplicate NULL rows, claiming them all would violate uq_reading_user_work and crash setup —
    they must be de-duped to the furthest-progressed one first."""
    from app.models import Chapter, ReadingState, Work
    db = SessionLocal()
    db.execute(delete(ReadingState)); db.execute(delete(Chapter)); db.execute(delete(Work))
    db.commit()
    w = Work(title="Legacy"); db.add(w); db.commit(); db.refresh(w)
    c1 = Chapter(work_id=w.id, index=1, source_chapter_ref="r1")
    c2 = Chapter(work_id=w.id, index=2, source_chapter_ref="r2")
    db.add_all([c1, c2]); db.commit(); db.refresh(c1); db.refresh(c2)
    # two legacy global rows for the SAME work (no user) — the duplicate that would collide
    db.add(ReadingState(user_id=None, work_id=w.id, last_chapter_id=c1.id, scroll_fraction=0.1))
    db.add(ReadingState(user_id=None, work_id=w.id, last_chapter_id=c2.id, scroll_fraction=0.9))
    db.commit()
    db.close()

    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={"username": "root", "password": "rootpw12"})
        assert r.status_code == 200                       # setup did NOT crash on the duplicate
    db = SessionLocal()
    rows = db.scalars(select(ReadingState).where(ReadingState.work_id == w.id)).all()
    assert len(rows) == 1                                 # de-duped to one
    assert rows[0].last_chapter_id == c2.id              # kept the furthest-progressed
    assert rows[0].user_id is not None                   # claimed by the admin
    db.close()


def test_setup_fails_closed_behind_untrusted_proxy():
    """S6: with no setup token AND trust_proxy off, a request carrying forwarding headers (so the
    apparent-local client IP is really the proxy) must be refused — a stranger behind the proxy
    can't claim admin."""
    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={"username": "x", "password": "xxxxxxxx"},
                   headers={"X-Forwarded-For": "203.0.113.7"})
        assert r.status_code == 403          # fail-closed
    # No forwarding header → local testclient request still allowed (fail-open for genuine local).
    with TestClient(app) as c:
        assert c.post("/api/auth/setup",
                      json={"username": "y", "password": "yyyyyyyy"}).status_code == 200


def test_sanitize_blocks_data_uri_img_src():
    """S6: <img src> data:/javascript: schemes are stripped (only http(s)/relative allowed)."""
    from app.sanitize import sanitize_html
    out = sanitize_html('<p><img src="data:image/png;base64,AAAA" alt="x">'
                        '<img src="/media/ok.jpg" alt="ok">'
                        '<img src="https://cdn.example/p.jpg"></p>')
    assert "data:image" not in out                     # data-URI src stripped
    assert "/media/ok.jpg" in out and "cdn.example" in out   # safe srcs kept


def test_media_and_covers_require_session(tmp_path):
    """S2: comic page imagery + cached chapter images (and covers) must not be served to an
    unauthenticated client — same per-user isolation as the API."""
    from app.covers import covers_dir
    from app.media import media_dir
    media_f = media_dir() / "imgcache"
    media_f.mkdir(parents=True, exist_ok=True)
    (media_f / "secret-page.txt").write_text("PRIVATE COMIC PAGE")
    (covers_dir()).mkdir(parents=True, exist_ok=True)
    (covers_dir() / "secret-cover.txt").write_text("COVER")
    try:
        with TestClient(app) as c:
            c.post("/api/auth/setup", json={"username": "owner", "password": "ownerpw1"})
            # authenticated (cookie set by setup) → served
            assert c.get("/media/imgcache/secret-page.txt").status_code == 200
            assert c.get("/covers/secret-cover.txt").status_code == 200
            c.post("/api/auth/logout")
        # a fresh client with no session → blocked
        with TestClient(app) as anon:
            assert anon.get("/media/imgcache/secret-page.txt").status_code == 401
            assert anon.get("/covers/secret-cover.txt").status_code == 401
    finally:
        (media_f / "secret-page.txt").unlink(missing_ok=True)
        (covers_dir() / "secret-cover.txt").unlink(missing_ok=True)


def test_logout_all_evicts_static_cache_immediately(tmp_path):
    """AUTHZ-2: a force-logged-out session must stop serving /media within the same request, not lag
    the cache TTL — logout-all evicts the token from the static-file positive cache."""
    from app.covers import covers_dir
    from app.media import media_dir
    import app.static_auth as _sa
    media_f = media_dir() / "imgcache"
    media_f.mkdir(parents=True, exist_ok=True)
    (media_f / "authz2-page.txt").write_text("PRIVATE")
    try:
        with TestClient(app) as admin:
            admin.post("/api/auth/setup", json={"username": "alice", "password": "hunter2pw"})
            bob = admin.post("/api/users", json={
                "username": "bob", "password": "bobpassword", "role": "user"}).json()
            cb = TestClient(app)
            cb.post("/api/auth/login", json={"username": "bob", "password": "bobpassword"})
            # Bob fetches an image → his token is now in the positive cache.
            assert cb.get("/media/imgcache/authz2-page.txt").status_code == 200
            assert _sa._cache, "the fetch should have populated the positive token cache"
            # Admin forces sign-out → the cache entry must be gone, so the next image 401s at once.
            admin.post(f"/api/users/{bob['id']}/logout-all")
            assert cb.get("/media/imgcache/authz2-page.txt").status_code == 401
    finally:
        (media_f / "authz2-page.txt").unlink(missing_ok=True)


def test_forgot_password_cannot_lock_victim_reset():
    """DOS-1: repeated forgot-password for one email must not 429 that email's OWN reset flow — the
    per-identifier counter was removed; only the abuser's per-IP bucket fills."""
    from app.config import get_settings
    import app.auth as _a
    n = get_settings().login_max_attempts
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "alice", "password": "hunter2pw"})
        # Hammer the SAME identifier many times. Each still returns ok (no enumeration) and, crucially,
        # never writes a per-identifier failure key — so the victim's reset bucket stays empty.
        for _ in range(n + 3):
            c.post("/api/auth/forgot-password", json={"identifier": "alice"})
        assert "forgot:alice" not in _a._fail_log


def test_health_readiness_probe():
    """F0.6: /health is a real readiness probe — DB query + disk + WAL, not a static {ok}."""
    with TestClient(app) as c:
        r = c.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok" and body["db"] == "ok"
        assert "disk_free_mb" in body and isinstance(body["disk_free_mb"], int)


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


def test_user_last_seen_and_force_logout():
    """list_users derives last_seen + active_sessions from sessions; logout-all revokes them."""
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "alice", "password": "hunter2pw"})
        bob = c.post("/api/users", json={
            "username": "bob", "password": "bobpassword", "role": "user", "email": "bob@x.com",
        })
        assert bob.status_code == 200, bob.text
        bob_id = bob.json()["id"]

        # Before bob signs in: no sessions, never seen.
        row = next(u for u in c.get("/api/users").json() if u["username"] == "bob")
        assert row["active_sessions"] == 0 and row["last_seen"] is None

        # Bob signs in on his own cookie jar → a session exists.
        cb = TestClient(app)
        assert cb.post("/api/auth/login", json={"username": "bob", "password": "bobpassword"}).status_code == 200
        assert cb.get("/api/works").status_code == 200
        row = next(u for u in c.get("/api/users").json() if u["username"] == "bob")
        assert row["active_sessions"] >= 1 and row["last_seen"] is not None

        # Admin forces sign-out everywhere → bob's session is dead.
        assert c.post(f"/api/users/{bob_id}/logout-all").json()["revoked"] >= 1
        assert cb.get("/api/works").status_code == 401
        row = next(u for u in c.get("/api/users").json() if u["username"] == "bob")
        assert row["active_sessions"] == 0


def test_admin_can_edit_email_with_uniqueness():
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "alice", "password": "hunter2pw"})
        bob = c.post("/api/users", json={"username": "bob", "password": "bobpassword", "role": "user"}).json()
        c.post("/api/users", json={"username": "dan", "password": "danpassword", "role": "user", "email": "dan@x.com"})
        # Set bob's email.
        assert c.patch(f"/api/users/{bob['id']}", json={"email": "bob@x.com"}).json()["email"] == "bob@x.com"
        # Colliding with dan's email is rejected.
        assert c.patch(f"/api/users/{bob['id']}", json={"email": "dan@x.com"}).status_code == 409
        # CODE-M2: admin path validates format and lowercases, matching public register.
        assert c.patch(f"/api/users/{bob['id']}", json={"email": "notanemail"}).status_code == 422
        assert c.patch(f"/api/users/{bob['id']}", json={"email": "Bob@EXAMPLE.com"}).json()["email"] == "bob@example.com"
        # Create-path validates too.
        assert c.post("/api/users", json={"username": "eve", "password": "evepassword", "email": "bad"}).status_code == 422
        # Empty string clears it back to null.
        assert c.patch(f"/api/users/{bob['id']}", json={"email": ""}).json()["email"] is None


def test_deleting_user_purges_per_user_settings():
    """ARCH-H2: a deleted user's string-keyed per-user AppSetting (route-priority override) must be
    removed, not leak forever (no FK cascades it)."""
    from app.db import SessionLocal
    from app.models import AppSetting
    from app.ingestion.acquire import _user_key, set_user_priority
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "alice", "password": "hunter2pw"})
        bob = c.post("/api/users", json={"username": "bob", "password": "bobpassword", "role": "user"}).json()
        db = SessionLocal()
        set_user_priority(db, bob["id"], ["pipeline", "torrent"])  # creates fetch_source_priority:user:N
        assert db.get(AppSetting, _user_key(bob["id"])) is not None
        db.close()
        assert c.delete(f"/api/users/{bob['id']}").status_code == 200
        db = SessionLocal()
        assert db.get(AppSetting, _user_key(bob["id"])) is None  # cascaded on delete
        db.close()
