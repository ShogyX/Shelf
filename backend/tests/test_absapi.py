"""Audiobookshelf-compatible API: the browse -> open -> play -> sync flow an ABS companion app (Still)
drives, authenticating with a bearer token (no Shelf cookie)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.main import app
from app.models import (
    Bookshelf, BookshelfItem, CatalogGroup, LibraryItem, ReadingState, User, UserSession, Work,
)
from app.routers import delivery as d


@pytest.fixture
def setup(tmp_path, monkeypatch):
    init_db()
    db = SessionLocal()
    for m in (BookshelfItem, Bookshelf, CatalogGroup, LibraryItem, ReadingState, UserSession, Work, User):
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
    # media.audioFiles / numAudioFiles must be populated or an ABS client thinks there's no audio
    # to play and never even POSTs /play.
    assert it["media"]["numAudioFiles"] == 1 and len(it["media"]["audioFiles"]) == 1

    ps = bare.post(f"/api/items/{wid}/play", headers=H, json={"deviceInfo": {"id": "x"}}).json()
    # playMethod is the ABS integer enum (0 = DirectPlay), NOT a string; currentTime/startTime present
    # (absent → the player seeks to NaN and buffers forever).
    assert ps["duration"] == 3600.0 and ps["audioTracks"] and ps["playMethod"] == 0
    assert ps["currentTime"] == 0.0 and "startTime" in ps

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


def test_abs_library_facets(setup):
    """Authors/Narrators/Genres/Stats/Bookmarks sections all return JSON (were 404 → spinners)."""
    token = TestClient(app).post("/login", json={"username": "abs", "password": "abspass12"}).json()["user"]["token"]
    H = {"Authorization": f"Bearer {token}"}
    bare = TestClient(app)
    auth = bare.get("/api/libraries/shelf-audiobooks/authors", headers=H).json()
    assert any(a["name"] == "Frank Herbert" for a in auth["authors"])
    narr = bare.get("/api/libraries/shelf-audiobooks/narrators", headers=H).json()
    assert any(n["name"] == "Scott Brick" for n in narr["narrators"])
    assert bare.get("/api/libraries/shelf-audiobooks/genres", headers=H).json() == {"genres": []}
    stats = bare.get("/api/libraries/shelf-audiobooks/stats", headers=H).json()
    assert stats["totalItems"] == 1 and stats["totalAuthors"] == 1
    assert bare.get("/api/me/bookmarks", headers=H).json() == {"bookmarks": []}


def test_abs_search_author_series_filter(setup):
    """Search, author/series detail, and the items filter= param (tapping an author shows only theirs)."""
    db = SessionLocal()
    db.add_all([
        Work(title="Dune Messiah", author="Frank Herbert", media_kind="text", local_path="/x/dm.epub", series="Dune", series_position=2),
        Work(title="Foundation", author="Isaac Asimov", media_kind="text", local_path="/x/f.epub"),
    ]); db.commit(); db.close()
    token = TestClient(app).post("/login", json={"username": "abs", "password": "abspass12"}).json()["user"]["token"]
    H = {"Authorization": f"Bearer {token}"}
    bare = TestClient(app)

    res = bare.get("/api/search?q=herbert", headers=H).json()   # matches the book (by author) + the author
    assert any(b["libraryItem"]["media"]["metadata"]["title"] == "Dune Messiah" for b in res["book"])
    assert any(a["name"] == "Frank Herbert" for a in res["authors"])

    ad = bare.get("/api/authors/aut_Frank%20Herbert?include=items", headers=H).json()
    assert ad["name"] == "Frank Herbert" and any(i["media"]["metadata"]["title"] == "Dune Messiah" for i in ad["libraryItems"])

    import base64
    fid = base64.b64encode(b"aut_Frank%20Herbert").decode()
    got = bare.get(f"/api/libraries/shelf-books/items?filter=authors.{fid}", headers=H).json()
    titles = {r["media"]["metadata"]["title"] for r in got["results"]}
    assert "Dune Messiah" in titles and "Foundation" not in titles

    ser = bare.get("/api/libraries/shelf-books/series", headers=H).json()
    assert any(s["name"] == "Dune" for s in ser["results"])
    sd = bare.get("/api/series/ser_Dune", headers=H).json()
    assert sd["name"] == "Dune" and len(sd["books"]) == 1

    assert bare.get("/api/me/listening-stats", headers=H).status_code == 200
    assert bare.get("/api/tags", headers=H).status_code == 200


def test_abs_offline_sync_progress_and_logout(setup):
    """Offline-progress sync, batch-get, progress reset, and logout (token revocation)."""
    wid = setup
    token = TestClient(app).post("/login", json={"username": "abs", "password": "abspass12"}).json()["user"]["token"]
    H = {"Authorization": f"Bearer {token}"}
    bare = TestClient(app)

    # newer offline progress is applied to the server
    r = bare.post("/api/me/sync-local-progress", headers=H, json={"localMediaProgresses": [
        {"libraryItemId": str(wid), "currentTime": 900.0, "duration": 3600.0, "isFinished": False,
         "lastUpdate": 9999999999999}]}).json()
    assert r["numServerProgressUpdates"] == 1
    assert abs(bare.get(f"/api/me/progress/{wid}", headers=H).json()["currentTime"] - 900.0) < 1

    assert bare.post("/api/session/local", headers=H, json={"libraryItemId": str(wid), "currentTime": 1000.0}).status_code == 200
    assert len(bare.post("/api/items/batch/get", headers=H, json={"libraryItemIds": [str(wid)]}).json()["libraryItems"]) == 1
    assert bare.get("/api/genres", headers=H).json() == {"genres": []}
    assert bare.get("/api/authors/aut_x/image", headers=H).status_code == 404
    assert bare.post("/api/playlists", headers=H, json={"name": "P"}).json()["name"] == "P"

    # reset progress → then no progress
    assert bare.delete(f"/api/me/progress/{wid}", headers=H).json()["success"] is True
    assert bare.get(f"/api/me/progress/{wid}", headers=H).status_code == 404

    # logout revokes the token
    assert bare.post("/logout", headers=H).status_code == 200
    assert bare.get("/api/me", headers=H).status_code == 401


def test_abs_access_control_per_user(setup):
    """A non-admin's ABS browse is library-isolated, mirroring the web app: books/comics are limited
    to the caller's OWN library membership; audiobooks are the shared global pool; a non-owned item
    (or its file) is not reachable by id. Guards against a user browsing more than they can access."""
    from app.auth import hash_password
    from app.library import add_to_library

    db = SessionLocal()
    u = User(username="reader", password_hash=hash_password("readerpass12"), role="user")
    db.add(u); db.commit(); db.refresh(u)
    mine = Work(title="Zephyr Mine", author="A", media_kind="text", local_path="/x/mine.epub")
    theirs = Work(title="Zephyr Theirs", author="B", media_kind="text", local_path="/x/theirs.epub")
    db.add_all([mine, theirs]); db.commit(); db.refresh(mine); db.refresh(theirs)
    add_to_library(db, u.id, mine.id)          # only "Zephyr Mine" is in reader's library
    mine_id, theirs_id = mine.id, theirs.id
    db.close()

    c = TestClient(app)
    token = c.post("/login", json={"username": "reader", "password": "readerpass12"}).json()["user"]["token"]
    H = {"Authorization": f"Bearer {token}"}

    # Books: ONLY the reader's own book (not the other user's book)
    books = c.get("/api/libraries/shelf-books/items", headers=H).json()
    assert books["total"] == 1 and books["results"][0]["media"]["metadata"]["title"] == "Zephyr Mine"
    # Audiobooks: the shared global pool — Dune (from setup), which the reader never "added"
    audio = c.get("/api/libraries/shelf-audiobooks/items", headers=H).json()
    assert audio["total"] == 1 and audio["results"][0]["media"]["metadata"]["title"] == "Dune"
    # A non-owned book is not reachable by id, and its file can't be downloaded (404, not 403)
    assert c.get(f"/api/items/{theirs_id}", headers=H).status_code == 404
    assert c.get(f"/api/items/{theirs_id}/ebook", headers=H).status_code == 404
    assert c.get(f"/api/items/{mine_id}", headers=H).status_code == 200      # the owned one is fine
    # Search (both titles share "Zephyr") must return the owned one and NOT leak the other user's
    titles = [b["libraryItem"]["media"]["metadata"]["title"]
              for b in c.get("/api/search?q=Zephyr", headers=H).json()["book"]]
    assert titles == ["Zephyr Mine"]


def test_abs_access_control_permissions_and_stock(setup):
    """Books/comics are the GLOBAL in-stock pool filtered by the user's inherited permissions: an
    in-stock title in a permitted category is visible WITHOUT adding it, while a category the user
    can't view (comics) and 18+ content they didn't opt into are hidden — server-side."""
    from app.auth import hash_password

    db = SessionLocal()
    # rdr2 may view Book + Novel but NOT "Manga & Comics"; 18+ turned OFF (explicit empty list)
    u = User(username="rdr2", password_hash=hash_password("rdr2pass12"), role="user",
             allowed_categories=["Book", "Novel"], adult_categories=[])
    db.add(u); db.commit()

    def mk(title, kind, path):
        w = Work(title=title, media_kind=kind, local_path=path)
        db.add(w); db.commit(); db.refresh(w)
        return w.id
    book_ok, comic_blk, adult_bk = (mk("Stock Book", "text", "/x/sb.epub"),
                                    mk("Stock Comic", "comic", "/x/sc.cbz"),
                                    mk("Adult Book", "text", "/x/ab.epub"))
    db.add_all([  # in-stock catalog groups hooked to each work
        CatalogGroup(norm_key="stock book", title="Stock Book", media_label="Book",
                     is_adult=False, hooked_work_id=book_ok),
        CatalogGroup(norm_key="stock comic", title="Stock Comic", media_label="Comic",
                     is_adult=False, hooked_work_id=comic_blk),
        CatalogGroup(norm_key="adult book", title="Adult Book", media_label="Book",
                     is_adult=True, hooked_work_id=adult_bk),
    ]); db.commit(); db.close()

    c = TestClient(app)
    tok = c.post("/login", json={"username": "rdr2", "password": "rdr2pass12"}).json()["user"]["token"]
    H = {"Authorization": f"Bearer {tok}"}

    titles = {r["media"]["metadata"]["title"]
              for r in c.get("/api/libraries/shelf-books/items", headers=H).json()["results"]}
    assert "Stock Book" in titles            # in-stock + permitted category → visible without adding
    assert "Adult Book" not in titles        # 18+ opted out → hidden
    # "Manga & Comics" category not permitted → the comics library is empty and the item is unreachable
    assert c.get("/api/libraries/shelf-comics/items", headers=H).json()["total"] == 0
    assert c.get(f"/api/items/{comic_blk}", headers=H).status_code == 404
    assert c.get(f"/api/items/{adult_bk}", headers=H).status_code == 404       # 18+ book unreachable too
    assert c.get(f"/api/items/{book_ok}", headers=H).status_code == 200        # permitted one is fine
