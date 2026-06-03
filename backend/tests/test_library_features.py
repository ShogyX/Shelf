"""New library features: continue-reading removal, bulk download, metadata stats,
rich bookshelf creation, and per-bookshelf Goodreads shelves."""
from __future__ import annotations

import asyncio
import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.integrations import metadata as M
from app.integrations import metadata_sync as MS
from app.main import app
from app.models import (
    AppSetting,
    Bookshelf,
    BookshelfItem,
    CatalogWork,
    Chapter,
    ChapterContent,
    CrawlJob,
    Integration,
    LibraryItem,
    MetadataLink,
    QueuedHook,
    ReadingState,
    Source,
    User,
    UserSession,
    UserSettings,
    Work,
)


@pytest.fixture
def clients():
    init_db()
    db = SessionLocal()
    for m in (LibraryItem, BookshelfItem, Bookshelf, QueuedHook, MetadataLink, CrawlJob,
              CatalogWork, ReadingState, ChapterContent, Chapter, Integration, Work,
              UserSession, UserSettings, User):
        db.execute(delete(m))
    db.execute(delete(AppSetting).where(AppSetting.key == "library_membership_seed_v1"))
    db.commit()
    db.close()
    admin = TestClient(app)
    admin.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
    admin.post("/api/users", json={"username": "bob", "password": "test1234", "role": "user"})
    bob = TestClient(app)
    bob.post("/api/auth/login", json={"username": "bob", "password": "test1234"})
    yield admin, bob


def _uid(name: str) -> int:
    db = SessionLocal()
    uid = db.scalar(select(User.id).where(User.username == name))
    db.close()
    return uid


def _work_with_chapter(title: str, member_uid: int | None) -> int:
    db = SessionLocal()
    w = Work(title=title, status="complete")
    db.add(w)
    db.commit()
    db.refresh(w)
    ch = Chapter(work_id=w.id, index=1, title="Ch 1", fetch_status="fetched")
    db.add(ch)
    db.commit()
    db.refresh(ch)
    c = ChapterContent(chapter_id=ch.id, format="html", body="<p>hello world</p>",
                       word_count=2, checksum=f"x{w.id}")
    db.add(c)
    db.commit()
    db.refresh(c)
    ch.content_id = c.id
    if member_uid is not None:
        db.add(LibraryItem(user_id=member_uid, work_id=w.id))
    db.commit()
    wid = w.id
    db.close()
    return wid


# ----------------------------------------------------------- continue-reading removal
def test_clear_progress_removes_from_continue_reading(clients):
    admin, bob = clients
    wid = _work_with_chapter("Reading", _uid("bob"))
    db = SessionLocal()
    cid = db.scalar(select(Chapter.id).where(Chapter.work_id == wid))
    db.close()
    bob.post(f"/api/works/{wid}/progress",
             json={"last_chapter_id": cid, "scroll_fraction": 0.3, "paragraph_index": 0})
    assert [c["work_id"] for c in bob.get("/api/continue-reading").json()] == [wid]
    # Remove it.
    assert bob.request("DELETE", f"/api/works/{wid}/progress").json()["cleared"] == wid
    assert bob.get("/api/continue-reading").json() == []
    # Work itself stays in the library.
    assert wid in [w["id"] for w in bob.get("/api/works").json()]


# ----------------------------------------------------------- bulk download
def test_bulk_download_zips_selected_and_shelf(clients):
    admin, bob = clients
    w1 = _work_with_chapter("One", _uid("bob"))
    w2 = _work_with_chapter("Two", _uid("bob"))
    other = _work_with_chapter("NotBobs", None)  # bob is not a member → excluded

    r = bob.post("/api/library/download", json={"work_ids": [w1, w2, other]})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert len(zf.namelist()) == 2  # only bob's two works

    # Empty selection is rejected.
    assert bob.post("/api/library/download", json={"work_ids": []}).status_code == 400


# ----------------------------------------------------------- metadata stats
def test_metadata_stats(clients):
    admin, bob = clients
    db = SessionLocal()
    a = Work(title="A", hooked=True)
    b = Work(title="B", hooked=True)
    c = Work(title="C", hooked=True)
    db.add_all([a, b, c])
    db.commit()
    db.refresh(a)
    db.refresh(b)
    db.add(MetadataLink(work_id=a.id, provider="ranobedb", ref="1", confidence=0.95))
    db.add(MetadataLink(work_id=b.id, provider="ranobedb", ref="2", confidence=0.65))
    db.commit()
    db.close()

    stats = admin.get("/api/metadata-stats").json()
    assert stats["total_library_works"] == 3
    rano = next(p for p in stats["providers"] if p["provider"] == "ranobedb")
    assert rano["matched"] == 2 and rano["unmatched"] == 1
    assert rano["high_confidence"] == 1 and rano["medium_confidence"] == 1
    # Non-admins can't see operator metadata stats.
    assert bob.get("/api/metadata-stats").status_code == 403


# ----------------------------------------------------------- rich bookshelf create
def test_create_bookshelf_with_config_and_works(clients):
    admin, bob = clients
    wid = _work_with_chapter("Shelved", _uid("bob"))
    r = bob.post("/api/bookshelves", json={
        "name": "Reading", "auto_update": True, "notify_on_add": True,
        "goodreads_shelf": "currently-reading", "work_ids": [wid],
    })
    assert r.status_code == 200
    out = r.json()
    assert out["auto_update"] and out["notify_on_add"]
    assert out["goodreads_shelf"] == "currently-reading" and out["count"] == 1


# ----------------------------------------------------------- per-bookshelf goodreads shelf
def test_goodreads_pulls_per_bookshelf_shelf(clients, monkeypatch):
    admin, bob = clients
    bob_id = _uid("bob")
    # Bob marks one shelf as the default target and another as a named external shelf.
    sid = bob.post("/api/bookshelves",
                   json={"name": "CR", "goodreads_shelf": "currently-reading"}).json()["id"]

    calls: list[str] = []

    class _GR:
        def __init__(self, shelf):
            self.shelf = shelf
        async def wanted(self):
            calls.append(self.shelf)
            return [M.ProviderMatch(ref="1", title=f"Book {self.shelf}", author="A")]

    def _fake_provider(integ, config=None):
        return _GR((config or {}).get("shelf") or (integ.config or {}).get("shelf") or "to-read")
    monkeypatch.setattr(M, "provider_for", _fake_provider)

    db = SessionLocal()
    integ = Integration(kind="goodreads", name="GR", base_url="123", api_key="",
                        user_id=bob_id, config={"shelf": "to-read"})
    db.add(integ)
    db.commit()
    res = asyncio.run(MS.import_goodreads(db, integ))
    db.close()

    # Both the default shelf and the bookshelf's named shelf were pulled.
    assert set(calls) == {"to-read", "currently-reading"}
    assert res["queued"] == 2
    db = SessionLocal()
    # The currently-reading book landed on the bookshelf that named that external shelf.
    assert db.scalar(select(QueuedHook).where(QueuedHook.target_shelf_id == sid)) is not None
    db.close()
