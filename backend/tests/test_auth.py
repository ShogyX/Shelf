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
    """Each auth test starts from a fresh (no-users) instance."""
    init_db()
    db = SessionLocal()
    for model in (UserSession, ReadingState, UserSettings, User):
        db.execute(delete(model))
    db.commit()
    db.close()
    yield


def test_auth_flow_and_gating():
    with TestClient(app) as c:
        # Unauthenticated API access is rejected.
        assert c.get("/api/works").status_code == 401

        # Fresh instance reports it needs setup.
        me = c.get("/api/auth/me").json()
        assert me["needs_setup"] is True and me["authenticated"] is False

        # First admin via setup → logs us in (cookie set on the client).
        r = c.post("/api/auth/setup", json={"username": "alice", "password": "hunter2"})
        assert r.status_code == 200 and r.json()["role"] == "admin"

        me = c.get("/api/auth/me").json()
        assert me["authenticated"] is True and me["user"]["username"] == "alice"
        assert me["needs_setup"] is False

        # Now authenticated requests succeed.
        assert c.get("/api/works").status_code == 200

        # Setup can't be run twice.
        assert c.post("/api/auth/setup", json={"username": "x", "password": "yyyy"}).status_code == 409

        # logout clears the session.
        assert c.post("/api/auth/logout").status_code == 200
        assert c.get("/api/works").status_code == 401


def test_admin_user_management_and_per_user_settings():
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "root", "password": "rootpw"})
        # admin creates a normal user
        r = admin.post("/api/users", json={"username": "reader", "password": "readerpw", "role": "user"})
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
            assert reader.get("/api/users").status_code == 403

        # admin's theme is unchanged by the reader's edit
        assert admin.get("/api/settings").json()["theme"] == "gruvbox-dark"

        # cannot delete the last admin / self
        me_id = admin.get("/api/auth/me").json()["user"]["id"]
        assert admin.delete(f"/api/users/{me_id}").status_code == 400


def test_bad_login_rejected():
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "adminpw"})
        c.post("/api/auth/logout")
        assert c.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "ghost", "password": "x"}).status_code == 401
