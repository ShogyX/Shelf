"""Wave G #15 — user-reported issues (flagging): create, scoped visibility, view_all, admin resolve."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.main import app
from app.models import Issue, User, UserSession, Work


@pytest.fixture
def client():
    init_db()
    db = SessionLocal()
    for m in (Issue, UserSession, Work, User):
        db.execute(delete(m))
    db.commit()
    db.close()
    c = TestClient(app)
    c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
    c.post("/api/users", json={"username": "joe", "password": "test1234", "role": "user"})
    c.post("/api/users", json={"username": "bob", "password": "test1234", "role": "user"})
    yield c


def _login(u):
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": u, "password": "test1234"})
    return c


def _work():
    db = SessionLocal()
    w = Work(title="Flagged Book", media_kind="text")
    db.add(w); db.commit(); wid = w.id; db.close()
    return wid


def _grant_view_all(username):
    db = SessionLocal()
    u = db.scalar(select(User).where(User.username == username))
    u.permissions = ["issues.view_all"]
    db.commit(); db.close()


def test_issue_create_visibility_view_all_and_resolve(client):
    wid = _work()
    joe, bob, admin = _login("joe"), _login("bob"), _login("admin")

    # empty description rejected
    assert joe.post("/api/issues", json={"work_id": wid, "kind": "other", "description": ""}).status_code == 422

    r = joe.post("/api/issues", json={"work_id": wid, "kind": "no_content", "description": "opens blank"})
    assert r.status_code == 200, r.text
    iid = r.json()["id"]
    assert r.json()["title"] == "Flagged Book" and r.json()["mine"] is True and r.json()["status"] == "open"

    # reporter sees their own; a peer without view_all does NOT
    assert any(x["id"] == iid for x in joe.get("/api/issues").json())
    assert all(x["id"] != iid for x in bob.get("/api/issues").json())

    # admin sees it, with the reporter's name, and may resolve
    ai = next(x for x in admin.get("/api/issues").json() if x["id"] == iid)
    assert ai["username"] == "joe" and ai["can_resolve"] is True
    assert admin.get("/api/issues/count").json()["open"] >= 1

    # granting bob issues.view_all lets him see others' (with the reporter name)
    _grant_view_all("bob")
    bi = [x for x in bob.get("/api/issues").json() if x["id"] == iid]
    assert bi and bi[0]["username"] == "joe" and bi[0]["mine"] is False

    # only admins change status
    assert joe.patch(f"/api/issues/{iid}", json={"status": "resolved"}).status_code == 403
    r2 = admin.patch(f"/api/issues/{iid}", json={"status": "resolved", "admin_note": "fixed"})
    assert r2.status_code == 200 and r2.json()["status"] == "resolved" and r2.json()["admin_note"] == "fixed"

    # delete: a non-owner non-admin can't; the reporter can
    assert bob.delete(f"/api/issues/{iid}").status_code == 403
    assert joe.delete(f"/api/issues/{iid}").status_code == 200
    assert all(x["id"] != iid for x in admin.get("/api/issues").json())
