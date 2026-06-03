"""Deleting/pausing a crawl job must STICK — the reaper/refresh scheduler must not auto-recreate
it (work.crawl_paused). Resume/retry/check-updates/re-hook clear the pause."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.db import SessionLocal, init_db
from app.ingestion.scheduler import reap_stalled_jobs
from app.main import app
from app.models import (
    Chapter,
    CrawlJob,
    LibraryItem,
    Source,
    User,
    UserSession,
    Work,
)


@pytest.fixture
def admin():
    init_db()
    db = SessionLocal()
    for m in (CrawlJob, Chapter, LibraryItem, Work, UserSession, User):
        db.execute(delete(m))
    db.commit()
    db.close()
    c = TestClient(app)
    c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
    return c


def _work_with_pending(db, paused=False) -> int:
    src = db.scalar(select(Source).where(Source.key == "generic_feed"))
    if src is None:
        src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                     tos_permitted=True)
        db.add(src)
        db.commit()
    w = Work(source_id=src.id, source_work_ref="r", title="W", hooked=True,
             status="ongoing", crawl_paused=paused)
    db.add(w)
    db.commit()
    db.refresh(w)
    for i in range(1, 4):
        db.add(Chapter(work_id=w.id, index=i, source_chapter_ref=f"c{i}",
                       title=f"Ch {i}", fetch_status="pending"))
    db.commit()
    return w.id


def _open_jobs(db, work_id) -> int:
    return db.scalar(
        select(func.count(CrawlJob.id)).where(
            CrawlJob.work_id == work_id,
            CrawlJob.status.in_(["scheduled", "running", "paused"]),
        )
    ) or 0


def test_reaper_revives_unpaused_work(admin):
    """Control: a hooked work with pending chapters and no job IS revived by the reaper."""
    db = SessionLocal()
    wid = _work_with_pending(db, paused=False)
    db.close()
    reap_stalled_jobs()
    db = SessionLocal()
    assert _open_jobs(db, wid) >= 1  # the reaper reopened a backfill (this is the resurrection)
    db.close()


def test_deleted_job_stays_gone(admin):
    """Deleting a job pauses the work so the reaper does NOT recreate it."""
    db = SessionLocal()
    wid = _work_with_pending(db, paused=False)
    job = CrawlJob(work_id=wid, kind="backfill", status="scheduled", cursor={})
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    assert admin.request("DELETE", f"/api/jobs/{jid}").json()["deleted"] == jid
    db = SessionLocal()
    assert db.get(Work, wid).crawl_paused is True
    assert _open_jobs(db, wid) == 0
    db.close()

    reap_stalled_jobs()  # the bug: this used to recreate the job
    db = SessionLocal()
    assert _open_jobs(db, wid) == 0  # stays gone
    db.close()


def test_retry_resumes_paused_work(admin):
    """Renewing (retry) a paused work clears crawl_paused so it crawls again."""
    db = SessionLocal()
    wid = _work_with_pending(db, paused=True)
    job = CrawlJob(work_id=wid, kind="backfill", status="failed", cursor={})
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    admin.post(f"/api/jobs/{jid}/retry")
    db = SessionLocal()
    assert db.get(Work, wid).crawl_paused is False
    db.close()
    # Now the reaper is free to keep it going again.
    reap_stalled_jobs()
    db = SessionLocal()
    assert _open_jobs(db, wid) >= 1
    db.close()
