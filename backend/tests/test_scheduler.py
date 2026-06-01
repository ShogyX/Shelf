"""Per-title crawl policy enforcement + the daily-cap count-collapse bug fix."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import scheduler
from app.ingestion.base import RawChapter
from app.ingestion.fetcher import DailyBudgetExceeded
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


class FakeAdapter:
    key = "generic_feed"

    def __init__(self, *, raise_budget=False):
        self.raise_budget = raise_budget
        self.calls = 0

    async def fetch_chapter(self, ref):
        self.calls += 1
        if self.raise_budget:
            raise DailyBudgetExceeded("daily budget exhausted")
        return RawChapter(title=ref.title, body="<p>body text here, long enough.</p>", fmt="html")


def _setup(db, *, expected=None, chapters=2, **work_kw) -> tuple[Work, CrawlJob]:
    src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                 tos_permitted=True)
    db.add(src)
    db.commit()
    w = Work(source_id=src.id, source_work_ref="https://s/n", title="X", hooked=True,
             status="ongoing", total_chapters_known=chapters,
             total_chapters_expected=expected, **work_kw)
    db.add(w)
    db.commit()
    db.refresh(w)
    for i in range(1, chapters + 1):
        db.add(Chapter(work_id=w.id, index=i, source_chapter_ref=f"https://s/n/c/{i}",
                       title=f"Chapter {i}", fetch_status="pending"))
    job = CrawlJob(work_id=w.id, kind="backfill", status="scheduled", cursor={})
    db.add(job)
    db.commit()
    db.refresh(job)
    return w, job


@pytest.mark.asyncio
async def test_daily_budget_exceeded_keeps_chapters_pending_and_total(monkeypatch):
    """The reported bug: hitting the cap must NOT fail chapters or collapse the total."""
    db = SessionLocal()
    w, job = _setup(db, expected=None, chapters=3)  # no advertised total
    adapter = FakeAdapter(raise_budget=True)
    monkeypatch.setattr("app.ingestion.scheduler.adapter_for", lambda src: adapter)

    await scheduler._process_job(db, job)

    statuses = [c.fetch_status for c in db.scalars(
        select(Chapter).where(Chapter.work_id == w.id).order_by(Chapter.index)).all()]
    assert statuses == ["pending", "pending", "pending"]   # nothing failed
    db.refresh(w)
    assert w.total_chapters_expected is None               # NOT collapsed to fetched(0)
    db.refresh(job)
    assert job.status == "scheduled"                        # will retry later
    assert "budget" in (job.last_error or "")
    db.close()


@pytest.mark.asyncio
async def test_outside_window_reschedules_without_fetching(monkeypatch):
    db = SessionLocal()
    now = datetime.now(UTC)
    # A 1-hour window two hours from now → current hour is outside it.
    start = (now.hour + 2) % 24
    w, job = _setup(db, crawl_window_start=start, crawl_window_end=(start + 1) % 24)
    adapter = FakeAdapter()
    monkeypatch.setattr("app.ingestion.scheduler.adapter_for", lambda src: adapter)

    await scheduler._process_job(db, job)

    assert adapter.calls == 0
    pend = db.scalars(select(Chapter).where(Chapter.work_id == w.id)).all()
    assert all(c.fetch_status == "pending" for c in pend)
    db.refresh(job)
    assert job.status == "scheduled" and "hours" in (job.last_error or "")
    db.close()


@pytest.mark.asyncio
async def test_daily_cap_reached_reschedules_for_tomorrow(monkeypatch):
    db = SessionLocal()
    today = datetime.now(UTC).date().isoformat()
    w, job = _setup(db, crawl_daily_limit=2, crawl_count_today=2, crawl_day=today)
    adapter = FakeAdapter()
    monkeypatch.setattr("app.ingestion.scheduler.adapter_for", lambda src: adapter)

    await scheduler._process_job(db, job)

    assert adapter.calls == 0
    db.refresh(job)
    assert job.status == "scheduled" and "daily limit" in (job.last_error or "")
    db.close()


@pytest.mark.asyncio
async def test_interval_fetches_one_per_run_and_counts(monkeypatch):
    db = SessionLocal()
    w, job = _setup(db, chapters=3, crawl_interval_s=30)
    adapter = FakeAdapter()
    monkeypatch.setattr("app.ingestion.scheduler.adapter_for", lambda src: adapter)

    await scheduler._process_job(db, job)

    assert adapter.calls == 1                       # one request this run (interval set)
    db.refresh(w)
    assert w.crawl_count_today == 1
    db.close()


def test_finalize_done_keeps_outstanding_visible():
    db = SessionLocal()
    src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                 tos_permitted=True)
    db.add(src)
    db.commit()
    w = Work(source_id=src.id, title="X", hooked=True, status="ongoing")
    db.add(w)
    db.commit()
    db.refresh(w)
    db.add_all([
        Chapter(work_id=w.id, index=1, fetch_status="fetched"),
        Chapter(work_id=w.id, index=2, fetch_status="fetched"),
        Chapter(work_id=w.id, index=3, fetch_status="failed"),
    ])
    job = CrawlJob(work_id=w.id, kind="backfill", status="running")
    db.add(job)
    db.commit()

    scheduler._finalize_done(db, job, w)

    db.refresh(w)
    # Total must NOT collapse to the 2 fetched — the failed chapter stays visible.
    assert w.total_chapters_expected == 3
    assert w.health == "incomplete"
    db.close()


def test_set_crawl_policy_endpoint():
    from app.routers.works import set_crawl_policy
    from app.schemas import CrawlPolicyIn

    db = SessionLocal()
    w = Work(title="X", hooked=True)
    db.add(w)
    db.commit()
    db.refresh(w)
    out = set_crawl_policy(
        w.id,
        CrawlPolicyIn(crawl_interval_s=20, crawl_daily_limit=100,
                      crawl_window_start=1, crawl_window_end=6),
        db,
    )
    assert out.crawl_interval_s == 20 and out.crawl_daily_limit == 100
    assert out.crawl_window_start == 1 and out.crawl_window_end == 6
    db.close()
