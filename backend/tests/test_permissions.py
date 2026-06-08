"""Granular per-user permissions: resolver, /auth/me, endpoint gating, admin controls."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app import permissions as P
from app.db import SessionLocal, init_db
from app.main import app
from app.models import AppSetting, User, UserSession


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    db.execute(delete(UserSession)); db.execute(delete(User))
    db.execute(delete(AppSetting).where(AppSetting.key == "default_user_permissions"))
    db.commit(); db.close()
    yield


@pytest.fixture
def admin():
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "hunter2pw"})
        yield c


def _login(username: str, password: str = "hunter2pw") -> TestClient:
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": username, "password": password})
    return c


def test_effective_permissions_admin_vs_default_vs_custom():
    db = SessionLocal()
    admin = User(username="a", password_hash="x", role="admin")
    u = User(username="u", password_hash="x", role="user")          # inherits → baseline
    custom = User(username="c", password_hash="x", role="user", permissions=["index.view"])
    db.add_all([admin, u, custom]); db.commit()
    assert set(P.effective_permissions(db, admin)) == set(P.ALL_PERMISSIONS)   # admin → all
    assert set(P.effective_permissions(db, u)) == set(P.DEFAULT_PERMISSIONS)   # baseline
    assert P.effective_permissions(db, custom) == ["index.view"]               # explicit subset
    # Global default overrides the baseline for inheriting users.
    P.set_default_permissions(db, ["index.view", "send.kindle"])
    assert set(P.effective_permissions(db, u)) == {"index.view", "send.kindle"}
    assert P.effective_permissions(db, custom) == ["index.view"]  # explicit still wins
    db.close()


def test_me_reports_permissions(admin):
    me = admin.get("/api/auth/me").json()
    assert set(me["permissions"]) == set(P.ALL_PERMISSIONS)  # admin sees all
    admin.post("/api/users", json={"username": "bob", "password": "hunter2pw", "role": "user",
                                   "permissions": ["index.view", "send.kindle"]})
    bob = _login("bob")
    assert set(bob.get("/api/auth/me").json()["permissions"]) == {"index.view", "send.kindle"}


def test_gates_enforced_per_permission(admin):
    # bob may VIEW the index + send, but NOT hook, acquire, add, or see jobs/sources.
    admin.post("/api/users", json={"username": "bob", "password": "hunter2pw", "role": "user",
                                   "permissions": ["index.view", "send.kindle"]})
    bob = _login("bob")
    assert bob.get("/api/catalog/rows").status_code == 200          # index.view ✓
    assert bob.post("/api/catalog/1/hook", json={}).status_code == 403   # index.hook ✗
    assert bob.post("/api/catalog/1/acquire", json={}).status_code == 403  # index.acquire ✗
    assert bob.post("/api/works/hook", json={"ref": "x"}).status_code == 403  # add.use ✗
    assert bob.get("/api/jobs").status_code == 403                  # jobs.view ✗
    assert bob.get("/api/sources").status_code == 403               # sources.view ✗

    # carol has no index.view → can't even browse the catalog.
    admin.post("/api/users", json={"username": "carol", "password": "hunter2pw", "role": "user",
                                   "permissions": []})
    carol = _login("carol")
    assert carol.get("/api/catalog/rows").status_code == 403

    # An admin passes every gate.
    assert admin.get("/api/jobs").status_code == 200
    assert admin.get("/api/sources").status_code == 200


def test_admin_permission_default_and_meta(admin):
    meta = admin.get("/api/users/permissions-meta").json()
    assert {p["key"] for p in meta["all"]} == set(P.ALL_PERMISSIONS)
    assert set(meta["baseline"]) == set(P.DEFAULT_PERMISSIONS)
    # Set a global default; a new inheriting user gets it.
    admin.put("/api/users/permission-default", json={"permissions": ["index.view"]})
    admin.post("/api/users", json={"username": "dave", "password": "hunter2pw", "role": "user"})
    dave = _login("dave")
    assert dave.get("/api/auth/me").json()["permissions"] == ["index.view"]
    assert dave.get("/api/catalog/rows").status_code == 200
    assert dave.post("/api/works/hook", json={"ref": "x"}).status_code == 403  # no add.use

    # PATCH a per-user override; null resets to the (new) default.
    uid = next(u["id"] for u in admin.get("/api/users").json() if u["username"] == "dave")
    admin.patch(f"/api/users/{uid}", json={"permissions": ["index.view", "add.use"]})
    assert dave.post("/api/works/hook", json={"ref": "x"}).status_code != 403  # add.use granted now
    admin.patch(f"/api/users/{uid}", json={"permissions": None})
    assert dave.get("/api/auth/me").json()["permissions"] == ["index.view"]  # back to default
