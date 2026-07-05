"""Audiobookshelf-compatible API: the browse -> open -> play -> sync flow an ABS companion app (Still)
drives, authenticating with a bearer token (no Shelf cookie)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.main import app
from app.models import LibraryItem, ReadingState, User, UserSession, Work
from app.routers import delivery as d


@pytest.fixture
def setup(tmp_path, monkeypatch):
    init_db()
    db = SessionLocal()
    for m in (LibraryItem, ReadingState, UserSession, Work, User):
        db.execute(delete(m))
    db.commit()
    f = tmp_path / "dune.m4b"
    f.write_bytes(b"x")
    w = Work(title="Dune", author="Frank Herbert", narrator="Scott Brick", media_kind="audio",
             local_path=str(f), cover_url="/covers/dune.jpg")
    db.add(w); db.commit(); db.refresh(w)
    wid = w.id
    db.close()
    # ffprobe is mocked: one 3600s AAC track with two chapters.
    monkeypatch.setattr(d, "_run_ffprobe", lambda p, **kw: {
        "format": {"duration": "3600.0"},
        "streams": [{"codec_type": "audio", "codec_name": "aac"}],
        "chapters": [{"start_time": "0.0", "tags": {"title": "One"}},
                     {"start_time": "1800.0", "tags": {"title": "Two"}}],
    })
    c = TestClient(app)
    c.post("/api/auth/setup", json={"username": "abs", "password": "abspass12"})
    return wid


def test_abs_full_flow(setup):
    wid = setup
    c = TestClient(app)
    assert c.get("/ping").json()["success"] is True                       # unauthenticated bootstrap

    r = c.post("/login", json={"username": "abs", "password": "abspass12"})
    assert r.status_code == 200
    body = r.json()
    token = body["user"]["token"]
    assert token and body["userDefaultLibraryId"] == "shelf-audiobooks"
    assert body["user"]["mediaProgress"] == []

    # A FRESH client with NO cookie — proves the ABS bearer token alone authenticates.
    bare = TestClient(app)
    H = {"Authorization": f"Bearer {token}"}

    libs = bare.get("/api/libraries", headers=H).json()["libraries"]
    assert len(libs) == 1 and libs[0]["mediaType"] == "book"

    items = bare.get(f"/api/libraries/{libs[0]['id']}/items", headers=H).json()
    assert items["total"] == 1
    assert items["results"][0]["media"]["metadata"]["title"] == "Dune"

    it = bare.get(f"/api/items/{wid}", headers=H).json()
    assert it["media"]["metadata"]["authorName"] == "Frank Herbert"
    assert it["media"]["metadata"]["narratorName"] == "Scott Brick"
    assert len(it["media"]["chapters"]) == 2
    tracks = it["media"]["tracks"]
    assert len(tracks) == 1 and "token=" in tracks[0]["contentUrl"]        # streamable with the token

    ps = bare.post(f"/api/items/{wid}/play", headers=H, json={"deviceInfo": {"id": "x"}}).json()
    assert ps["duration"] == 3600.0 and ps["audioTracks"] and ps["playMethod"] == "directPlay"

    pr = bare.patch(f"/api/me/progress/{wid}", headers=H,
                    json={"currentTime": 1800.0, "duration": 3600.0, "progress": 0.5}).json()
    assert abs(pr["currentTime"] - 1800.0) < 1 and abs(pr["progress"] - 0.5) < 0.05

    me = bare.get("/api/me", headers=H).json()
    assert any(p["libraryItemId"] == str(wid) for p in me["mediaProgress"])


def test_abs_login_rejects_bad_password(setup):
    c = TestClient(app)
    assert c.post("/login", json={"username": "abs", "password": "wrong"}).status_code == 401


def test_abs_requires_auth(setup):
    # No token → the authenticated endpoints refuse.
    assert TestClient(app).get("/api/libraries").status_code == 401
