"""Tests for per-shelf path monitoring + automation events."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.ingestion.adapters  # noqa: F401 — register local_folder adapter
from app.db import SessionLocal, init_db
from app.ingestion import local_folder as lf
from app.main import app
from app.models import Bookshelf, User, UserSettings, WatchedFolder, Work


def test_fire_shelf_events_respects_toggles(monkeypatch):
    init_db(); db = SessionLocal()
    db.execute(delete(WatchedFolder)); db.execute(delete(Bookshelf))
    db.execute(delete(UserSettings)); db.execute(delete(User)); db.execute(delete(Work))
    db.commit()
    from app.models import NotificationChannel
    u = User(username="u", password_hash="x", role="user"); db.add(u); db.commit(); db.refresh(u)
    db.add(UserSettings(user_id=u.id, kindle_email="k@kindle.com",
                        delivery_config={"email_to": "me@x.com"}))
    db.add(NotificationChannel(user_id=u.id, kind="ntfy", apprise_url="ntfy://t", enabled=True))
    shelf = Bookshelf(user_id=u.id, name="Inbox", notify_on_add=True, auto_kindle=True,
                      notify_email=True)
    db.add(shelf); db.commit(); db.refresh(shelf)
    w = Work(title="A Book"); db.add(w); db.commit(); db.refresh(w)
    folder = WatchedFolder(path="/tmp/x", shelf_id=shelf.id, user_id=u.id)
    db.add(folder); db.commit(); db.refresh(folder)

    sent = []
    monkeypatch.setattr(lf, "_send_book", lambda db_, work, delivery, to, label: sent.append((label, to)))
    pushes = []
    # Pushes now flow through the notifications dispatcher (library.added → the user's channels).
    monkeypatch.setattr("app.notifications.notify", lambda url, t, b: pushes.append(b) or True)

    lf._fire_shelf_events(db, folder, [w.id])
    assert pushes and "A Book" in pushes[0]
    assert ("auto-kindle", "k@kindle.com") in sent
    assert ("shelf-email", "me@x.com") in sent

    # The Kindle/email shelf toggles still gate their sends; the library.added push is governed by
    # the user's per-event preference (default on), not the shelf flags, so it keeps firing.
    shelf.auto_kindle = shelf.notify_email = False
    db.commit(); sent.clear(); pushes.clear()
    lf._fire_shelf_events(db, folder, [w.id])
    assert not sent
    assert pushes and "A Book" in pushes[0]
    db.close()


def test_baseline_first_scan_silent_then_fires(monkeypatch, tmp_path):
    """Mapping a populated folder must NOT email the backlog: the first scan places existing files
    silently; only content discovered AFTER the mapping fires events."""
    init_db(); db = SessionLocal()
    db.execute(delete(WatchedFolder)); db.execute(delete(Bookshelf))
    db.execute(delete(User)); db.execute(delete(Work)); db.commit()
    u = User(username="u2", password_hash="x", role="admin"); db.add(u); db.commit(); db.refresh(u)
    shelf = Bookshelf(user_id=u.id, name="Drop", notify_email=True); db.add(shelf); db.commit(); db.refresh(shelf)
    folder = WatchedFolder(path=str(tmp_path), shelf_id=shelf.id, user_id=u.id, last_scan_at=None)
    db.add(folder); db.commit(); db.refresh(folder)

    fired = []
    monkeypatch.setattr(lf, "_fire_shelf_events", lambda db_, f, ids: fired.append(list(ids)))

    # A pre-existing book at mapping time.
    (tmp_path / "old.md").write_text("# Chapter 1\nHello there, this is an existing book.")
    s1 = lf.sync_folder(db, folder)
    assert s1["added"] == 1 and not fired             # baseline: placed silently, no events
    from app.models import BookshelfItem
    assert db.scalar(select(BookshelfItem.id).where(BookshelfItem.shelf_id == shelf.id)) is not None

    # New content discovered after mapping → events fire.
    (tmp_path / "new.md").write_text("# Chapter 1\nA brand new arrival in the folder.")
    db.refresh(folder)
    lf.sync_folder(db, folder)
    assert fired and len(fired[-1]) == 1              # only the new work fires
    db.close()


def _admin_client():
    init_db(); db = SessionLocal()
    db.execute(delete(WatchedFolder)); db.execute(delete(Bookshelf)); db.execute(delete(User))
    db.commit(); db.close()
    c = TestClient(app)
    c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
    return c


def test_watch_path_is_admin_only(tmp_path):
    with _admin_client() as admin:
        # admin creates a normal user
        admin.post("/api/users", json={"username": "bob", "password": "test1234", "role": "user"})
        with TestClient(app) as bob:
            bob.post("/api/auth/login", json={"username": "bob", "password": "test1234"})
            # non-admin cannot map a host path
            r = bob.post("/api/bookshelves", json={"name": "Mine", "watch_path": str(tmp_path)})
            assert r.status_code == 403
            # but can create a normal shelf
            assert bob.post("/api/bookshelves", json={"name": "Plain"}).status_code == 200


def test_admin_watch_path_creates_and_removes_folder(tmp_path, monkeypatch):
    # avoid touching the real watchdog observer
    monkeypatch.setattr("app.ingestion.watcher.manager.add", lambda *a, **k: None)
    monkeypatch.setattr("app.ingestion.watcher.manager.remove", lambda *a, **k: None)
    with _admin_client() as admin:
        r = admin.post("/api/bookshelves", json={"name": "Drop", "watch_path": str(tmp_path)})
        assert r.status_code == 200
        sid = r.json()["id"]
        db = SessionLocal()
        wf = db.scalar(select(WatchedFolder).where(WatchedFolder.shelf_id == sid))
        assert wf is not None and wf.path == str(tmp_path) and wf.user_id is not None
        db.close()
        # clearing the path removes the backing folder
        assert admin.patch(f"/api/bookshelves/{sid}", json={"watch_path": ""}).status_code == 200
        db = SessionLocal()
        assert db.scalar(select(WatchedFolder).where(WatchedFolder.shelf_id == sid)) is None
        db.close()


def test_sync_does_not_add_stock_dir_files_to_library(tmp_path, monkeypatch):
    """The operator stock dir can live INSIDE a watched library folder (e.g. .../Books/Stock). A
    shelf-mapped sync must add the library file to the user's library but NOT the stocked file —
    stocked works are shared/operator-managed, not the user's deliberate library."""
    from app.ingestion import stock as stock_mod
    from app.models import BookshelfItem, LibraryItem
    init_db(); db = SessionLocal()
    for m in (BookshelfItem, LibraryItem, WatchedFolder, Bookshelf, Work, User):
        db.execute(delete(m))
    db.commit()
    u = User(username="op", password_hash="h", role="admin"); db.add(u); db.commit(); db.refresh(u)
    shelf = Bookshelf(user_id=u.id, name="Lib"); db.add(shelf); db.commit(); db.refresh(shelf)
    # stock dir is a SUBFOLDER of the watched library folder
    stock = tmp_path / "Stock"; stock.mkdir()
    stock_mod.set_stock_dir(db, str(stock))
    (tmp_path / "my-book.md").write_text("# Chapter 1\nA real library book I own.")
    (stock / "stocked.md").write_text("# Chapter 1\nAn operator-stocked shared title.")
    folder = WatchedFolder(path=str(tmp_path), shelf_id=shelf.id, user_id=u.id, recursive=True,
                           last_scan_at=None)
    db.add(folder); db.commit(); db.refresh(folder)

    lf.sync_folder(db, folder)
    paths = {w.local_path for w in db.scalars(select(Work)).all()}
    assert any(p and p.endswith("my-book.md") for p in paths)
    assert any(p and "/Stock/" in p for p in paths)             # stock file WAS indexed as a Work
    in_lib_paths = {db.get(Work, li.work_id).local_path for li in
                    db.scalars(select(LibraryItem).where(LibraryItem.user_id == u.id)).all()}
    assert any(p and p.endswith("my-book.md") for p in in_lib_paths)        # library file added
    assert not any(p and "/Stock/" in (p or "") for p in in_lib_paths)      # stock file NOT added
    db.close()


def test_upsert_media_work_dedupes_by_content_hash():
    """13C: the same file bytes imported under a different name/ref adopt the EXISTING Work
    (content-hash dedupe) and re-home it, instead of creating a duplicate."""
    import hashlib

    from sqlalchemy import func

    from app.ingestion.base import registry
    from app.ingestion.engine import ensure_source
    from app.ingestion.local_folder import upsert_media_work
    from app.ingestion.media import parse_media

    init_db(); db = SessionLocal()
    db.execute(delete(Work)); db.commit()
    src = ensure_source(db, registry.get("local_import"))
    data = b"# Chapter One\nReal content so it parses into at least one chapter.\nMore lines.\n"
    h = hashlib.sha256(data).hexdigest()

    w1 = upsert_media_work(db, src, source_work_ref="local:alpha.md",
                           parsed=parse_media(data, "alpha.md"), cover_key="c-alpha",
                           local_path="/x/alpha.md", content_hash=h)
    # Byte-identical file under a different name/ref → same Work, re-homed (no duplicate).
    w2 = upsert_media_work(db, src, source_work_ref="local:beta.md",
                           parsed=parse_media(data, "beta.md"), cover_key="c-beta",
                           local_path="/x/beta.md", content_hash=h)
    assert w1.id == w2.id
    assert db.scalar(select(func.count(Work.id))) == 1
    assert w2.source_work_ref == "local:beta.md" and w2.content_hash == h
    db.close()
