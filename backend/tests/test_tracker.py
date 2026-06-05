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


@pytest.mark.asyncio
async def test_discover_updates_respects_start_chapter():
    """A work hooked from chapter 3 must never re-add chapters 1–2 on a refresh, but does pick
    up new chapters past the start."""
    init_db()
    db = SessionLocal()
    w = _work(db)
    w.start_chapter = 3
    db.commit()
    _add(db, w, 3)
    _add(db, w, 4)
    meta = WorkMeta(source_work_ref=w.source_work_ref, title="X", status="ongoing")
    toc = [ChapterRef(source_chapter_ref=f"{BASE}{i}", index=i, title=f"Chapter {i}")
           for i in range(1, 7)]  # source lists 1..6
    added, _ = await tracker.discover_updates(db, w, FakeAdapter(meta, toc))
    db.commit()
    idx = sorted(c.index for c in db.scalars(select(Chapter).where(Chapter.work_id == w.id)).all())
    assert idx == [3, 4, 5, 6]  # 1,2 never re-added; 5,6 are new
    assert added == 2
    db.close()


from app.ingestion.base import ComplianceDeclaration, SourceAdapter, registry  # noqa: E402


@registry.register
class _StartChAdapter(SourceAdapter):
    key = "start_ch_test"
    display_name = "Start-Chapter Test"
    base_url = "https://sct.test"
    compliance = ComplianceDeclaration(
        license_basis="user-attested", tos_permitted_default=True, robots_respected=False,
        needs_attestation=False, min_request_interval_s=0.0, max_daily_requests=10000,
    )

    async def discover_work(self, ref):
        return WorkMeta(source_work_ref=ref, title="SCT", status="ongoing",
                        total_chapters_expected=6)

    async def list_chapters(self, meta):
        return [ChapterRef(source_chapter_ref=f"https://sct.test/c/{i}", index=i,
                           title=f"Chapter {i}") for i in range(1, 7)]

    async def fetch_chapter(self, ref):
        return RawChapter(title=ref.title, body="x", fmt="html", next_ref=None)


@pytest.mark.asyncio
async def test_hook_work_start_chapter_skips_earlier(monkeypatch):
    """Hooking from chapter 3 creates only chapters 3..6, sets the backfill cursor there, and
    counts the tracked span (not the full series)."""
    from app.ingestion import engine

    async def _noop(db, work):  # skip the best-effort provider enrich (no network in tests)
        return None
    monkeypatch.setattr("app.integrations.metadata_sync.enrich_work_all_providers", _noop)

    init_db()
    db = SessionLocal()
    work = await engine.hook_work(db, "start_ch_test", "https://sct.test/x", start_chapter=3)
    idx = sorted(c.index for c in db.scalars(select(Chapter).where(Chapter.work_id == work.id)).all())
    assert idx == [3, 4, 5, 6]  # chapters 1,2 skipped entirely
    assert work.start_chapter == 3
    assert work.total_chapters_known == 4
    assert work.total_chapters_expected == 4  # 6 advertised − 2 skipped
    job = db.scalar(select(CrawlJob).where(
        CrawlJob.work_id == work.id, CrawlJob.kind == "backfill"))
    assert job is not None and job.cursor == {"next_index": 3}
    db.close()


@registry.register
class _PosIndexAdapter(SourceAdapter):
    """Mimics comix: chapters indexed by list POSITION (1..N); the real chapter NUMBER lives only
    in the title — so position != number."""
    key = "pos_index_test"
    display_name = "Position Index Test"
    base_url = "https://pos.test"
    compliance = ComplianceDeclaration(
        license_basis="user-attested", tos_permitted_default=True, robots_respected=False,
        needs_attestation=False, min_request_interval_s=0.0, max_daily_requests=10000,
    )

    async def discover_work(self, ref):
        return WorkMeta(source_work_ref=ref, title="POS", status="ongoing")

    async def list_chapters(self, meta):
        # positions 1..6 → chapter numbers 100..105 (like Kingdom: position 700 = "Chapter 677").
        return [ChapterRef(source_chapter_ref=f"https://pos.test/c/{100 + i}", index=i + 1,
                           title=f"Chapter {100 + i}") for i in range(6)]

    async def fetch_chapter(self, ref):
        return RawChapter(title=ref.title, body="x", fmt="html", next_ref=None)


@pytest.mark.asyncio
async def test_hook_start_chapter_is_chapter_number_not_list_position(monkeypatch):
    """Regression: hooking 'from chapter 103' must keep the chapters LABELLED 103+, not the items
    at position 103+. (Kingdom on comix indexes by position, so start_chapter=700 wrongly kept
    'Chapter 677'+.) The backfill cursor starts at the first kept chapter's index."""
    from app.ingestion import engine

    async def _noop(db, work):
        return None
    monkeypatch.setattr("app.integrations.metadata_sync.enrich_work_all_providers", _noop)

    init_db()
    db = SessionLocal()
    work = await engine.hook_work(db, "pos_index_test", "https://pos.test/x", start_chapter=103)
    chs = sorted(work.chapters, key=lambda c: c.index)
    assert [c.title for c in chs] == ["Chapter 103", "Chapter 104", "Chapter 105"]
    assert [c.index for c in chs] == [4, 5, 6]  # positions of chapters 103/104/105
    assert work.start_chapter == 103 and work.total_chapters_known == 3
    job = db.scalar(select(CrawlJob).where(CrawlJob.work_id == work.id, CrawlJob.kind == "backfill"))
    assert job.cursor == {"next_index": 4}  # first kept chapter's POSITION, not 103

    # A refresh must not re-add the skipped early chapters (number < 103).
    added, _ = await tracker.discover_updates(db, work, _PosIndexAdapter(None))
    assert added == 0
    db.close()
