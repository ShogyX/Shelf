"""The stalled-job reaper: revive crashed/parked/orphaned jobs, never finished works."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion.scheduler import _aware, reap_stalled_jobs
from app.models import Chapter, CrawlJob, Source, Work


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for model in (CrawlJob, Chapter, Work, Source):
        db.execute(delete(model))
    db.commit()
    db.close()
    yield


def _now():
    return datetime.now(UTC)


def _make(db, *, statuses, job=None, **work_kw):
    """Create a work with chapters of the given fetch_statuses and an optional job dict."""
    src = db.scalar(select(Source).where(Source.key == "generic_feed"))
    if src is None:
        src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                     tos_permitted=True)
        db.add(src)
        db.commit()
    work_kw.setdefault("status", "ongoing")
    w = Work(source_id=src.id, source_work_ref=f"https://s/{len(statuses)}-{id(statuses)}",
             title="W", hooked=True, **work_kw)
    db.add(w)
    db.commit()
    db.refresh(w)
    for i, st in enumerate(statuses, start=1):
        db.add(Chapter(work_id=w.id, index=i, source_chapter_ref=f"c{i}",
                       title=f"Ch {i}", fetch_status=st))
    if job is not None:
        db.add(CrawlJob(work_id=w.id, kind="backfill", cursor={}, **job))
    db.commit()
    db.refresh(w)
    return w


def test_reopens_orphaned_work_with_pending_chapters():
    db = SessionLocal()
    w = _make(db, statuses=["pending", "fetched"], job=None)  # no open job
    db.close()
    assert reap_stalled_jobs() == 1
    db = SessionLocal()
    jobs = db.scalars(select(CrawlJob).where(CrawlJob.work_id == w.id)).all()
    assert len(jobs) == 1 and jobs[0].status == "scheduled"
    db.close()


def test_requeues_failed_chapters_when_no_open_job():
    db = SessionLocal()
    w = _make(db, statuses=["fetched", "failed", "failed"], job=None)
    db.close()
    assert reap_stalled_jobs() == 1
    db = SessionLocal()
    states = [
        c.fetch_status
        for c in db.scalars(select(Chapter).where(Chapter.work_id == w.id)).all()
    ]
    assert states.count("pending") == 2 and states.count("failed") == 0  # revived
    db.close()


def test_failed_chapters_not_retried_too_often():
    # A work whose last job finished moments ago must NOT have its failed chapters
    # requeued (anti-thrash) — otherwise a permanently-broken chapter loops forever.
    db = SessionLocal()
    w = _make(db, statuses=["fetched", "failed"],
              job={"status": "done", "finished_at": _now() - timedelta(seconds=60)})
    db.close()
    assert reap_stalled_jobs() == 0
    db = SessionLocal()
    states = [
        c.fetch_status
        for c in db.scalars(select(Chapter).where(Chapter.work_id == w.id)).all()
    ]
    assert states.count("failed") == 1  # left failed, not requeued
    db.close()


def test_does_not_restart_a_finished_work():
    db = SessionLocal()
    _make(db, statuses=["fetched", "fetched"], status="complete", job=None)
    db.close()
    assert reap_stalled_jobs() == 0  # nothing pending/failed → leave it alone


def test_rearms_abandoned_running_job():
    db = SessionLocal()
    old = _now() - timedelta(hours=2)
    _make(db, statuses=["pending"], job={"status": "running", "started_at": old,
                                         "scheduled_for": old})
    db.close()
    assert reap_stalled_jobs() == 1
    db = SessionLocal()
    j = db.scalar(select(CrawlJob))
    assert j.status == "scheduled"
    db.close()


def test_leaves_source_budget_parked_job_alone():
    db = SessionLocal()
    future = _now() + timedelta(minutes=50)
    _make(db, statuses=["pending"],
          job={"status": "scheduled", "scheduled_for": future,
               "last_error": "source daily budget reached; resuming later"})
    db.close()
    # Must NOT pull it forward (would hammer the shared source budget).
    assert reap_stalled_jobs() == 0
    db = SessionLocal()
    j = db.scalar(select(CrawlJob))
    assert _aware(j.scheduled_for) > _now()
    db.close()


def test_pulls_forward_when_per_title_block_cleared():
    db = SessionLocal()
    future = _now() + timedelta(hours=5)
    # Parked in the future but no per-title window/limit set → block is clear → revive.
    _make(db, statuses=["pending"],
          job={"status": "scheduled", "scheduled_for": future,
               "last_error": "outside the title's allowed crawl hours; resuming later"})
    db.close()
    assert reap_stalled_jobs() == 1
    db = SessionLocal()
    j = db.scalar(select(CrawlJob))
    assert _aware(j.scheduled_for) <= _now()
    db.close()
