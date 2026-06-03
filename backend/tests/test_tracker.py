"""Update tracker: refreshed metadata + new chapters (TOC + sequential re-seed)."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import tracker
from app.ingestion.base import ChapterRef, RawChapter, WorkMeta
from app.models import Chapter, CrawlJob, Source, Work

BASE = "https://s.test/novel/x/chapter/"


@pytest.fixture(autouse=True)
def _clean():
    """Each test starts from empty works/chapters/sources (the test DB is shared)."""
    init_db()
    db = SessionLocal()
    for model in (CrawlJob, Chapter, Work, Source):
        db.execute(delete(model))
    db.commit()
    db.close()
    yield


class FakeAdapter:
    key = "generic_feed"

    def __init__(self, meta: WorkMeta, toc: list[ChapterRef], next_ref: str | None = None):
        self._meta = meta
        self._toc = toc
        self._next_ref = next_ref

    async def discover_work(self, ref):
        return self._meta

    async def list_chapters(self, meta):
        return self._toc

    async def fetch_chapter(self, ref):
        return RawChapter(title=ref.title, body="x", fmt="html", next_ref=self._next_ref)


def _work(db, *, adapter_key="generic_feed", ref="https://s.test/novel/x", expected=None) -> Work:
    src = Source(key=f"k{adapter_key}", display_name=adapter_key, adapter_key=adapter_key,
                 tos_permitted=True)
    db.add(src)
    db.commit()
    w = Work(source_id=src.id, source_work_ref=ref, title="X", hooked=True,
             status="ongoing", total_chapters_expected=expected)
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def _add(db, work, index, status="fetched"):
    db.add(Chapter(work_id=work.id, index=index, source_chapter_ref=f"{BASE}{index}",
                   title=f"Chapter {index}", fetch_status=status))
    db.commit()


@pytest.mark.asyncio
async def test_discover_updates_enqueues_new_toc_chapters_and_refreshes_metadata():
    init_db()
    db = SessionLocal()
    w = _work(db)
    _add(db, w, 1)
    _add(db, w, 2)
    meta = WorkMeta(source_work_ref=w.source_work_ref, title="X",
                    cover_url="https://s.test/cover.jpg", description="A fresh synopsis.",
                    total_chapters_expected=4, status="ongoing")
    toc = [ChapterRef(source_chapter_ref=f"{BASE}{i}", index=i, title=f"Chapter {i}")
           for i in range(1, 5)]
    added, changed = await tracker.discover_updates(db, w, FakeAdapter(meta, toc))
    db.commit()
    assert added == 2 and changed is True
    indexes = {c.index for c in db.scalars(select(Chapter).where(Chapter.work_id == w.id)).all()}
    assert indexes == {1, 2, 3, 4}
    assert w.cover_url == "https://s.test/cover.jpg"
    assert w.total_chapters_expected == 4
    db.close()


@pytest.mark.asyncio
async def test_discover_updates_reseeds_sequential_serial():
    init_db()
    db = SessionLocal()
    w = _work(db)
    for i in (1, 2, 3):
        _add(db, w, i)
    # Sequential: the TOC only ever returns the seed (chapter 1, already present).
    meta = WorkMeta(source_work_ref=w.source_work_ref, title="X")
    seed = [ChapterRef(source_chapter_ref=f"{BASE}1", index=1, title="Chapter 1")]
    added, _changed = await tracker.discover_updates(db, w, FakeAdapter(meta, seed))
    db.commit()
    assert added == 1
    nxt = db.scalar(select(Chapter).where(Chapter.work_id == w.id, Chapter.index == 4))
    assert nxt is not None and nxt.source_chapter_ref == f"{BASE}4"
    assert nxt.fetch_status == "pending"
    db.close()


@pytest.mark.asyncio
async def test_discover_updates_no_new_content():
    init_db()
    db = SessionLocal()
    w = _work(db)
    # Non-numeric ref so sequential synthesis can't fabricate a next chapter,
    # and the fake adapter scrapes no next-link.
    db.add(Chapter(work_id=w.id, index=1, source_chapter_ref="https://s.test/only-chapter",
                   title="Chapter 1", fetch_status="fetched"))
    db.commit()
    meta = WorkMeta(source_work_ref=w.source_work_ref, title="X")
    seed = [ChapterRef(source_chapter_ref="https://s.test/only-chapter", index=1,
                       title="Chapter 1")]
    added, changed = await tracker.discover_updates(db, w, FakeAdapter(meta, seed, next_ref=None))
    assert added == 0 and changed is False
    db.close()


@pytest.mark.asyncio
async def test_check_work_skips_static_sources():
    init_db()
    db = SessionLocal()
    w = _work(db, adapter_key="gutenberg", ref="74")
    r = await tracker.check_work(db, w)
    assert r["checked"] is False and r["new_chapters"] == 0
    assert w.last_checked_at is not None and w.last_update_at is None
    db.close()


@pytest.mark.asyncio
async def test_check_work_stamps_and_records_update(monkeypatch):
    init_db()
    db = SessionLocal()
    w = _work(db)
    _add(db, w, 1)
    meta = WorkMeta(source_work_ref=w.source_work_ref, title="X", status="ongoing")
    toc = [ChapterRef(source_chapter_ref=f"{BASE}{i}", index=i, title=f"Chapter {i}")
           for i in (1, 2)]
    fake = FakeAdapter(meta, toc)
    monkeypatch.setattr("app.ingestion.tracker.adapter_for", lambda src: fake)
    r = await tracker.check_work(db, w)
    assert r["checked"] is True and r["new_chapters"] == 1
    assert w.last_checked_at is not None and w.last_update_at is not None
    # A backfill/refresh job was opened so the new chapter gets fetched.
    from app.models import CrawlJob
    assert db.scalar(select(CrawlJob).where(CrawlJob.work_id == w.id))
    db.close()


@pytest.mark.asyncio
async def test_discover_updates_raises_stale_ceiling_on_continuation():
    """A serial advertised at 3 chapters that gains a 4th must raise its ceiling so the new
    chapter never reads as 'beyond the limit'."""
    init_db()
    db = SessionLocal()
    w = _work(db, expected=3)
    w.total_chapters_known = 3
    db.commit()
    for i in (1, 2, 3):
        _add(db, w, i)
    meta = WorkMeta(source_work_ref=w.source_work_ref, title="X")  # no fresh advertised total
    seed = [ChapterRef(source_chapter_ref=f"{BASE}1", index=1, title="Chapter 1")]
    added, _ = await tracker.discover_updates(db, w, FakeAdapter(meta, seed))
    db.commit()
    assert added == 1                          # chapter 4 re-seeded
    assert w.total_chapters_known == 4
    assert w.total_chapters_expected == 4      # ceiling lifted from 3 → 4
    db.close()
