"""Audiobookshelf-compatible API: the browse -> open -> play -> sync flow an ABS companion app (Still)
drives, authenticating with a bearer token (no Shelf cookie)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.main import app
from app.models import Bookshelf, BookshelfItem, LibraryItem, ReadingState, User, UserSession, Work
from app.routers import delivery as d


@pytest.fixture
def setup(tmp_path, monkeypatch):
    init_db()
    db = SessionLocal()
    for m in (BookshelfItem, Bookshelf, LibraryItem, ReadingState, UserSession, Work, User):
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


def test_abs_login_rejects_pending_account(setup):
    """A self-registered account still awaiting approval can't log in via ABS (parity with web)."""
    from app.auth import hash_password
    from app.models import User
    db = SessionLocal()
    db.add(User(username="pend", password_hash=hash_password("pendpass12"), role="user",
                approval_status="pending", is_active=True))
    db.commit(); db.close()
    assert TestClient(app).post("/login", json={"username": "pend", "password": "pendpass12"}).status_code == 403


def test_query_token_scoped_to_media_paths(setup):
    """A ?token= is honoured only on media routes (audio/cover), not general API endpoints — so the
    session token doesn't leak into the URLs of ordinary calls."""
    wid = setup
    token = TestClient(app).post("/login", json={"username": "abs", "password": "abspass12"}).json()["user"]["token"]
    bare = TestClient(app)   # no cookie
    assert bare.get(f"/api/libraries?token={token}").status_code == 401           # general path: ignored
    assert bare.get(f"/api/works/{wid}/audio/manifest?token={token}").status_code == 200  # media path: honoured


def test_abs_status_is_unauthenticated_json(setup):
    """The pre-login /status probe must return JSON identifying an ABS server (not the SPA sign-in
    HTML, and without auth) — else the Still app rejects the server URL."""
    r = TestClient(app).get("/status")   # no cookie / token
    assert r.status_code == 200
    body = r.json()
    assert body["app"] == "audiobookshelf" and body["isInit"] is True and "local" in body["authMethods"]


def test_abs_multi_library_and_bootstrap_endpoints(setup):
    """Both Audiobooks and Books libraries surface, and the endpoints that were 404-ing (and left the
    app stuck loading) now return valid data: personalized, items-in-progress, authorize POST,
    filterdata, and a clean 404 for socket.io (not the SPA HTML)."""
    db = SessionLocal()
    db.add(Work(title="Way of Kings", author="Brandon Sanderson", media_kind="text",
                local_path="/x/wok.epub"))
    db.commit(); db.close()
    token = TestClient(app).post("/login", json={"username": "abs", "password": "abspass12"}).json()["user"]["token"]
    H = {"Authorization": f"Bearer {token}"}
    bare = TestClient(app)

    libs = {lib["id"] for lib in bare.get("/api/libraries", headers=H).json()["libraries"]}
    assert {"shelf-audiobooks", "shelf-books"} <= libs and "shelf-comics" not in libs  # comics empty

    books = bare.get("/api/libraries/shelf-books/items", headers=H).json()
    assert books["total"] == 1 and books["results"][0]["media"]["metadata"]["title"] == "Way of Kings"

    assert bare.get("/api/libraries/shelf-audiobooks/personalized", headers=H).status_code == 200
    assert bare.get("/api/me/items-in-progress", headers=H).status_code == 200
    assert bare.post("/api/authorize", headers=H).status_code == 200
    assert bare.get("/api/libraries/shelf-books/filterdata", headers=H).status_code == 200
    assert bare.get("/socket.io/?EIO=4&transport=polling", headers=H).status_code == 404


def test_abs_collections_map_to_bookshelves(setup):
    """Creating/editing an ABS collection creates/edits a Shelf Bookshelf (tracked in the web UI)."""
    db = SessionLocal()
    w = Work(title="Mistborn", author="Brandon Sanderson", media_kind="text", local_path="/x/m.epub")
    db.add(w); db.commit(); wid = w.id; db.close()
    token = TestClient(app).post("/login", json={"username": "abs", "password": "abspass12"}).json()["user"]["token"]
    H = {"Authorization": f"Bearer {token}"}
    bare = TestClient(app)

    col = bare.post("/api/collections", headers=H, json={"name": "Fav", "books": [str(wid)]}).json()
    assert col["name"] == "Fav" and len(col["books"]) == 1
    # It's a real Shelf Bookshelf now (so it shows in Shelf's own UI).
    db = SessionLocal()
    shelf = db.scalar(select(Bookshelf).where(Bookshelf.name == "Fav"))
    assert shelf is not None
    assert db.scalar(select(BookshelfItem.id).where(BookshelfItem.shelf_id == shelf.id)) is not None
    db.close()
    # Listed, renamed, book removed, deleted — all via the ABS collection endpoints.
    assert any(c["name"] == "Fav" for c in bare.get("/api/collections", headers=H).json()["collections"])
    assert bare.patch(col["id"].join(["/api/collections/", ""]) if False else f"/api/collections/{col['id']}",
                      headers=H, json={"name": "Favourites"}).json()["name"] == "Favourites"
    bare.delete(f"/api/collections/{col['id']}/book/{wid}", headers=H)
    assert bare.get(f"/api/collections/{col['id']}", headers=H).json()["books"] == []
    assert bare.delete(f"/api/collections/{col['id']}", headers=H).status_code == 200


def test_abs_session_sync_persists_progress(setup):
    """Progress synced from the app's playback session is written to Shelf's ReadingState."""
    wid = setup
    token = TestClient(app).post("/login", json={"username": "abs", "password": "abspass12"}).json()["user"]["token"]
    H = {"Authorization": f"Bearer {token}"}
    bare = TestClient(app)
    sid = bare.post(f"/api/items/{wid}/play", headers=H, json={"deviceInfo": {"id": "x"}}).json()["id"]
    assert bare.post(f"/api/session/{sid}/sync", headers=H, json={"currentTime": 1200.0}).status_code == 200
    assert abs(bare.get(f"/api/me/progress/{wid}", headers=H).json()["currentTime"] - 1200.0) < 1
    assert bare.post(f"/api/session/{sid}/close", headers=H, json={"currentTime": 1300.0}).status_code == 200


def test_abs_ebook_and_download(setup, tmp_path):
    """Ebook reader + item/file download serve the Work's actual on-disk file, with the real format."""
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"PK\x03\x04fake-epub")
    db = SessionLocal()
    w = Work(title="Downloadable", author="A", media_kind="text", local_path=str(epub))
    db.add(w); db.commit(); wid = w.id; db.close()
    token = TestClient(app).post("/login", json={"username": "abs", "password": "abspass12"}).json()["user"]["token"]
    H = {"Authorization": f"Bearer {token}"}
    bare = TestClient(app)

    it = bare.get(f"/api/items/{wid}", headers=H).json()
    assert it["media"]["ebookFile"]["ebookFormat"] == "epub"        # real format, not hardcoded

    r = bare.get(f"/api/items/{wid}/ebook", headers=H)              # reader (inline)
    assert r.status_code == 200 and r.content == b"PK\x03\x04fake-epub" and "epub" in r.headers.get("content-type", "")

    d = bare.get(f"/api/items/{wid}/download", headers=H)           # download (attachment)
    assert d.status_code == 200 and "attachment" in d.headers.get("content-disposition", "")

    assert bare.get(f"/api/items/{wid}/ebook?token={token}").status_code == 200   # URL token honoured here too

    db = SessionLocal()
    w2 = Work(title="Gone", media_kind="text", local_path="/nope/x.epub")
    db.add(w2); db.commit(); w2id = w2.id; db.close()
    assert bare.get(f"/api/items/{w2id}/ebook", headers=H).status_code == 404     # missing file → 404
