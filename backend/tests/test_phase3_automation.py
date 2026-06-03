"""Phase-3 shelf automation behaviours: the per-shelf toggles actually *do* something.

  * auto_update   → gates periodic refresh-job scheduling (any member opted in)
  * auto_kindle   → mails newly fetched chapters (baseline-then-send, no backlog flood)
  * notify_on_add → pushes via Apprise when a title auto-hooks onto a notify shelf
  * per-user Goodreads → wishlist auto-hooks land in the connecting user's library + shelf
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, func, select

from app.db import SessionLocal, init_db
from app.integrations import metadata as M
from app.integrations import metadata_sync as MS
from app.ingestion import scheduler
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
    QueuedHook,
    Source,
    User,
    UserSettings,
    Work,
)


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    for m in (LibraryItem, BookshelfItem, Bookshelf, QueuedHook, CrawlJob, CatalogWork,
              ChapterContent, Chapter, Integration, Work, UserSettings, User):
        s.execute(delete(m))
    s.execute(delete(AppSetting).where(AppSetting.key == "library_membership_seed_v1"))
    s.commit()
    yield s
    s.close()


def _user(db, name="bob", role="user") -> int:
    u = User(username=name, password_hash="x", role=role, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u.id


def _trackable_work(db, title="Serial") -> Work:
    src = db.scalar(select(Source).where(Source.key == "generic_feed"))
    if src is None:
        src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                     tos_permitted=True)
        db.add(src)
        db.commit()
    w = Work(source_id=src.id, source_work_ref="ref-1", title=title,
             hooked=True, status="ongoing")
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def _shelf(db, user_id, **flags) -> Bookshelf:
    sh = Bookshelf(user_id=user_id, name="S", **flags)
    db.add(sh)
    db.commit()
    db.refresh(sh)
    return sh


def _place(db, shelf_id, user_id, work_id):
    db.add(LibraryItem(user_id=user_id, work_id=work_id))
    db.add(BookshelfItem(shelf_id=shelf_id, work_id=work_id))
    db.commit()


def _fetched_chapter(db, work_id, index):
    ch = Chapter(work_id=work_id, index=index, title=f"Ch {index}", fetch_status="fetched")
    db.add(ch)
    db.commit()
    db.refresh(ch)
    c = ChapterContent(chapter_id=ch.id, format="html", body=f"<p>body {index}</p>",
                       word_count=2, checksum=f"c{index}")
    db.add(c)
    db.commit()
    db.refresh(c)
    ch.content_id = c.id
    db.commit()


# ----------------------------------------------------------------- auto_update
def test_auto_update_gates_refresh_jobs(db):
    uid = _user(db)
    w = _trackable_work(db)
    sh = _shelf(db, uid, auto_update=False)
    _place(db, sh.id, uid, w.id)

    # Nobody opted in → no refresh job enqueued, even for a trackable ongoing work.
    scheduler.schedule_refresh_jobs()
    assert (db.scalar(select(func.count(CrawlJob.id)).where(CrawlJob.work_id == w.id)) or 0) == 0

    # Flip the shelf to auto_update → a member opted in → a refresh job appears.
    sh.auto_update = True
    db.commit()
    scheduler.schedule_refresh_jobs()
    job = db.scalar(select(CrawlJob).where(CrawlJob.work_id == w.id, CrawlJob.kind == "refresh"))
    assert job is not None


# ----------------------------------------------------------------- auto_kindle
def test_auto_kindle_baselines_then_sends(db, monkeypatch):
    uid = _user(db)
    w = _trackable_work(db, title="Mailed")
    _fetched_chapter(db, w.id, 1)
    _fetched_chapter(db, w.id, 2)
    sh = _shelf(db, uid, auto_kindle=True)
    _place(db, sh.id, uid, w.id)
    db.add(UserSettings(user_id=uid, theme="system", reader_prefs={},
                        kindle_email="me@kindle.com",
                        delivery_config={"smtp_host": "smtp.x", "smtp_from": "a@b.com"}))
    db.commit()

    sent: list[tuple] = []

    import app.kindle as kindle
    monkeypatch.setattr(kindle, "send_document",
                        lambda cfg, **kw: sent.append((kw["to_email"], kw["filename"])))

    # First pass: baseline only — record the ceiling, mail nothing of the backlog.
    scheduler.auto_kindle_tick()
    li = db.scalar(select(LibraryItem).where(
        LibraryItem.user_id == uid, LibraryItem.work_id == w.id))
    db.refresh(li)
    assert li.auto_kindle_through == 2 and sent == []

    # A new chapter is fetched → next pass mails exactly the new content + advances the cursor.
    _fetched_chapter(db, w.id, 3)
    scheduler.auto_kindle_tick()
    db.refresh(li)
    assert li.auto_kindle_through == 3
    assert len(sent) == 1 and sent[0][0] == "me@kindle.com"


def test_auto_kindle_skips_unconfigured_member(db, monkeypatch):
    uid = _user(db)
    w = _trackable_work(db)
    _fetched_chapter(db, w.id, 1)
    sh = _shelf(db, uid, auto_kindle=True)
    _place(db, sh.id, uid, w.id)
    # No UserSettings / SMTP at all → can't deliver; cursor stays NULL (no flood when set up later).
    import app.kindle as kindle
    monkeypatch.setattr(kindle, "send_document",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send")))
    scheduler.auto_kindle_tick()
    li = db.scalar(select(LibraryItem).where(
        LibraryItem.user_id == uid, LibraryItem.work_id == w.id))
    assert li.auto_kindle_through is None


# ---------------------------------------------------------- per-user Goodreads
def test_import_goodreads_stamps_owner_and_shelf(db, monkeypatch):
    uid = _user(db)
    shelf = _shelf(db, uid, goodreads_target=True)

    class _GR:
        kind = "goodreads"
        async def wanted(self):
            return [M.ProviderMatch(ref="1", title="Wanted Book", author="Auth")]
    monkeypatch.setattr(M, "provider_for", lambda integ, config=None: _GR())

    integ = Integration(kind="goodreads", name="GR", base_url="", api_key="", user_id=uid)
    db.add(integ)
    db.commit()

    res = asyncio.run(MS.import_goodreads(db, integ))
    assert res["queued"] == 1
    qh = db.scalar(select(QueuedHook).where(QueuedHook.reason == "goodreads"))
    assert qh.user_id == uid and qh.target_shelf_id == shelf.id


def test_import_goodreads_already_hooked_adds_membership(db, monkeypatch):
    uid = _user(db)
    w = _trackable_work(db, title="Already Here")
    # A hooked catalog entry exists for the wanted title (someone else crawled it).
    db.add(CatalogWork(provider="web_index", domain="x.com", work_url="https://x.com/n",
                       title="Already Here", norm_key="already here", hooked_work_id=w.id))
    db.commit()

    class _GR:
        kind = "goodreads"
        async def wanted(self):
            return [M.ProviderMatch(ref="1", title="Already Here", author=None)]
    monkeypatch.setattr(M, "provider_for", lambda integ, config=None: _GR())

    integ = Integration(kind="goodreads", name="GR", base_url="", api_key="", user_id=uid)
    db.add(integ)
    db.commit()

    res = asyncio.run(MS.import_goodreads(db, integ))
    # Membership-only: no queue (the crawl is shared), but the user now has it in their library.
    assert res["queued"] == 0
    assert db.scalar(select(LibraryItem.id).where(
        LibraryItem.user_id == uid, LibraryItem.work_id == w.id)) is not None


# ----------------------------------------------------------------- notify_on_add
def test_notify_on_add_pushes_to_owner(db, monkeypatch):
    uid = _user(db)
    shelf = _shelf(db, uid, notify_on_add=True)
    db.add(UserSettings(user_id=uid, theme="system", reader_prefs={},
                        apprise_url="ntfy://example/topic"))
    qh = QueuedHook(title="New Title", norm_key="new title", reason="goodreads",
                    media_kind="text", status="pending", user_id=uid, target_shelf_id=shelf.id)
    db.add(qh)
    cw = CatalogWork(provider="web_index", domain="x.com", work_url="https://x.com/nt",
                     title="New Title", norm_key="new title")
    db.add(cw)
    db.commit()

    hooked = _trackable_work(db, title="New Title")

    async def _fake_hook(_db, entry):
        entry.hooked_work_id = hooked.id
        return hooked
    import app.ingestion.catalog as cat
    monkeypatch.setattr(cat, "hook_entry", _fake_hook)

    pushes: list[tuple] = []
    import app.notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify",
                        lambda url, title, body: pushes.append((url, body)) or True)

    res = asyncio.run(MS.process_queued_hooks(db))
    assert res["hooked"] == 1 and res["notified"] == 1
    assert pushes and pushes[0][0] == "ntfy://example/topic"
    # And it landed on the user's shelf.
    assert db.scalar(select(BookshelfItem.id).where(
        BookshelfItem.shelf_id == shelf.id, BookshelfItem.work_id == hooked.id)) is not None


# ----------------------------------------------------------------- CLI filter
def test_cli_work_rows_filters_by_membership(db):
    from app.cli import _work_rows

    uid = _user(db)
    mine = _trackable_work(db, title="Mine")
    _trackable_work(db, title="Not Mine")
    db.add(LibraryItem(user_id=uid, work_id=mine.id))
    db.commit()

    rows = _work_rows(db, uid)
    assert [r["title"] for r in rows] == ["Mine"]
    # No account resolved → empty (no personal library to show).
    assert _work_rows(db, None) == []


# ------------------------------------------------ per-user Goodreads connect surface
def test_per_user_goodreads_is_auth_gated_and_stamps(db, monkeypatch):
    """A regular (non-admin) user can connect THEIR OWN Goodreads; its wishlist queues stamped to
    them + their goodreads_target shelf."""
    from fastapi.testclient import TestClient

    from app.main import app

    # Fresh users (the `db` fixture already wiped the tables).
    db.close()
    admin = TestClient(app)
    admin.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
    admin.post("/api/users", json={"username": "bob", "password": "test1234", "role": "user"})
    bob = TestClient(app)
    bob.post("/api/auth/login", json={"username": "bob", "password": "test1234"})

    class _GR:
        kind = "goodreads"
        async def wanted(self):
            return [M.ProviderMatch(ref="1", title="Bob Wishlist", author="A")]
    monkeypatch.setattr(M, "provider_for", lambda integ, config=None: _GR())

    sid = bob.post("/api/bookshelves", json={"name": "Wishlist"}).json()["id"]
    bob.patch(f"/api/bookshelves/{sid}", json={"goodreads_target": True})

    assert bob.get("/api/me/goodreads").json()["connected"] is False
    r = bob.put("/api/me/goodreads", json={"goodreads_user_id": "12345", "shelf": "to-read"})
    assert r.status_code == 200 and r.json()["connected"] is True

    s = SessionLocal()
    bob_id = s.scalar(select(User.id).where(User.username == "bob"))
    qh = s.scalar(select(QueuedHook).where(QueuedHook.norm_key == "bob wishlist"))
    assert qh is not None and qh.user_id == bob_id and qh.target_shelf_id == sid
    s.close()
