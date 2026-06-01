"""CrawlScheduler (Stage 7).

An AsyncIOScheduler ticks periodically and drains due CrawlJobs *slowly*, fetching
only `chapters_per_tick` chapters per job per tick, within each source's rate budget.
Jobs persist a `cursor` so a backfill resumes after a restart, and content is
checksum-deduped so re-runs are idempotent.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import Chapter, CrawlJob, Work
from .base import ChapterRef
from .engine import adapter_for, store_chapter_content

log = logging.getLogger("shelf.scheduler")
settings = get_settings()
_scheduler: AsyncIOScheduler | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _append_next_chapter(
    db: Session, work: Work, current: Chapter, next_ref: str, next_title: str | None
) -> None:
    """Create a pending Chapter for a next-link, unless we've already seen that ref
    (dedupe prevents loops in sequential crawling)."""
    from ..models import Chapter as ChapterModel

    next_ref = (next_ref or "").strip()
    if not next_ref or next_ref == current.source_chapter_ref:
        return
    exists = db.scalar(
        select(ChapterModel.id).where(
            ChapterModel.work_id == work.id, ChapterModel.source_chapter_ref == next_ref
        )
    )
    if exists:
        return
    next_index = (db.scalar(
        select(func.max(ChapterModel.index)).where(ChapterModel.work_id == work.id)
    ) or current.index) + 1
    db.add(
        ChapterModel(
            work_id=work.id,
            source_chapter_ref=next_ref,
            index=next_index,
            title=next_title or f"Chapter {next_index}",
            fetch_status="pending",
        )
    )
    work.total_chapters_known = max(work.total_chapters_known, next_index)
    # Keep the advertised total from lagging behind what we've actually discovered.
    if work.total_chapters_expected and next_index > work.total_chapters_expected:
        work.total_chapters_expected = next_index


async def _process_job(db: Session, job: CrawlJob) -> None:
    work = db.get(Work, job.work_id)
    if work is None or work.source is None:
        job.status = "failed"
        job.last_error = "work or source missing"
        db.commit()
        return

    try:
        adapter = adapter_for(work.source)
    except Exception as exc:  # disabled/non-permitted
        job.status = "failed"
        job.last_error = str(exc)
        db.commit()
        return

    job.status = "running"
    if job.started_at is None:
        job.started_at = _utcnow()
    db.commit()

    # Refresh jobs re-check the source for new content + refreshed metadata.
    if job.kind == "refresh":
        try:
            from . import tracker

            added, changed = await tracker.discover_updates(db, work, adapter)
            now = _utcnow()
            work.last_checked_at = now
            if added or changed:
                work.last_update_at = now
            db.commit()
            if added or changed:
                log.info("refresh work=%s found new=%s meta_changed=%s",
                         work.id, added, changed)
        except Exception as exc:
            # Discard any partially-applied metadata/chapter mutations before recording
            # the error, so a failed refresh never half-commits.
            db.rollback()
            job.last_error = f"refresh failed: {exc}"
            db.commit()

    # Fetch up to N pending chapters this tick.
    pending = db.scalars(
        select(Chapter)
        .where(Chapter.work_id == work.id, Chapter.fetch_status == "pending")
        .order_by(Chapter.index)
        .limit(settings.chapters_per_tick)
    ).all()

    if not pending:
        job.status = "done"
        job.finished_at = _utcnow()
        fetched = db.scalar(
            select(func.count(Chapter.id)).where(
                Chapter.work_id == work.id, Chapter.fetch_status == "fetched"
            )
        ) or 0
        # Only let the gathered count BECOME the authoritative total when it matches (or
        # exceeds) what the source advertised — otherwise a prematurely-stopped crawl
        # would hide that chapters are missing. Diagnose + flag completeness either way.
        if not work.total_chapters_expected or fetched >= work.total_chapters_expected:
            work.total_chapters_expected = fetched
        db.commit()
        try:
            from .diagnose import apply_health, completeness

            apply_health(db, work, completeness(db, work))
        except Exception:  # noqa: BLE001 — health is best-effort
            log.exception("completeness check failed for work %s", work.id)
            db.rollback()
        log.info("job %s done (work %s, health=%s)", job.id, work.id, work.health)
        return

    for ch in pending:
        try:
            raw = await adapter.fetch_chapter(
                ChapterRef(
                    source_chapter_ref=ch.source_chapter_ref or str(ch.index),
                    index=ch.index,
                    title=ch.title,
                )
            )
            store_chapter_content(db, ch, raw)
            # Sequential crawling: enqueue the next chapter discovered on this page.
            if raw.next_ref:
                _append_next_chapter(db, work, ch, raw.next_ref, raw.next_title)
            job.cursor = {"next_index": ch.index + 1}
            job.attempts = 0
            job.last_error = None
            db.commit()
            log.info("fetched work=%s chapter=%s", work.id, ch.index)
        except Exception as exc:
            job.attempts += 1
            job.last_error = f"chapter {ch.index}: {exc}"
            if job.attempts >= 5:
                ch.fetch_status = "failed"
                job.attempts = 0
            db.commit()
            log.warning("fetch failed work=%s chapter=%s: %s", work.id, ch.index, exc)

    # Reschedule another tick shortly; per-source rate limits enforce real slowness.
    job.status = "scheduled"
    job.scheduled_for = _utcnow() + timedelta(seconds=settings.scheduler_tick_seconds)
    db.commit()


async def tick() -> None:
    db = SessionLocal()
    try:
        now = _utcnow()
        jobs = db.scalars(
            select(CrawlJob)
            .where(CrawlJob.status.in_(["scheduled", "running"]))
            .order_by(CrawlJob.scheduled_for)
        ).all()
        due = [j for j in jobs if (_aware(j.scheduled_for) or now) <= now]
        for job in due[: settings.global_max_concurrency]:
            await _process_job(db, job)
    except Exception:  # never let a tick kill the scheduler
        log.exception("scheduler tick failed")
    finally:
        db.close()


def schedule_refresh_jobs() -> None:
    """Enqueue periodic refresh jobs for trackable hooked works that have none open."""
    from .tracker import is_trackable

    db = SessionLocal()
    try:
        works = db.scalars(select(Work).where(Work.hooked.is_(True))).all()
        for work in works:
            # Only serialized/remote works can gain content; skip static books.
            if not is_trackable(work) or work.status != "ongoing":
                continue
            open_job = db.scalar(
                select(CrawlJob).where(
                    CrawlJob.work_id == work.id,
                    CrawlJob.status.in_(["scheduled", "running", "paused"]),
                )
            )
            if open_job is None:
                db.add(
                    CrawlJob(
                        work_id=work.id,
                        kind="refresh",
                        status="scheduled",
                        scheduled_for=_utcnow(),
                        cursor={},
                    )
                )
        db.commit()
    finally:
        db.close()


def _folder_rescan() -> None:
    from .watcher import rescan_all

    try:
        rescan_all()
    except Exception:
        log.exception("folder rescan failed")


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(tick, "interval", seconds=settings.scheduler_tick_seconds, id="crawl_tick",
                  max_instances=1, coalesce=True)
    sched.add_job(schedule_refresh_jobs, "interval", hours=6, id="refresh_enqueue",
                  max_instances=1, coalesce=True)
    # URL-index auto-crawl: drains pending indexed pages politely.
    from .indexer import index_tick

    sched.add_job(index_tick, "interval", seconds=settings.scheduler_tick_seconds,
                  id="index_tick", max_instances=1, coalesce=True)
    # Watched-folder safety rescan (backstops any filesystem events watchdog missed).
    sched.add_job(_folder_rescan, "interval", minutes=10, id="folder_rescan",
                  max_instances=1, coalesce=True)
    sched.start()
    _scheduler = sched
    log.info("crawl scheduler started (tick=%ss)", settings.scheduler_tick_seconds)
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
