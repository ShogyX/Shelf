"""Per-user library isolation + RBAC: each user's library is their own; a work already hooked by
someone else is surfaced (membership only, no new crawl jobs); non-admins can't reach admin pages."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.db import SessionLocal, init_db
from app.main import app
from app.models import (
    AppSetting,
    Bookshelf,
    BookshelfItem,
    CatalogWork,
    Chapter,
    ChapterContent,
    CrawlJob,
    LibraryItem,
    ReadingState,
    User,
    UserSession,
    UserSettings,
    Work,
)


@pytest.fixture
def clients():
    init_db()
    db = SessionLocal()
    for m in (LibraryItem, BookshelfItem, Bookshelf, CrawlJob, CatalogWork, ReadingState,
              ChapterContent, Chapter, Work, UserSession, UserSettings, User):
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


def _uid(username: str) -> int:
    db = SessionLocal()
    uid = db.scalar(select(User.id).where(User.username == username))
    db.close()
    return uid


def test_library_is_per_user_and_isolated(clients):
    admin, bob = clients
    # A shared work that only admin has in their library.
    db = SessionLocal()
    w = Work(title="Admin's Book")
    db.add(w)
    db.commit()
    db.refresh(w)
    db.add(LibraryItem(user_id=_uid("admin"), work_id=w.id))
    db.commit()
    wid = w.id
    db.close()

    assert [x["id"] for x in admin.get("/api/works").json()] == [wid]   # admin sees it
    assert bob.get("/api/works").json() == []                          # bob's library is empty
    assert bob.get(f"/api/works/{wid}").status_code == 404             # and can't read it
    # Admin can inspect bob's (empty) library; bob can't inspect admin's.
    assert admin.get(f"/api/works?user_id={_uid('bob')}").json() == []
    assert bob.get(f"/api/works?user_id={_uid('admin')}").status_code == 403


def test_already_hooked_surfaces_without_new_jobs(clients):
    admin, bob = clients
    db = SessionLocal()
    w = Work(title="Shared Title", source_work_ref="x")
    db.add(w)
    db.commit()
    db.refresh(w)
    # A catalog entry already hooked to the shared work (as if someone hooked it before).
    cw = CatalogWork(provider="web_index", domain="x.com", work_url="https://x.com/n/1",
                     title="Shared Title", norm_key="shared title", hooked_work_id=w.id)
    db.add(cw)
    db.commit()
    cid, wid = cw.id, w.id
    jobs_before = db.scalar(select(func.count(CrawlJob.id))) or 0
    db.close()

    r = bob.post(f"/api/catalog/{cid}/hook")
    assert r.status_code == 200 and r.json()["id"] == wid

    db = SessionLocal()
    # Bob now has membership; NO new crawl job was triggered for the already-hooked work.
    assert db.scalar(select(LibraryItem.id).where(
        LibraryItem.user_id == _uid("bob"), LibraryItem.work_id == wid)) is not None
    assert (db.scalar(select(func.count(CrawlJob.id))) or 0) == jobs_before
    db.close()
    # And it now appears in bob's library.
    assert wid in [x["id"] for x in bob.get("/api/works").json()]


def test_non_member_cannot_access_work_content(clients):
    """The critical isolation fix: a non-member must not read chapters/TOC/epub, send-to-kindle,
    or read/write progress for a work that isn't in their library."""
    from app.models import Chapter, ChapterContent

    admin, bob = clients
    db = SessionLocal()
    w = Work(title="Private Book")
    db.add(w)
    db.commit()
    db.refresh(w)
    db.add(LibraryItem(user_id=_uid("admin"), work_id=w.id))
    ch = Chapter(work_id=w.id, index=1, title="Ch 1", fetch_status="fetched")
    db.add(ch)
    db.commit()
    db.refresh(ch)
    content = ChapterContent(chapter_id=ch.id, format="html", body="<p>secret</p>",
                             word_count=1, checksum="x")
    db.add(content)
    db.commit()
    db.refresh(content)
    ch.content_id = content.id
    db.commit()
    wid, cid = w.id, ch.id
    db.close()

    # Bob is not a member → every content path 404s (admin, a member, still works).
    assert bob.get(f"/api/works/{wid}/chapters").status_code == 404
    assert admin.get(f"/api/works/{wid}/chapters").status_code == 200
    assert bob.get(f"/api/chapters/{cid}").status_code == 404
    assert admin.get(f"/api/chapters/{cid}").status_code == 200
    assert bob.get(f"/api/works/{wid}/export.epub").status_code == 404
    assert bob.post(f"/api/works/{wid}/send-to-kindle", json={"to": "x@kindle.com"}).status_code == 404
    assert bob.get(f"/api/works/{wid}/progress").status_code == 404
    assert bob.post(f"/api/works/{wid}/progress",
                    json={"last_chapter_id": cid, "scroll_fraction": 0.1,
                          "paragraph_index": 0}).status_code == 404


def test_non_admin_cannot_run_operator_work_actions(clients):
    """Operator actions on the shared crawl are admin-only."""
    admin, bob = clients
    db = SessionLocal()
    w = Work(title="Op Book")
    db.add(w)
    db.commit()
    db.refresh(w)
    db.add(LibraryItem(user_id=_uid("bob"), work_id=w.id))  # bob IS a member here
    db.commit()
    wid = w.id
    db.close()
    # Even as a member, bob can't edit the shared crawl policy or run the global check.
    assert bob.patch(f"/api/works/{wid}/crawl-policy", json={}).status_code == 403
    assert bob.post("/api/works/check-updates").status_code == 403


def test_non_admin_cannot_reach_admin_pages(clients):
    admin, bob = clients
    # Sources + Jobs are admin-only; the catalog browse stays open to everyone.
    assert bob.get("/api/sources").status_code == 403
    assert bob.get("/api/jobs").status_code == 403
    assert admin.get("/api/sources").status_code == 200
    assert admin.get("/api/jobs").status_code == 200
    assert bob.get("/api/catalog").status_code == 200   # browsing the index is allowed


def test_bookshelves_crud_and_membership(clients):
    admin, bob = clients
    # bob has a work in his library.
    db = SessionLocal()
    w = Work(title="Bob's Book")
    db.add(w)
    db.commit()
    db.refresh(w)
    db.add(LibraryItem(user_id=_uid("bob"), work_id=w.id))
    db.commit()
    wid = w.id
    db.close()

    # Create a shelf, with name-uniqueness enforced.
    sid = bob.post("/api/bookshelves", json={"name": "Favorites"}).json()["id"]
    assert bob.post("/api/bookshelves", json={"name": "Favorites"}).status_code == 409
    # Per-shelf automation settings.
    upd = bob.patch(f"/api/bookshelves/{sid}",
                    json={"auto_update": True, "notify_on_add": True, "goodreads_target": True}).json()
    assert upd["auto_update"] and upd["notify_on_add"] and upd["goodreads_target"]

    # Place the work on the shelf; it shows in the shelf count + the work's shelf_ids + ?shelf_id.
    assert bob.post(f"/api/bookshelves/{sid}/works/{wid}").json()["count"] == 1
    works = bob.get("/api/works").json()
    assert sid in next(x for x in works if x["id"] == wid)["shelf_ids"]
    assert [x["id"] for x in bob.get(f"/api/works?shelf_id={sid}").json()] == [wid]

    # Isolation: admin can't see or touch bob's shelf.
    assert admin.get(f"/api/works?shelf_id={sid}").status_code == 404
    assert admin.patch(f"/api/bookshelves/{sid}", json={"name": "x"}).status_code == 404

    # Remove from shelf (work stays in library); delete shelf (work stays in library).
    assert bob.request("DELETE", f"/api/bookshelves/{sid}/works/{wid}").json()["count"] == 0
    assert wid in [x["id"] for x in bob.get("/api/works").json()]
    bob.request("DELETE", f"/api/bookshelves/{sid}")
    assert bob.get("/api/bookshelves").json() == []
    assert wid in [x["id"] for x in bob.get("/api/works").json()]


def test_cannot_shelve_a_work_not_in_your_library(clients):
    admin, bob = clients
    db = SessionLocal()
    w = Work(title="Not Bob's")
    db.add(w)
    db.commit()
    db.refresh(w)
    wid = w.id
    db.close()
    sid = bob.post("/api/bookshelves", json={"name": "Shelf"}).json()["id"]
    assert bob.post(f"/api/bookshelves/{sid}/works/{wid}").status_code == 404
