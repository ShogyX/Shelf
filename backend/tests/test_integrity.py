"""Active integrity checker: detect + fix skipped chapter numbers (contiguous indices)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import diagnose
from app.models import Chapter, CrawlJob, Source, Work

PREFIX = "https://s.test/novel/x/chapter/"


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for model in (CrawlJob, Chapter, Work, Source):
        db.execute(delete(model))
    db.commit()
    db.close()
    yield


def _work_with_numbers(db, numbers, *, titles=None, expected=None):
    """Create a hooked work whose chapters carry the given chapter numbers, stored with
    CONTIGUOUS indices (1..N) — the sequential-crawl shape where a skip hides in the URL,
    not the index."""
    src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                 tos_permitted=True)
    db.add(src)
    db.commit()
    w = Work(source_id=src.id, source_work_ref="https://s.test/novel/x", title="X",
             hooked=True, status="ongoing", total_chapters_expected=expected,
             total_chapters_known=len(numbers))
    db.add(w)
    db.commit()
    db.refresh(w)
    for idx, n in enumerate(numbers, start=1):
        ref = None if n is None else f"{PREFIX}{n}"
        t = (titles or {}).get(n) or (f"Chapter {n}" if n is not None else "Prologue")
        db.add(Chapter(work_id=w.id, source_chapter_ref=ref, index=idx, title=t,
                       fetch_status="fetched"))
    db.commit()
    db.refresh(w)
    return w


def test_numeric_gaps_detects_single_skip():
    db = SessionLocal()
    w = _work_with_numbers(db, [1, 2, 3, 4, 6, 7])  # 5 is skipped; indices are 1..6
    assert diagnose.numeric_gaps(db, w) == [5]
    db.close()


def test_numeric_gaps_ignores_irregular_numbering():
    db = SessionLocal()
    w = _work_with_numbers(db, [1, 10, 100, 250])  # sparse → not a 1..N run
    assert diagnose.numeric_gaps(db, w) == []
    db.close()


def test_completeness_flags_skip_as_incomplete():
    db = SessionLocal()
    w = _work_with_numbers(db, [1, 2, 3, 5])  # 4 skipped, no open job
    rep = diagnose.completeness(db, w)
    assert rep["chapter_gaps"] == [4]
    assert rep["health"] == "incomplete"
    assert "skipped" in rep["detail"]
    db.close()


def test_repair_fills_skip_and_reorders():
    db = SessionLocal()
    w = _work_with_numbers(db, [1, 2, 3, 4, 6])  # skip 5; "6" sits at index 5
    added = diagnose.repair_numeric_gaps(db, w)
    db.commit()
    assert added == 1
    chs = db.scalars(
        select(Chapter).where(Chapter.work_id == w.id).order_by(Chapter.index)
    ).all()
    # Now reads in numeric order 1..6, with the new chapter 5 pending and to-be-fetched.
    assert [c.source_chapter_ref for c in chs] == [f"{PREFIX}{n}" for n in (1, 2, 3, 4, 5, 6)]
    ch5 = next(c for c in chs if c.source_chapter_ref == f"{PREFIX}5")
    assert ch5.fetch_status == "pending"
    # Indices are contiguous 1..6 (no unique-constraint collision during reindex).
    assert [c.index for c in chs] == [1, 2, 3, 4, 5, 6]
    db.close()


def test_reindex_keeps_unnumbered_prologue_first():
    db = SessionLocal()
    # A leading unnumbered "Prologue" then chapters 1,2,4 (skip 3).
    w = _work_with_numbers(db, [None, 1, 2, 4])
    diagnose.repair_numeric_gaps(db, w)
    db.commit()
    chs = db.scalars(
        select(Chapter).where(Chapter.work_id == w.id).order_by(Chapter.index)
    ).all()
    assert chs[0].title == "Prologue"  # prologue stays first
    assert [c.source_chapter_ref for c in chs[1:]] == [f"{PREFIX}{n}" for n in (1, 2, 3, 4)]
    db.close()


def test_repair_runs_gap_first_at_next_tick():
    """A re-added missing chapter must be fetched FIRST on the next tick: it gets a low
    index (so the index-ordered pending query picks it first) and the backfill job is
    pulled forward to run now (not at the head crawl's distant next run)."""
    from app.ingestion.scheduler import _aware

    db = SessionLocal()
    w = _work_with_numbers(db, [1, 2, 3, 5])  # chapter 4 skipped
    far_future = datetime.now(UTC) + timedelta(hours=6)
    db.add(CrawlJob(work_id=w.id, kind="backfill", status="scheduled",
                    scheduled_for=far_future))
    db.commit()

    diagnose.repair(db, w)
    db.commit()

    chs = db.scalars(
        select(Chapter).where(Chapter.work_id == w.id).order_by(Chapter.index)
    ).all()
    ch4 = next(c for c in chs if c.source_chapter_ref == f"{PREFIX}4")
    ch5 = next(c for c in chs if c.source_chapter_ref == f"{PREFIX}5")
    assert ch4.fetch_status == "pending"
    assert ch4.index < ch5.index  # ordered before chapter 5 → fetched first
    job = db.scalar(
        select(CrawlJob).where(CrawlJob.work_id == w.id, CrawlJob.kind == "backfill")
    )
    assert _aware(job.scheduled_for) <= datetime.now(UTC)  # pulled forward to next tick
    db.close()


def test_integrity_tick_repairs_skip():
    import asyncio

    from app.ingestion.scheduler import integrity_tick

    db = SessionLocal()
    w = _work_with_numbers(db, [1, 2, 3, 5])  # skip 4
    db.close()
    asyncio.run(integrity_tick())  # now a @scheduled_task coroutine (owns its own Session, off-loop)
    db = SessionLocal()
    refs = {
        r for (r,) in db.execute(
            select(Chapter.source_chapter_ref).where(Chapter.work_id == w.id)
        ).all()
    }
    assert f"{PREFIX}4" in refs  # the skipped chapter was filled
    db.close()
