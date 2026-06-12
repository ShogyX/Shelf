"""Race-hardening unique constraints (F0.2): dedupe of historical collisions, enforcement,
and the insert_or_reuse savepoint pattern at the insert sites."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select, text

from app.db import (SessionLocal, dedupe_unique_collisions, engine, enforce_unique_indexes,
                    init_db, insert_or_reuse)
from app.models import (CatalogGroup, Chapter, CrawlJob, LibraryItem, StockItem, StockJob, User,
                        WatchedFolder, Work)


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    for m in (LibraryItem, Chapter, CrawlJob, StockItem, StockJob, WatchedFolder, CatalogGroup,
              Work, User):
        s.execute(delete(m))
    s.commit()
    yield s
    # These tests DROP the unique indexes to simulate a pre-upgrade DB; wipe + re-enforce so the
    # shared DB isn't left without them for a later test file.
    for m in (LibraryItem, Chapter, CrawlJob, StockItem, StockJob, WatchedFolder, CatalogGroup,
              Work, User):
        s.execute(delete(m))
    s.commit()
    s.close()
    enforce_unique_indexes()


def _drop_unique_indexes():
    """Simulate a pre-upgrade DB: the unique indexes don't exist yet (so dup rows can be made)."""
    with engine.begin() as conn:
        for name in ("uq_crawl_active", "uq_work_source_ref", "uq_stock_norm_key"):
            conn.execute(text(f"DROP INDEX IF EXISTS {name}"))


def test_dedupe_stock_items_keeps_stocked(db):
    _drop_unique_indexes()
    db.add(StockItem(norm_key="dune", title="Dune", status="pending"))
    db.add(StockItem(norm_key="dune", title="Dune", status="stocked"))
    db.add(StockItem(norm_key="dune", title="Dune", status="failed"))
    db.commit()
    dedupe_unique_collisions()
    enforce_unique_indexes()
    rows = db.scalars(select(StockItem).where(StockItem.norm_key == "dune")).all()
    assert len(rows) == 1 and rows[0].status == "stocked"   # the stocked row survives
    # …and the index now blocks a new duplicate.
    db.expire_all()
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        db.add(StockItem(norm_key="dune", title="Dune", status="pending"))
        db.flush()
    db.rollback()


def test_dedupe_crawl_jobs_scoped_by_kind(db):
    """An active backfill + descramble for the SAME work must BOTH survive (the descramble
    pipeline depends on a live backfill) — only same-kind active duplicates are removed."""
    _drop_unique_indexes()
    w = Work(title="W")
    db.add(w); db.commit(); db.refresh(w)
    db.add(CrawlJob(work_id=w.id, kind="backfill", status="scheduled"))
    db.add(CrawlJob(work_id=w.id, kind="backfill", status="running"))    # same-kind dup
    db.add(CrawlJob(work_id=w.id, kind="descramble", status="scheduled"))  # different kind: keep
    db.add(CrawlJob(work_id=w.id, kind="backfill", status="done"))       # terminal: history, keep
    db.commit()
    dedupe_unique_collisions()
    enforce_unique_indexes()
    rows = db.scalars(select(CrawlJob).where(CrawlJob.work_id == w.id)).all()
    kinds = sorted((j.kind, j.status) for j in rows)
    assert ("descramble", "scheduled") in kinds              # coexisting kind untouched
    assert ("backfill", "done") in kinds                     # terminal history untouched
    active_backfills = [j for j in rows if j.kind == "backfill" and j.status != "done"]
    assert len(active_backfills) == 1 and active_backfills[0].status == "running"


def test_dedupe_works_migrates_memberships(db):
    _drop_unique_indexes()
    u = User(username="u9", password_hash="x", role="user")
    db.add(u); db.commit(); db.refresh(u)
    w1 = Work(title="Book", source_id=None, source_work_ref=None)
    db.add(w1); db.commit()  # NULL ref — must be exempt (never deduped)
    # Two race-created duplicates of one file; the user's membership is on the LOSER.
    from app.models import Source
    src = db.scalar(select(Source))
    if src is None:
        src = Source(key="local_folder", display_name="Local", adapter_key="local_folder",
                     tos_permitted=True)
        db.add(src); db.commit(); db.refresh(src)
    keep = Work(title="Book A", source_id=src.id, source_work_ref="localfolder:1:/x/a.epub")
    lose = Work(title="Book A", source_id=src.id, source_work_ref="localfolder:1:/x/a.epub")
    db.add_all([keep, lose]); db.commit()
    db.add(Chapter(work_id=keep.id, index=1, source_chapter_ref="c1"))  # keep = most chapters
    db.add(LibraryItem(user_id=u.id, work_id=lose.id))
    db.commit()
    keep_id, lose_id = keep.id, lose.id

    dedupe_unique_collisions()
    enforce_unique_indexes()
    db.expire_all()
    assert db.get(Work, lose_id) is None                      # spare dropped
    li = db.scalar(select(LibraryItem).where(LibraryItem.user_id == u.id))
    assert li is not None and li.work_id == keep_id           # membership migrated, not lost
    assert db.get(Work, w1.id) is not None                    # NULL-ref row untouched


def test_insert_or_reuse_savepoint_preserves_batch(db):
    """A collision must roll back ONLY the colliding insert — earlier rows in the SAME
    still-uncommitted batch survive (a sync batch must not be aborted, nor silently discarded,
    by one duplicate)."""
    enforce_unique_indexes()
    existing = WatchedFolder(path="/tmp/iou-dup")
    db.add(existing); db.commit()                  # the row a later insert will collide with

    f1 = WatchedFolder(path="/tmp/iou-one")        # UNCOMMITTED earlier batch row
    db.add(f1); db.flush()
    # Collide on the unique path: must reuse `existing`, NOT roll back the outer txn (which would
    # discard the uncommitted f1 — the bug a naive db.rollback() in the helper caused).
    row, created = insert_or_reuse(db, WatchedFolder(path="/tmp/iou-dup"),
                                   select(WatchedFolder).where(WatchedFolder.path == "/tmp/iou-dup"))
    assert created is False and row is not None and row.id == existing.id
    # f1 is still pending in the live outer transaction
    assert f1 in db.new or db.scalar(
        select(WatchedFolder).where(WatchedFolder.path == "/tmp/iou-one")) is not None
    db.commit()
    assert db.scalar(select(WatchedFolder).where(WatchedFolder.path == "/tmp/iou-one")) is not None
    # a fresh, non-colliding insert still reports created=True
    row2, created2 = insert_or_reuse(db, WatchedFolder(path="/tmp/iou-three"),
                                     select(WatchedFolder).where(WatchedFolder.path == "/tmp/iou-three"))
    assert created2 is True and row2 is not None
    db.commit()


def test_queue_selection_concurrent_duplicate_counts_as_skipped(db, monkeypatch):
    """The race window: queue_selection's existence pre-check misses, but the row already exists
    when it inserts (a concurrent writer won). insert_or_reuse must turn that IntegrityError into a
    'skipped' (savepoint-contained) instead of a duplicate row or an aborted batch."""
    from app.ingestion import stock as stock_mod
    enforce_unique_indexes()
    g1 = CatalogGroup(norm_key="alpha", title="Alpha", media_bucket="prose")
    g2 = CatalogGroup(norm_key="beta", title="Beta", media_bucket="prose")
    db.add_all([g1, g2]); db.commit()
    monkeypatch.setattr(stock_mod, "_select_groups", lambda *a, **k: [g1, g2])
    # 'alpha' already exists (the concurrent writer's row), committed before we queue.
    db.add(StockItem(norm_key="alpha", title="Alpha", status="pending")); db.commit()
    # Force the existence PRE-CHECK to miss for both, so both reach the insert path — alpha then
    # collides on the unique index (race), beta inserts cleanly.
    real_scalar = db.scalar
    def blind_precheck(stmt, *a, **k):
        s = str(stmt)
        if "stock_items.id" in s:
            return None
        return real_scalar(stmt, *a, **k)
    monkeypatch.setattr(db, "scalar", blind_precheck)

    out = stock_mod.queue_selection(db, name="t", limit=10)
    assert out["queued"] == 1 and out["skipped"] == 1        # alpha collided→skipped, beta queued
    rows = db.scalars(select(StockItem)).all()
    assert sorted(i.norm_key for i in rows) == ["alpha", "beta"]   # exactly one each, no duplicate
