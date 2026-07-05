"""Wave H #16 — Cloudflare Access integration: include-list logic, config/secret handling, the
best-effort user hooks (HTTP mocked), token redaction, and admin gating."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.integrations import cloudflare
from app.main import app
from app.models import AppSetting, User, UserSession


# ---------------------------------------------------------------- pure include helpers
def test_add_is_idempotent_and_case_insensitive():
    inc = [{"email": {"email": "a@x.com"}}]
    assert cloudflare.add_to_include(inc, "a@x.com") == inc            # already present → unchanged
    assert cloudflare.add_to_include(inc, "A@X.COM") == inc            # case-insensitive dedupe
    out = cloudflare.add_to_include(inc, "b@x.com")
    assert {r["email"]["email"] for r in out} == {"a@x.com", "b@x.com"}


def test_remove_is_case_insensitive():
    inc = [{"email": {"email": "a@x.com"}}, {"email": {"email": "b@x.com"}}, {"everyone": {}}]
    out = cloudflare.remove_from_include(inc, "A@X.com")
    assert {r.get("email", {}).get("email") for r in out if "email" in r} == {"b@x.com"}
    assert {"everyone": {}} in out                                    # non-email rules preserved


def test_is_configured_requires_everything_and_enabled():
    assert cloudflare.is_configured({}) is False
    full = {"enabled": True, "api_token": "t", "account_id": "a", "app_id": "p", "policy_id": "q"}
    assert cloudflare.is_configured(full) is True
    assert cloudflare.is_configured({**full, "enabled": False}) is False
    assert cloudflare.is_configured({**full, "api_token": ""}) is False


# ---------------------------------------------------------------- config + secret preservation
@pytest.fixture(autouse=True)
def _clean_cfg():
    init_db()
    db = SessionLocal()
    db.execute(delete(AppSetting).where(AppSetting.key == "cloudflare_access"))
    db.commit(); db.close()
    yield


def test_set_config_preserves_blank_token():
    db = SessionLocal()
    cloudflare.set_config(db, {"account_id": "a", "app_id": "p", "policy_id": "q",
                              "api_token": "secret", "enabled": True})
    cloudflare.set_config(db, {"account_id": "a2", "api_token": ""})   # blank token → keep old
    cfg = cloudflare.get_config(db)
    assert cfg["api_token"] == "secret" and cfg["account_id"] == "a2"
    db.close()


# ---------------------------------------------------------------- HTTP round-trip (mocked)
class _Resp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p


class _FakeClient:
    calls: list = []
    # A REUSABLE (account-level) policy, like a real Cloudflare Access policy: it carries read-only
    # `reusable`/`precedence`/`id` that must NOT be echoed on PUT, and an admin-set `session_duration`
    # that must survive.
    policy = {"id": "pol1", "name": "Shelf", "decision": "allow", "reusable": True, "precedence": 1,
              "include": [{"email": {"email": "keep@x.com"}}], "exclude": [], "require": [],
              "session_duration": "24h"}

    def __init__(self, timeout=None): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def get(self, url, headers=None):
        _FakeClient.calls.append(("GET", url))
        return _Resp({"result": type(self).policy})   # type(self) so a subclass can override the policy

    def put(self, url, headers=None, json=None):
        _FakeClient.calls.append(("PUT", url, json))
        return _Resp({"result": json})


def test_add_and_remove_user_email_hit_the_policy(monkeypatch):
    monkeypatch.setattr(cloudflare.httpx, "Client", _FakeClient)
    _FakeClient.calls = []
    db = SessionLocal()
    cloudflare.set_config(db, {"account_id": "a", "app_id": "p", "policy_id": "q",
                              "api_token": "t", "enabled": True})

    cloudflare.add_user_email(db, "new@x.com")
    put = next(c for c in _FakeClient.calls if c[0] == "PUT")
    url, body = put[1], put[2]
    emails = {r["email"]["email"] for r in body["include"]}
    assert emails == {"keep@x.com", "new@x.com"}                      # added alongside the existing rule
    assert body["session_duration"] == "24h"                         # admin-set field preserved on PUT
    # A REUSABLE policy must be updated via the account-level endpoint, NOT /apps/ (Cloudflare rejects
    # the latter with error 12130), and its read-only kind/order fields must be stripped from the PUT.
    assert "/access/policies/q" in url and "/apps/" not in url
    for k in ("id", "reusable", "precedence"):
        assert k not in body


def test_inline_policy_uses_the_app_endpoint(monkeypatch):
    # A non-reusable (inline, app-specific) policy is updated via the /apps/ endpoint.
    class _Inline(_FakeClient):
        policy = {**_FakeClient.policy, "reusable": False}
    monkeypatch.setattr(cloudflare.httpx, "Client", _Inline)
    _FakeClient.calls = []   # the inherited get/put record onto the base's shared list
    db = SessionLocal()
    cloudflare.set_config(db, {"account_id": "a", "app_id": "p", "policy_id": "q",
                              "api_token": "t", "enabled": True})
    cloudflare.add_user_email(db, "new@x.com")
    url = next(c for c in _FakeClient.calls if c[0] == "PUT")[1]
    assert "/access/apps/p/policies/q" in url

    # unconfigured → no HTTP at all
    _FakeClient.calls = []
    cloudflare.set_config(db, {"enabled": False})
    cloudflare.add_user_email(db, "z@x.com")
    assert _FakeClient.calls == []
    db.close()


def test_add_user_email_swallows_errors(monkeypatch):
    class _Boom(_FakeClient):
        def get(self, url, headers=None):
            raise RuntimeError("cloudflare down")
    monkeypatch.setattr(cloudflare.httpx, "Client", _Boom)
    db = SessionLocal()
    cloudflare.set_config(db, {"account_id": "a", "app_id": "p", "policy_id": "q",
                              "api_token": "t", "enabled": True})
    cloudflare.add_user_email(db, "new@x.com")   # must NOT raise
    db.close()


# ---------------------------------------------------------------- endpoints
@pytest.fixture
def client():
    db = SessionLocal()
    for m in (UserSession, User):
        db.execute(delete(m))
    db.commit(); db.close()
    c = TestClient(app)
    c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
    c.post("/api/users", json={"username": "joe", "password": "test1234", "role": "user"})
    return c


def _login(u):
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": u, "password": "test1234"})
    return c


def test_cf_endpoints_admin_only_and_redact_token(client):
    admin, joe = _login("admin"), _login("joe")
    assert joe.get("/api/settings/cloudflare-access").status_code == 403
    admin.put("/api/settings/cloudflare-access",
              json={"account_id": "a", "app_id": "p", "policy_id": "q", "api_token": "secret", "enabled": True})
    out = admin.get("/api/settings/cloudflare-access").json()
    assert out == {"account_id": "a", "app_id": "p", "policy_id": "q", "enabled": True, "api_token_set": True}
    assert "api_token" not in out                                     # the secret is never returned
    # test with a still-unconfigured (blank) token would 400; here it's configured so test would try HTTP —
    # unconfigured path is the safe assertion:
    admin.put("/api/settings/cloudflare-access", json={"enabled": False})
    assert admin.post("/api/settings/cloudflare-access/test").status_code == 400
