"""Pull wanted (missing-format) items from companion apps → gated Shelf fetches (Phase 3)."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import companion
from app.models import CatalogWork, Integration, QueuedHook, User


def _reset(db):
    for m in (QueuedHook, CatalogWork, Integration, User):
        db.execute(delete(m))
    db.commit()


def _admin(db):
    u = User(username="admin", role="admin", is_active=True, password_hash="x")
    db.add(u)
    db.commit()


def _catalog(db, norm):
    db.add(CatalogWork(provider="openlibrary", domain="d", work_url=f"u/{norm}", norm_key=norm,
                       title=norm.title(), media_kind="text"))
    db.commit()


def test_queued_hooks_variant_migration_registered():
    # Regression: the queued_hooks additive-migration block must include `variant` (a duplicate dict
    # key once silently dropped it → the column never lands on an existing DB → crash on first pull).
    from app.db import _ADDITIVE_COLUMNS
    assert "variant" in _ADDITIVE_COLUMNS["queued_hooks"]


@pytest.mark.asyncio
async def test_ebook_hook_does_not_satisfy_audiobook_want(monkeypatch):
    # A hooked web ebook must NOT mark a same-title audiobook want as hooked (different format).
    from app.integrations import metadata_sync as ms
    from app.models import Work
    init_db()
    db = SessionLocal()
    _reset(db)
    db.execute(delete(Work))
    db.commit()
    db.add(CatalogWork(provider="web_index", domain="d", work_url="u", norm_key="dune",
                       title="Dune", media_kind="text"))
    eh = QueuedHook(title="Dune", norm_key="dune", media_kind="text", variant="ebook",
                    reason="goodreads", status="pending")
    ah = QueuedHook(title="Dune", norm_key="dune", media_kind="audio", variant="audiobook",
                    reason="storyteller", status="pending")
    db.add_all([eh, ah])
    db.commit()
    ah_id = ah.id

    async def fake_hook(db_, cand, **k):
        w = Work(title="Dune", media_kind="text")
        db_.add(w)
        db_.commit()
        db_.refresh(w)
        return w
    monkeypatch.setattr("app.ingestion.catalog.hook_entry", fake_hook)
    monkeypatch.setattr(ms, "_pipeline_fetch_queued", lambda db_, h: _none())

    await ms.process_queued_hooks(db)
    db2 = SessionLocal()
    assert db2.get(QueuedHook, ah_id).status == "pending"  # audiobook want NOT satisfied by the ebook
    db.close()
    db2.close()


async def _none():
    return None


def test_missing_format():
    assert companion._missing_format(True, False) == "audiobook"
    assert companion._missing_format(False, True) == "ebook"
    assert companion._missing_format(True, True) is None
    assert companion._missing_format(False, False) is None


def test_queue_want_gates(monkeypatch):
    init_db()
    db = SessionLocal()
    _reset(db)
    _admin(db)
    integ = Integration(kind="audiobookshelf", name="ABS", base_url="http://h", api_key="k", config={})
    db.add(integ)
    db.commit()

    # No catalog match → not queued.
    assert companion._queue_want(db, integ, "Unknown Title", "A", "audiobook") is False
    # With a catalog match → queued with the right variant.
    _catalog(db, "dune")
    assert companion._queue_want(db, integ, "Dune", "Frank Herbert", "audiobook") is True
    qh = db.scalar(select(QueuedHook))
    assert qh.variant == "audiobook" and qh.reason == "audiobookshelf" and qh.status == "pending"
    # Dedup: a second want for the same title+format is skipped.
    assert companion._queue_want(db, integ, "Dune", "Frank Herbert", "audiobook") is False
    # A DIFFERENT format for the same title IS allowed.
    assert companion._queue_want(db, integ, "Dune", "Frank Herbert", "ebook") is True


@pytest.mark.asyncio
async def test_pull_abs_skips_comics_and_caps(monkeypatch):
    init_db()
    db = SessionLocal()
    _reset(db)
    _admin(db)
    integ = Integration(kind="audiobookshelf", name="ABS", base_url="http://h", api_key="k",
                        config={"pull_wanted": True})
    db.add(integ)
    db.commit()
    db.refresh(integ)
    for t in ("Book One", "Book Two", "Comic One"):
        _catalog(db, companion.norm_title(t))

    items = [
        {"title": "Book One", "author": "A", "isbn": None, "asin": None,
         "ebook_format": "epub", "has_ebook": True, "has_audio": False},   # wants audiobook ✓
        {"title": "Comic One", "author": "A", "isbn": None, "asin": None,
         "ebook_format": "cbz", "has_ebook": True, "has_audio": False},    # comic → skipped
        {"title": "Book Two", "author": "A", "isbn": None, "asin": None,
         "ebook_format": None, "has_ebook": False, "has_audio": True},     # wants ebook ✓
    ]

    class FakeABS:
        async def book_libraries(self):
            return [{"id": "l1", "name": "Books", "folders": ["/x"]}]

        async def iter_items(self, lib_id, **kw):
            return items
    monkeypatch.setattr(companion, "client_for", lambda i: FakeABS())

    res = await companion._pull_abs(db, integ)
    assert res["queued"] == 2
    by = {(q.norm_key, q.variant) for q in db.scalars(select(QueuedHook)).all()}
    assert (companion.norm_title("Book One"), "audiobook") in by
    assert (companion.norm_title("Book Two"), "ebook") in by
    assert not any(q.norm_key == companion.norm_title("Comic One") for q in db.scalars(select(QueuedHook)).all())


@pytest.mark.asyncio
async def test_audiobook_want_routes_to_pipeline_not_webhook(monkeypatch):
    # An audiobook QueuedHook must NOT be satisfied by hooking a web-crawl EBOOK; it goes to the
    # pipeline with variant="audiobook".
    from app.integrations import metadata_sync as ms
    from types import SimpleNamespace
    init_db()
    db = SessionLocal()
    _reset(db)
    # A web-crawl ebook catalog entry exists for the title (would normally be web-hooked).
    db.add(CatalogWork(provider="web_index", domain="d", work_url="u", norm_key="dune",
                       title="Dune", media_kind="text"))
    qh = QueuedHook(title="Dune", norm_key="dune", media_kind="text", variant="audiobook",
                    reason="storyteller", status="pending")
    db.add(qh)
    db.commit()
    qid = qh.id

    captured = {}

    async def fake_pipeline(db_, hook):
        captured["variant"] = hook.variant
        return SimpleNamespace(id=77)
    monkeypatch.setattr(ms, "_pipeline_fetch_queued", fake_pipeline)

    async def boom_hook(*a, **k):
        raise AssertionError("audiobook want must not web-hook the ebook")
    monkeypatch.setattr("app.ingestion.catalog.hook_entry", boom_hook)

    await ms.process_queued_hooks(db)
    db_ = SessionLocal()
    q = db_.get(QueuedHook, qid)
    assert captured["variant"] == "audiobook"
    assert q.status == "downloading" and q.detail == "dljob:77"
    cw = db_.scalar(select(CatalogWork).where(CatalogWork.norm_key == "dune"))
    assert cw.hooked_work_id is None  # the web ebook was NOT hooked
    db.close()
    db_.close()


@pytest.mark.asyncio
async def test_pull_disabled_without_flag(monkeypatch):
    init_db()
    db = SessionLocal()
    _reset(db)
    integ = Integration(kind="storyteller", name="ST", base_url="http://h", api_key="pw",
                        config={"username": "u"})  # no pull_wanted
    db.add(integ)
    db.commit()

    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        return {}
    monkeypatch.setattr(companion, "_pull_storyteller", boom)
    out = await companion.pull_tick()
    assert called["n"] == 0  # pull_wanted off → never invoked
    db.close()
