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
from .fetcher import DailyBudgetExceeded

log = logging.getLogger("shelf.scheduler")
settings = get_settings()
_scheduler: AsyncIOScheduler | None = None

# When a source's shared daily budget is exhausted, retry this title later (the
# source's rolling 24h window frees up); keep its chapters pending meanwhile.
_BUDGET_RETRY_SECONDS = 3600


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _in_window(work: Work, now: datetime) -> bool:
    """Is `now` (UTC) within the title's allowed crawl hours? (no window = always)."""
    s, e = work.crawl_window_start, work.crawl_window_end
    if s is None or e is None or s == e:
        return True
    h = now.hour
    return (s <= h < e) if s < e else (h >= s or h < e)


def _seconds_until_window(work: Work, now: datetime) -> float:
    """Seconds until the title's crawl window next opens (0 if open now / no window)."""
    if _in_window(work, now):
        return 0.0
    s = work.crawl_window_start
    hours = (s - now.hour) % 24 or 24
    target = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=hours)
    return max(1.0, (target - now).total_seconds())


def _seconds_until_tomorrow(now: datetime) -> float:
    """Seconds until the next UTC midnight (when the per-title daily counter resets)."""
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds()


def _reset_daily_counter(work: Work, now: datetime) -> None:
    today = now.date().isoformat()
    if work.crawl_day != today:
        work.crawl_day = today
        work.crawl_count_today = 0


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

    # --- Per-title crawl policy: time window + daily request cap -------------
    now = _utcnow()
    _reset_daily_counter(work, now)
    wait = _seconds_until_window(work, now)
    if wait > 0:
        job.status = "scheduled"
        job.scheduled_for = now + timedelta(seconds=wait)
        job.last_error = "outside the title's allowed crawl hours; resuming later"
        db.commit()
        return
    if work.crawl_daily_limit and work.crawl_count_today >= work.crawl_daily_limit:
        job.status = "scheduled"
        job.scheduled_for = now + timedelta(seconds=_seconds_until_tomorrow(now) + 5)
        job.last_error = (
            f"daily limit of {work.crawl_daily_limit} requests reached; resuming tomorrow"
        )
        db.commit()
        return

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

    # Batch size: one request per run when a per-title interval is set (so 'speed' is
    # honoured), else the global per-tick batch — capped by remaining daily budget.
    per_request = bool(work.crawl_interval_s and work.crawl_interval_s > 0)
    batch = 1 if per_request else settings.chapters_per_tick
    if work.crawl_daily_limit:
        batch = min(batch, work.crawl_daily_limit - work.crawl_count_today)
    batch = max(1, batch)

    pending = db.scalars(
        select(Chapter)
        .where(Chapter.work_id == work.id, Chapter.fetch_status == "pending")
        .order_by(Chapter.index)
        .limit(batch)
    ).all()

    if not pending:
        _finalize_done(db, job, work)
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
            work.crawl_count_today += 1
            job.cursor = {"next_index": ch.index + 1}
            job.attempts = 0
            job.last_error = None
            db.commit()
            log.info("fetched work=%s chapter=%s", work.id, ch.index)
        except DailyBudgetExceeded as exc:
            # The source's shared daily budget is spent. Leave the remaining chapters
            # PENDING (do NOT fail them or collapse the total) and retry later — this is
            # the fix for "exceeding the daily cap hides the outstanding chapters".
            db.rollback()
            job.status = "scheduled"
            job.scheduled_for = _utcnow() + timedelta(seconds=_BUDGET_RETRY_SECONDS)
            job.last_error = f"source daily budget reached; resuming later ({exc})"
            db.commit()
            return
        except Exception as exc:
            job.attempts += 1
            job.last_error = f"chapter {ch.index}: {exc}"
            if job.attempts >= 5:
                ch.fetch_status = "failed"
                job.attempts = 0
            db.commit()
            log.warning("fetch failed work=%s chapter=%s: %s", work.id, ch.index, exc)
        # Stop once the title's own daily cap is reached mid-batch.
        if work.crawl_daily_limit and work.crawl_count_today >= work.crawl_daily_limit:
            break

    # Reschedule; honour the per-title interval as a minimum spacing between runs.
    interval = work.crawl_interval_s or settings.scheduler_tick_seconds
    job.status = "scheduled"
    job.scheduled_for = _utcnow() + timedelta(
        seconds=max(settings.scheduler_tick_seconds, interval)
    )
    db.commit()


def _finalize_done(db: Session, job: CrawlJob, work: Work) -> None:
    """Mark a backfill done and reconcile the expected total WITHOUT hiding outstanding
    chapters: only adopt the gathered count as the authoritative total when the crawl is
    genuinely complete (nothing failed); otherwise never report a total below the number
    of chapters we already know about (fetched + failed)."""
    job.status = "done"
    job.finished_at = _utcnow()
    counts = dict(
        db.execute(
            select(Chapter.fetch_status, func.count(Chapter.id))
            .where(Chapter.work_id == work.id)
            .group_by(Chapter.fetch_status)
        ).all()
    )
    fetched = counts.get("fetched", 0)
    failed = counts.get("failed", 0)
    total_rows = sum(counts.values())
    expected = work.total_chapters_expected
    if failed == 0 and (not expected or fetched >= expected):
        work.total_chapters_expected = fetched
    else:
        work.total_chapters_expected = max(work.total_chapters_expected or 0, total_rows)
    db.commit()
    try:
        from .diagnose import apply_health, completeness

        apply_health(db, work, completeness(db, work))
    except Exception:  # noqa: BLE001 — health is best-effort
        log.exception("completeness check failed for work %s", work.id)
        db.rollback()
    log.info("job %s done (work %s, health=%s)", job.id, work.id, work.health)


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
    # Integration sync: pull Readarr/Kapowarr libraries into the catalog periodically.
    from ..integrations.sync import sync_all

    sched.add_job(sync_all, "interval", hours=6, id="integration_sync",
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
