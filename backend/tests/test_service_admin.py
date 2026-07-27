"""GATE-1 — the service-token admin API (/api/admin/*): auth, its own rate-limit bucket, the 409
that carries the existing user's id, the read-back, deactivate/delete, and the Cloudflare silence."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app import service_auth
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.integrations import cloudflare
from app.main import app
from app.models import AppSetting, ReadingState, User, UserSession, UserSettings

# Obviously fake; the instance only ever holds its hash.
TOKEN = "shelf-service-token-EXAMPLE-do-not-use"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _clean_service():
    """Fresh (no-users) instance, one configured service token, and empty rate buckets."""
    import app.auth as _a
    from app.safety import require_destructive_ok
    require_destructive_ok("test_service_admin table reset")   # never against the prod DB
    init_db()
    db = SessionLocal()
    for model in (UserSession, ReadingState, UserSettings, User):
        db.execute(delete(model))
    db.execute(delete(AppSetting).where(AppSetting.key == "cloudflare_access"))
    db.commit(); db.close()
    with _a._fail_lock:
        _a._fail_log.clear()
    with service_auth._lock:
        service_auth._hits.clear()
        service_auth._last_sweep = 0.0
    settings = get_settings()
    settings.service_tokens = [service_auth.token_hash(TOKEN)]
    try:
        yield
    finally:
        settings.service_tokens = []
        # _hits is process-global and this fixture is module-scoped, so leaving a spent budget behind
        # would 429 anything else that ever hits a service-token route (test order is randomised).
        with service_auth._lock:
            service_auth._hits.clear()
            service_auth._last_sweep = 0.0
        # Leave no enabled Cloudflare config behind — a later test's create_user would try to reach
        # the real API with the fake client already restored.
        db = SessionLocal()
        db.execute(delete(AppSetting).where(AppSetting.key == "cloudflare_access"))
        db.commit(); db.close()


def _new_user(username="reader", email="reader@example.com", **extra) -> dict:
    """The create body an external provisioner sends: no categories, no adult opt-in, invite off."""
    return {"username": username, "email": email, "password": "provisionpw1",
            "send_invite": False, **extra}


# ------------------------------------------------------------------------------------ auth
def test_token_accepted_rejected_and_unset_means_disabled():
    settings = get_settings()
    with TestClient(app) as c:
        assert c.get("/api/admin/users", headers=AUTH).status_code == 200      # the real token
        assert c.get("/api/admin/users").status_code == 401                    # no header at all
        assert c.get("/api/admin/users",
                     headers={"Authorization": "Bearer wrong-token"}).status_code == 401
        assert c.get("/api/admin/users",
                     headers={"Authorization": TOKEN}).status_code == 401      # not a Bearer header

        # SHELF_SERVICE_TOKENS holds HASHES: the plaintext token configured as-is authenticates nothing.
        settings.service_tokens = [TOKEN]
        assert c.get("/api/admin/users", headers=AUTH).status_code == 401

        # Unset = the surface is DISABLED, not open — a valid-looking token gets nowhere either.
        settings.service_tokens = []
        assert c.get("/api/admin/users", headers=AUTH).status_code == 401
        assert c.get("/api/admin/users").status_code == 401

    # Comma-separated or JSON-array env values both parse into the hash list.
    from app.config import Settings
    assert Settings(service_tokens="aaa,bbb").service_tokens == ["aaa", "bbb"]


def test_token_comparison_is_constant_time(monkeypatch):
    """Tokens are compared with hmac.compare_digest, over the SHA-256 digests — never the plaintext,
    and never a byte-at-a-time `==` that would leak the matching prefix through timing."""
    seen: list[tuple[str, str]] = []
    real = service_auth.hmac.compare_digest

    class _SpyHmac:
        @staticmethod
        def compare_digest(a, b):
            seen.append((a, b))
            return real(a, b)

    monkeypatch.setattr(service_auth, "hmac", _SpyHmac)
    with TestClient(app) as c:
        assert c.get("/api/admin/users", headers=AUTH).status_code == 200
        assert c.get("/api/admin/users",
                     headers={"Authorization": "Bearer nope"}).status_code == 401
    assert seen, "the token check must go through hmac.compare_digest"
    for a, b in seen:
        assert TOKEN not in (a, b)                                   # the plaintext is never compared
        assert len(a) == len(b) == 64                                # both sides are sha256 digests


def test_no_session_fallback_and_no_service_token_elsewhere():
    """The two halves of 'for those routes alone': a session cannot open /api/admin/*, and a service
    token cannot open anything else."""
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "root", "password": "rootpw1234"})
        assert admin.get("/api/users").status_code == 200             # the session admin API still works
        assert admin.get("/api/admin/users").status_code == 401       # …but not here, cookie and all

    with TestClient(app) as svc:
        assert svc.get("/api/admin/users", headers=AUTH).status_code == 200
        assert svc.get("/api/users", headers=AUTH).status_code == 401   # not a session token
        assert svc.get("/api/works", headers=AUTH).status_code == 401


def test_rate_limit_has_its_own_bucket(monkeypatch):
    """§1.3's 'rate-limited', on a counter of its own: a provisioner reads before every create, and
    that must not spend the login budget (nor a login flood the provisioner's)."""
    import app.auth as _a
    monkeypatch.setattr(get_settings(), "service_rate_limit", 3)
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "root", "password": "rootpw1234"})
        codes = [c.get("/api/admin/users", headers=AUTH).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429], codes
        limited = c.get("/api/admin/users", headers=AUTH)
        assert int(limited.headers["Retry-After"]) > 0
        # Nothing was charged to the login throttle, and a human can still sign in.
        assert _a._fail_log == {}
        assert c.post("/api/auth/login",
                      json={"username": "root", "password": "rootpw1234"}).status_code == 200

    # …and the reverse: an exhausted login budget does not close the provisioning surface.
    with service_auth._lock:
        service_auth._hits.clear()
    monkeypatch.setattr(get_settings(), "service_rate_limit", 120)
    with TestClient(app) as c:
        for _ in range(get_settings().login_max_attempts + 2):
            c.post("/api/auth/login", json={"username": "root", "password": "wrong"})
        assert c.post("/api/auth/login",
                      json={"username": "root", "password": "rootpw1234"}).status_code == 429
        assert c.get("/api/admin/users", headers=AUTH).status_code == 200


# ----------------------------------------------------------------------------- create + 409
def test_create_returns_the_user_and_defaults_the_optional_fields():
    with TestClient(app) as c:
        r = c.post("/api/admin/users", headers=AUTH, json=_new_user())
        assert r.status_code == 201, r.text
        body = r.json()
        assert isinstance(body["id"], int) and body["id"] > 0   # an account with no id can't be revoked
        assert body["username"] == "reader" and body["email"] == "reader@example.com"
        assert body["is_active"] is True and body["role"] == "user"
        # Neither field was sent: the cap and the capability set inherit the instance default, and
        # the user's own 18+ opt-in stays unset (nobody opts in on somebody else's behalf).
        assert body["allowed_categories"] is None and body["permissions"] is None
        db = SessionLocal()
        assert db.get(User, body["id"]).adult_categories is None
        db.close()


def test_duplicate_username_and_email_409_with_the_existing_id():
    """The 409 is what makes a create whose response never arrived safe to repeat — so it must carry
    the id as a FIELD. A caller must never have to parse one out of the message."""
    with TestClient(app) as c:
        first = c.post("/api/admin/users", headers=AUTH, json=_new_user()).json()

        same_name = c.post("/api/admin/users", headers=AUTH,
                           json=_new_user(email="other@example.com"))
        assert same_name.status_code == 409
        detail = same_name.json()["detail"]
        assert detail["id"] == first["id"] and detail["error"] == "username_taken"
        assert detail["username"] == "reader" and detail["email"] == "reader@example.com"
        assert detail["is_active"] is True

        same_email = c.post("/api/admin/users", headers=AUTH, json=_new_user(username="other"))
        assert same_email.status_code == 409
        detail = same_email.json()["detail"]
        assert detail["id"] == first["id"] and detail["error"] == "email_in_use"
        assert detail["username"] == "reader"     # the colliding account, so a caller can refuse it

        # Non-duplicate failures are still the plain validation errors they were.
        assert c.post("/api/admin/users", headers=AUTH,
                      json=_new_user(username="bad", email="notanemail")).status_code == 422


# ------------------------------------------------------------------------------------ read
def test_read_back_by_id_and_by_username():
    """The endpoint §1.3 omits, and the one the caller's idempotency rests on."""
    with TestClient(app) as c:
        created = c.post("/api/admin/users", headers=AUTH, json=_new_user()).json()

        one = c.get(f"/api/admin/users/{created['id']}", headers=AUTH)
        assert one.status_code == 200 and one.json()["username"] == "reader"
        assert c.get("/api/admin/users/999999", headers=AUTH).status_code == 404

        found = c.get("/api/admin/users", headers=AUTH, params={"username": "reader"}).json()
        assert [u["id"] for u in found] == [created["id"]]
        assert c.get("/api/admin/users", headers=AUTH,
                     params={"username": "nobody"}).json() == []
        # Unfiltered still lists everyone.
        c.post("/api/admin/users", headers=AUTH, json=_new_user(username="second",
                                                                email="second@example.com"))
        assert len(c.get("/api/admin/users", headers=AUTH).json()) == 2


# ------------------------------------------------------------------------- revoke + delete
def test_patch_is_active_revokes_and_delete_removes():
    with TestClient(app) as c:
        created = c.post("/api/admin/users", headers=AUTH, json=_new_user()).json()

        # The account works before the revoke…
        signed_in = TestClient(app)
        assert signed_in.post("/api/auth/login",
                              json={"username": "reader", "password": "provisionpw1"}).status_code == 200
        assert signed_in.get("/api/works").status_code == 200

        r = c.patch(f"/api/admin/users/{created['id']}", headers=AUTH, json={"is_active": False})
        assert r.status_code == 200 and r.json()["is_active"] is False
        assert signed_in.get("/api/works").status_code == 401       # …and their session died with it
        assert c.get(f"/api/admin/users/{created['id']}",
                     headers=AUTH).json()["is_active"] is False

        assert c.delete(f"/api/admin/users/{created['id']}",
                        headers=AUTH).json() == {"deleted": created["id"]}
        assert c.get(f"/api/admin/users/{created['id']}", headers=AUTH).status_code == 404
        assert c.delete(f"/api/admin/users/{created['id']}", headers=AUTH).status_code == 404


def test_admin_accounts_are_off_limits():
    """A provisioner must not be able to lock the instance's admins out — or touch them at all. The
    last-admin guards still stand behind this, but they are no longer what's doing the work: an admin
    target is refused outright, so deactivate/demote/delete never reach them."""
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "root", "password": "rootpw1234"})
        root = admin.get("/api/auth/me").json()["user"]["id"]
    with TestClient(app) as c:
        assert c.patch(f"/api/admin/users/{root}", headers=AUTH,
                       json={"is_active": False}).status_code == 403
        assert c.patch(f"/api/admin/users/{root}", headers=AUTH,
                       json={"role": "user"}).status_code == 403
        assert c.delete(f"/api/admin/users/{root}", headers=AUTH).status_code == 403
        # ...and the operator's account is untouched by any of it.
        me = c.get(f"/api/admin/users/{root}", headers=AUTH).json()
        assert me["is_active"] is True and me["role"] == "admin"


def test_surface_cannot_escalate_to_instance_admin():
    """A provisioning token grants ACCESS; it must not be able to become — or seize — an operator.
    Both paths are one request: mint an admin and log in as it, or reset the operator's password."""
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "root", "password": "rootpw1234"})
        root = admin.get("/api/auth/me").json()["user"]["id"]
    with TestClient(app) as c:
        assert c.post("/api/admin/users", headers=AUTH, json={
            "username": "evil", "password": "evilpw1234", "role": "admin"}).status_code == 403
        assert c.get("/api/admin/users?username=evil", headers=AUTH).json() == []
        for field, value in (("role", "admin"), ("password", "hijacked999"), ("username", "taken")):
            r = c.patch(f"/api/admin/users/{root}", headers=AUTH, json={field: value})
            assert r.status_code == 403, (field, r.status_code)
        # `email` stays writable for real provisioning, so the OPERATOR's row must be off-limits
        # outright: rewriting an admin's email is a takeover in three requests — PATCH it to one you
        # control, POST /api/auth/forgot-password with their username (it matches on username but
        # mails user.email), then reset the password.
        assert c.patch(f"/api/admin/users/{root}", headers=AUTH,
                       json={"email": "attacker@evil.test"}).status_code == 403
        assert c.delete(f"/api/admin/users/{root}", headers=AUTH).status_code == 403
    with TestClient(app) as admin:
        assert admin.get(f"/api/admin/users/{root}", headers=AUTH).json()["email"] != "attacker@evil.test"
    # The operator's own credential still works — the refusals changed nothing.
    with TestClient(app) as admin:
        assert admin.post("/api/auth/login",
                          json={"username": "root", "password": "rootpw1234"}).status_code == 200


def test_allowed_updates_still_work():
    """The revoke and the profile fields a provisioner legitimately drives are untouched — including
    email-clearing, which update_user reads off model_fields_set (so the filter must rebuild the
    payload, not strip fields off it)."""
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "root", "password": "rootpw1234"})
    with TestClient(app) as c:
        uid = c.post("/api/admin/users", headers=AUTH, json={
            "username": "grantee", "password": "granteepw1", "email": "g@example.com"}).json()["id"]
        assert c.patch(f"/api/admin/users/{uid}", headers=AUTH,
                       json={"display_name": "Grantee", "email": None}).status_code == 200
        out = c.patch(f"/api/admin/users/{uid}", headers=AUTH, json={"is_active": False}).json()
        assert out["is_active"] is False and out["display_name"] == "Grantee"
        assert out["email"] is None


# ------------------------------------------------------------------------------ cloudflare
class _FakeClient:
    """Records every Cloudflare call instead of making one (shape as in test_cloudflare.py)."""
    calls: list = []
    policy = {"id": "pol1", "name": "Shelf", "decision": "allow", "reusable": False, "precedence": 1,
              "include": [], "exclude": [], "require": []}

    def __init__(self, timeout=None): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    class _Resp:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    def get(self, url, headers=None):
        _FakeClient.calls.append(("GET", url))
        return self._Resp({"result": _FakeClient.policy})

    def put(self, url, headers=None, json=None):
        _FakeClient.calls.append(("PUT", url, json))
        return self._Resp({"result": json})


def test_service_path_never_touches_cloudflare(monkeypatch):
    """Shelf's own create/delete rewrite an Access POLICY's include array. A provisioner that owns
    edge grants through Gateway Lists and reconciles them itself must not have a second writer behind
    it minting grants it never decided — so this surface leaves the policy alone."""
    monkeypatch.setattr(cloudflare.httpx, "Client", _FakeClient)
    db = SessionLocal()
    cloudflare.set_config(db, {"account_id": "a", "app_id": "p", "policy_id": "q",
                               "api_token": "fake-token", "enabled": True})
    assert cloudflare.is_configured(cloudflare.get_config(db)) is True   # it WOULD fire if allowed
    db.close()

    with TestClient(app) as c:
        _FakeClient.calls = []
        created = c.post("/api/admin/users", headers=AUTH, json=_new_user()).json()
        assert _FakeClient.calls == [], _FakeClient.calls
        c.patch(f"/api/admin/users/{created['id']}", headers=AUTH, json={"is_active": False})
        assert _FakeClient.calls == [], _FakeClient.calls
        c.delete(f"/api/admin/users/{created['id']}", headers=AUTH)
        assert _FakeClient.calls == [], _FakeClient.calls

    # Control: the SESSION admin path is untouched — it still grants at the edge as it always did,
    # which is what proves the suppression above is scoped and not just a broken fixture.
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "root", "password": "rootpw1234"})
        _FakeClient.calls = []
        admin.post("/api/users", json=_new_user(username="by-hand", email="hand@example.com"))
        assert any(call[0] == "PUT" for call in _FakeClient.calls), _FakeClient.calls
        assert {"email": {"email": "hand@example.com"}} in next(
            c for c in _FakeClient.calls if c[0] == "PUT")[2]["include"]
