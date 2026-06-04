"""CrawlScheduler (Stage 7).

An AsyncIOScheduler ticks periodically and drains due CrawlJobs *slowly*, fetching
only `chapters_per_tick` chapters per job per tick, within each source's rate budget.
Jobs persist a `cursor` so a backfill resumes after a restart, and content is
checksum-deduped so re-runs are idempotent.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import Chapter, CrawlJob, Work
from .base import ChapterRef, PermanentFetchError
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
    # Stamp each run so the reaper's stuck-detection measures *liveness* (a run that's been
    # executing too long), not the job's first-ever start — otherwise every long backfill
    # looks "stuck" and the reaper would keep yanking it forward, defeating per-title pacing.
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
    if per_request:
        batch = 1
    else:
        from . import crawl_tuning
        batch = crawl_tuning.get_tuning(db)["chapters_per_tick"]
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
            # Sanitizing chapter HTML is CPU-heavy (BeautifulSoup); run it off the event loop
            # so concurrent API requests stay responsive while the backfill churns.
            from .engine import DEAD_END, STORED
            result = await asyncio.to_thread(
                store_chapter_content, db, ch, raw, detect_dead_end=True
            )
            work.crawl_count_today += 1
            if result == DEAD_END:
                # The synthesized/next page was a placeholder or a loop — the serial has no more
                # chapters right now. Stop chaining and finalize so the work doesn't grow forever.
                job.cursor = {"next_index": ch.index}
                job.attempts = 0
                job.last_error = None
                # Retract the phantom ceiling that appending this dead-end may have inflated.
                if work.total_chapters_expected and ch.index >= work.total_chapters_expected:
                    fetched_max = db.scalar(
                        select(func.max(Chapter.index)).where(
                            Chapter.work_id == work.id, Chapter.fetch_status == "fetched")
                    ) or 0
                    work.total_chapters_expected = fetched_max or None
                db.commit()
                log.info("end-of-content work=%s at chapter=%s (placeholder/duplicate)",
                         work.id, ch.index)
                _finalize_done(db, job, work)
                return
            # Sequential crawling: enqueue the next chapter discovered on this page (real content
            # only — never chain off a deduped re-fetch's stale next link or a dead-end).
            if result == STORED and raw.next_ref:
                _append_next_chapter(db, work, ch, raw.next_ref, raw.next_title)
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
        except PermanentFetchError as exc:
            # Members-only / paywalled with no credentials: mark 'unavailable' (a terminal
            # state the reaper does NOT revive) so it never thrashes the source every hour.
            db.rollback()
            ch.fetch_status = "unavailable"
            job.attempts = 0
            job.last_error = f"chapter {ch.index}: {exc}"
            db.commit()
            log.info("chapter unavailable work=%s chapter=%s: %s", work.id, ch.index, exc)
            continue
        except Exception as exc:
            # Discard any half-applied content flush before recording the error on the job,
            # so we never commit partially-stored chapter state.
            db.rollback()
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


# A 'running' job untouched for this long is presumed abandoned (e.g. a crash/restart
# killed it mid-fetch) and is re-armed by the reaper.
_STUCK_RUNNING_S = 600
# Don't retry a work's failed chapters more often than this (anti-thrash backstop).
_FAILED_RETRY_S = 3600
# Serialize reaper runs: the timer reaper (threadpool) and a manual POST /jobs/reap can
# otherwise run concurrently and race on job creation.
_reaper_lock = threading.Lock()


def _prune_superseded_jobs(db: Session) -> int:
    """Tidy the Jobs list: a terminal (done/failed) job is just history once a newer job
    exists for the same work. Delete terminal jobs that an active (scheduled/running/paused)
    job supersedes, and keep only the most-recent terminal job per work otherwise (so
    periodic refresh runs don't pile up). Gathered chapters are untouched — this only
    removes task records."""
    open_work_ids = {
        wid for (wid,) in db.execute(
            select(CrawlJob.work_id).where(
                CrawlJob.status.in_(["scheduled", "running", "paused"])
            ).distinct()
        ).all()
    }
    terminal = db.scalars(
        select(CrawlJob)
        .where(CrawlJob.status.in_(["done", "failed"]))
        .order_by(CrawlJob.created_at.desc())
    ).all()
    pruned = 0
    kept_per_work: set[int] = set()
    for job in terminal:
        superseded = job.work_id in open_work_ids
        # Keep the newest terminal job per work when there's no active job; drop the rest.
        redundant = job.work_id in kept_per_work
        if superseded or redundant:
            db.delete(job)
            pruned += 1
        else:
            kept_per_work.add(job.work_id)
    return pruned


def _outstanding(db: Session, work_id: int) -> tuple[int, int]:
    """(pending, failed) chapter counts for a work."""
    rows = dict(
        db.execute(
            select(Chapter.fetch_status, func.count(Chapter.id))
            .where(Chapter.work_id == work_id,
                   Chapter.fetch_status.in_(["pending", "failed"]))
            .group_by(Chapter.fetch_status)
        ).all()
    )
    return rows.get("pending", 0), rows.get("failed", 0)


def reap_stalled_jobs() -> int:
    """Revive crawl jobs that died or stalled, as long as their stop condition is NOT yet
    met (the work still has chapters to gather). Handles three failure modes:

      1. A job stuck in 'running' after a crash/restart → re-arm it.
      2. A job parked in the future by a *per-title* limit (crawl window / daily cap) that
         has since cleared → pull it forward to run now. (Source-level daily-budget parks
         are left alone so we never hammer a source whose shared budget is still spent.)
      3. A work that still has pending/failed chapters but NO open job (its job was marked
         done/failed prematurely) → re-queue failed chapters and reopen a backfill job.

    Returns the number of jobs revived/created. Never restarts a genuinely finished work.
    """
    if not _reaper_lock.acquire(blocking=False):
        return 0  # another reaper run (timer or manual) is already in progress
    db = SessionLocal()
    revived = 0
    try:
        now = _utcnow()

        # (1) + (2): re-arm open jobs that are stuck or parked-but-unblocked.
        open_jobs = db.scalars(
            select(CrawlJob).where(CrawlJob.status.in_(["scheduled", "running", "paused"]))
        ).all()
        for job in open_jobs:
            work = db.get(Work, job.work_id)
            if work is None:
                continue
            if work.crawl_paused:
                continue  # operator paused/deleted this work's crawl — don't re-arm
            pending, _failed = _outstanding(db, work.id)
            if pending <= 0:
                continue  # nothing to do → not stalled
            sched = _aware(job.scheduled_for) or now
            if job.status == "running" and job.started_at and (
                now - (_aware(job.started_at) or now)
            ).total_seconds() > _STUCK_RUNNING_S:
                job.status, job.scheduled_for = "scheduled", now
                revived += 1
                continue
            if job.status in ("scheduled", "paused") and sched > now:
                if "source daily budget" in (job.last_error or "").lower():
                    continue  # let the shared source budget recover on its own
                _reset_daily_counter(work, now)
                blocked = _seconds_until_window(work, now) > 0 or bool(
                    work.crawl_daily_limit and work.crawl_count_today >= work.crawl_daily_limit
                )
                if not blocked:
                    job.status, job.scheduled_for = "scheduled", now
                    revived += 1

        # (3): works with outstanding chapters but no open job → reopen.
        works = db.scalars(select(Work).where(Work.hooked.is_(True))).all()
        for work in works:
            if work.crawl_paused:
                continue  # deleted/paused job stays gone until the operator resumes
            has_open = db.scalar(
                select(CrawlJob.id).where(
                    CrawlJob.work_id == work.id,
                    CrawlJob.status.in_(["scheduled", "running", "paused"]),
                )
            )
            if has_open:
                continue
            pending, failed = _outstanding(db, work.id)
            if failed > 0:
                # Retry failed chapters when reviving — but only if the last attempt wasn't
                # recent, so a permanently-broken chapter can't thrash the source every cycle
                # (fail 5×→failed→requeue→fail 5×→…). At most one retry per work per hour.
                last_finished = _aware(
                    db.scalar(
                        select(func.max(CrawlJob.finished_at)).where(CrawlJob.work_id == work.id)
                    )
                )
                stale = last_finished is None or (
                    now - last_finished
                ).total_seconds() >= _FAILED_RETRY_S
                if stale:
                    db.execute(
                        update(Chapter)
                        .where(Chapter.work_id == work.id, Chapter.fetch_status == "failed")
                        .values(fetch_status="pending")
                    )
                    pending += failed
            if pending > 0:
                # Re-check open jobs right before inserting (another reaper run / hook may
                # have created one concurrently) to avoid a duplicate backfill.
                if db.scalar(
                    select(CrawlJob.id).where(
                        CrawlJob.work_id == work.id,
                        CrawlJob.status.in_(["scheduled", "running", "paused"]),
                    )
                ):
                    continue
                db.add(CrawlJob(work_id=work.id, kind="backfill", status="scheduled",
                                scheduled_for=now, cursor={"next_index": 1}))
                revived += 1

        db.flush()  # so newly-created jobs count as "open" in the prune below
        pruned = _prune_superseded_jobs(db)
        db.commit()
        if revived or pruned:
            log.info("reaper revived %d job(s), pruned %d superseded", revived, pruned)
    except Exception:
        log.exception("job reaper failed")
        db.rollback()
    finally:
        db.close()
        _reaper_lock.release()
    return revived


async def tick() -> None:
    """Run due backfill jobs CONCURRENTLY and INDEPENDENTLY — one coroutine per job, each in its
    own DB session. Jobs for different sources are paced by their own per-source budget, so a slow
    job never blocks the others (and the index crawl runs independently of all of them). The gather
    is awaited so the next tick can't re-pick a job that's still running."""
    from . import crawl_tuning

    db = SessionLocal()
    job_ids: list[int] = []
    try:
        # Backfill's OWN per-tick budget (independent of the index crawl): how many jobs run
        # concurrently this tick.
        backfill_budget = crawl_tuning.get_tuning(db)["parallel_fetches"]
        now = _utcnow()
        jobs = db.scalars(
            select(CrawlJob)
            .where(CrawlJob.status.in_(["scheduled", "running"]))
            .order_by(CrawlJob.scheduled_for)
        ).all()
        # Don't run two jobs for the SAME work in one tick (a duplicate job — e.g. from a
        # reaper/hook race — would otherwise double-fetch the same pending chapters).
        seen_works: set[int] = set()
        for job in jobs:
            if (_aware(job.scheduled_for) or now) > now:
                continue
            if job.work_id in seen_works:
                continue
            seen_works.add(job.work_id)
            job_ids.append(job.id)
            if len(job_ids) >= backfill_budget:
                break
    except Exception:  # never let a tick kill the scheduler
        log.exception("scheduler tick orchestration failed")
        return
    finally:
        db.close()

    if job_ids:
        await asyncio.gather(*(_run_job(jid) for jid in job_ids), return_exceptions=True)


async def _run_job(job_id: int) -> None:
    """Process one backfill job in its own session, isolated from the concurrent jobs."""
    db = SessionLocal()
    try:
        job = db.get(CrawlJob, job_id)
        if job is None or job.status not in ("scheduled", "running"):
            return
        await _process_job(db, job)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        log.exception("backfill job failed job=%s", job_id)
    finally:
        db.close()


def _auto_update_work_ids(db: Session) -> set[int]:
    """Work ids that at least one user has placed on an ``auto_update`` bookshelf.

    The crawl is shared (one refresh serves every member), so the per-user/per-shelf
    ``auto_update`` toggle is OR-ed across all members: a work is auto-refreshed when ANY
    member opted in. A work on no shelf — or only on auto-update-off shelves — is not
    auto-refreshed (manual 'check for updates' still works)."""
    from ..models import Bookshelf, BookshelfItem

    return set(
        db.scalars(
            select(BookshelfItem.work_id)
            .join(Bookshelf, Bookshelf.id == BookshelfItem.shelf_id)
            .where(Bookshelf.auto_update.is_(True))
        ).all()
    )


def schedule_refresh_jobs() -> None:
    """Enqueue periodic refresh jobs for trackable hooked works that some member opted into
    auto-updating (any member with the work on an ``auto_update`` shelf) and have none open."""
    from .tracker import is_trackable

    db = SessionLocal()
    try:
        auto_ids = _auto_update_work_ids(db)
        works = db.scalars(select(Work).where(Work.hooked.is_(True))).all()
        for work in works:
            # Only serialized/remote works can gain content; skip static books.
            if not is_trackable(work) or work.status != "ongoing":
                continue
            if work.crawl_paused:
                continue  # operator paused this work's crawl — don't re-enqueue refreshes
            # 'Any member opted in': skip works no one placed on an auto_update shelf.
            if work.id not in auto_ids:
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


_INTEGRITY_BATCH = 5  # works scanned per integrity tick (rotates by least-recently-checked)


def integrity_tick() -> None:
    """Actively scan a few hooked works for chapter gaps — true index holes AND skipped
    chapter *numbers* (a contiguous-index sequential crawl that jumped a chapter) — and
    repair them. Rotates through the library by least-recently-checked so the whole
    library is covered over time without scanning everything at once."""
    from . import diagnose

    db = SessionLocal()
    try:
        works = db.scalars(
            select(Work)
            .where(Work.hooked.is_(True))
            .order_by(Work.health_checked_at.is_(None).desc(), Work.health_checked_at.asc())
            .limit(_INTEGRITY_BATCH)
        ).all()
        for work in works:
            try:
                rep = diagnose.completeness(db, work)
                # Only auto-repair real structural gaps/skips here (failed chapters and
                # advertised-vs-fetched are handled, rate-limited, by the reaper) — and
                # never while an active crawl is still draining pending chapters.
                fixable = (rep["gaps"] or rep["chapter_gaps"]) and not (
                    rep["has_open_job"] and rep["pending"]
                )
                if fixable:
                    diagnose.repair(db, work)
                else:
                    diagnose.apply_health(db, work, rep)
            except Exception:
                log.exception("integrity check failed for work %s", work.id)
                db.rollback()
    except Exception:
        log.exception("integrity tick failed")
    finally:
        db.close()


def _folder_rescan() -> None:
    from .watcher import rescan_all

    try:
        rescan_all()
    except Exception:
        log.exception("folder rescan failed")


def _cache_covers_batch() -> int:
    """Download a batch of remote cover images to permanent local storage and rewrite the
    cover_url to the local path — so the library/catalog never re-fetches them from remote.
    Returns how many were processed. Sync (runs off the event loop).

    CRITICAL: the image downloads (slow network I/O) run with NO open DB transaction. Holding a
    transaction across 40 downloads kept a read snapshot alive for tens of seconds, which both
    starved the WAL checkpoint (the -wal file ballooned to GBs) and collided with the crawl's
    writers on commit ('database is locked'). So: read the work-list, RELEASE the snapshot,
    download, then apply each result in its own short write transaction."""
    from .. import imagecache
    from ..models import CatalogWork, IndexedPage

    db = SessionLocal()
    done = 0
    try:
        for model in (Work, CatalogWork, IndexedPage):
            rows = db.execute(
                select(model.id, model.cover_url).where(model.cover_url.like("http%")).limit(40)
            ).all()
            db.commit()  # release the read snapshot before the slow downloads
            for rid, url in rows:
                res = imagecache.cache_image(url)  # network — no DB transaction held here
                if res and res != imagecache.PERMANENT_FAIL:
                    new = res
                elif res == imagecache.PERMANENT_FAIL:
                    new = None  # give up → falls back to a generated cover
                else:
                    continue  # None (transient) → leave the remote URL for a later retry
                # Short, isolated write so a writer collision affects one row, not the batch.
                db.execute(update(model).where(model.id == rid).values(cover_url=new))
                db.commit()
                done += 1
    except Exception:
        db.rollback()
        log.exception("cover cache tick failed")
    finally:
        db.close()
    return done


async def cache_images_tick() -> None:
    """Periodically localize remote cover images (covers discovered while indexing AND on
    hooked works). Image downloads are blocking → run off the event loop."""
    try:
        await asyncio.to_thread(_cache_covers_batch)
    except Exception:
        log.exception("cache_images_tick failed")


async def wal_checkpoint_tick() -> None:
    """Keep the SQLite WAL bounded. Under the continuous crawl, passive autocheckpoint is starved
    by always-active readers and the -wal file grows without bound (seen at ~6 GB), which
    collapses write throughput into 'database is locked'. A periodic TRUNCATE checkpoint reclaims
    it whenever a clean window appears. Blocking PRAGMA → run off the event loop."""
    from ..db import checkpoint_wal
    try:
        await asyncio.to_thread(checkpoint_wal)
    except Exception:
        log.exception("wal_checkpoint_tick failed")


async def queued_hook_tick() -> None:
    """Auto-hook queued titles (related series + Goodreads wishlist) once they appear in the
    index. Cheap when the queue is empty (single indexed-status query)."""
    from ..db import SessionLocal
    from ..integrations import metadata_sync
    from ..models import QueuedHook

    db = SessionLocal()
    try:
        if not db.scalar(select(QueuedHook.id).where(QueuedHook.status == "pending").limit(1)):
            return
        await metadata_sync.process_queued_hooks(db)
    except Exception:
        log.exception("queued_hook_tick failed")
        db.rollback()
    finally:
        db.close()


def auto_kindle_tick() -> None:
    """Auto-send newly fetched chapters to the Kindle of every member who has a work on an
    ``auto_kindle`` shelf.

    Per (member, work) we track the highest chapter index already sent
    (``LibraryItem.auto_kindle_through``). The first pass baselines it to the work's current
    fetched ceiling WITHOUT sending — so enabling auto-kindle never mails the whole existing
    backlog — and later passes mail only the chapters fetched since. Members without configured
    SMTP + a Kindle address are skipped (their cursor is left untouched, so they get content
    from when they set delivery up, not a flood)."""
    from sqlalchemy import func

    from ..config import get_settings as _gs
    from ..kindle import resolve_smtp, send_document, smtp_configured
    from ..models import Bookshelf, BookshelfItem, Chapter, LibraryItem, UserSettings, Work
    from ..routers.delivery import gather_epub

    env = _gs()
    db = SessionLocal()
    try:
        pairs = db.execute(
            select(Bookshelf.user_id, BookshelfItem.work_id)
            .join(BookshelfItem, BookshelfItem.shelf_id == Bookshelf.id)
            .where(Bookshelf.auto_kindle.is_(True))
            .distinct()
        ).all()
        if not pairs:
            return
        cfg_cache: dict[int, tuple] = {}  # user_id -> (SmtpConfig, recipient)
        for user_id, work_id in pairs:
            if user_id not in cfg_cache:
                us = db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
                cfg_cache[user_id] = (
                    resolve_smtp(env, us.delivery_config if us else None),
                    ((us.kindle_email if us else None) or "").strip(),
                )
            cfg, to = cfg_cache[user_id]
            if not smtp_configured(cfg) or "@" not in to:
                continue  # member can't receive — don't baseline/advance their cursor
            li = db.scalar(select(LibraryItem).where(
                LibraryItem.user_id == user_id, LibraryItem.work_id == work_id))
            if li is None:
                continue
            max_idx = db.scalar(
                select(func.max(Chapter.index)).where(
                    Chapter.work_id == work_id, Chapter.content_id.is_not(None))
            ) or 0
            if li.auto_kindle_through is None:
                li.auto_kindle_through = max_idx  # baseline only; don't mail the backlog
                db.commit()
                continue
            if max_idx <= li.auto_kindle_through:
                continue
            work = db.get(Work, work_id)
            if work is None:
                continue
            built = gather_epub(db, work, li.auto_kindle_through + 1, None)
            if built is None:
                continue
            epub_bytes, filename, n, last = built
            try:
                send_document(
                    cfg, to_email=to, subject=f"{work.title} — new chapters",
                    body=f"{n} new chapter(s) of {work.title}, sent from Shelf.",
                    attachment=epub_bytes, filename=filename,
                )
            except Exception:  # noqa: BLE001 — one failed send mustn't abort the sweep
                log.exception("auto-kindle send failed user=%s work=%s", user_id, work_id)
                continue
            li.auto_kindle_through = last
            db.commit()
            log.info("auto-kindle sent user=%s work=%s chapters=%s through=%s",
                     user_id, work_id, n, last)
    except Exception:  # noqa: BLE001
        log.exception("auto_kindle_tick failed")
        db.rollback()
    finally:
        db.close()


def reschedule_crawl_ticks(tick_seconds: int) -> None:
    """Re-apply the crawl/index tick cadence to the running scheduler (called when the operator
    edits crawl speed). No-op if the scheduler isn't started yet — startup reads it fresh."""
    if _scheduler is None:
        return
    secs = max(2, int(tick_seconds))
    for job_id in ("crawl_tick", "index_tick"):
        try:
            _scheduler.reschedule_job(job_id, trigger="interval", seconds=secs)
        except Exception:  # noqa: BLE001 — job may not exist yet
            log.exception("could not reschedule %s", job_id)


def _initial_tick_seconds() -> int:
    """The live crawl-tuning tick cadence, falling back to the static config default."""
    try:
        from . import crawl_tuning
        db = SessionLocal()
        try:
            return crawl_tuning.get_tuning(db)["tick_seconds"]
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        return settings.scheduler_tick_seconds


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    sched = AsyncIOScheduler(timezone="UTC")
    tick_seconds = _initial_tick_seconds()
    sched.add_job(tick, "interval", seconds=tick_seconds, id="crawl_tick",
                  max_instances=1, coalesce=True)
    sched.add_job(schedule_refresh_jobs, "interval", hours=6, id="refresh_enqueue",
                  max_instances=1, coalesce=True)
    # Reaper: revive crawl jobs that died/stalled (crash, cleared rate-limit, orphaned work).
    sched.add_job(reap_stalled_jobs, "interval",
                  seconds=max(60, settings.scheduler_tick_seconds * 8),
                  id="job_reaper", max_instances=1, coalesce=True)
    # Integrity: rotate through the library detecting + repairing chapter gaps / skips.
    sched.add_job(integrity_tick, "interval", minutes=15, id="integrity_check",
                  max_instances=1, coalesce=True)
    # URL-index auto-crawl: drains pending indexed pages politely.
    from .indexer import index_tick

    sched.add_job(index_tick, "interval", seconds=tick_seconds,
                  id="index_tick", max_instances=1, coalesce=True)
    # Permanently cache remote cover images locally (covers from indexing + hooked works).
    sched.add_job(cache_images_tick, "interval", seconds=30, id="cache_images",
                  max_instances=1, coalesce=True)
    # Keep the SQLite WAL from ballooning under the continuous crawl (checkpoint starvation).
    sched.add_job(wal_checkpoint_tick, "interval", seconds=30, id="wal_checkpoint",
                  max_instances=1, coalesce=True)
    # Watched-folder safety rescan (backstops any filesystem events watchdog missed).
    sched.add_job(_folder_rescan, "interval", minutes=10, id="folder_rescan",
                  max_instances=1, coalesce=True)
    # Integration sync: pull Readarr/Kapowarr libraries into the catalog periodically.
    from ..integrations.sync import sync_all

    # Run an initial sweep shortly after startup (APScheduler's interval trigger otherwise waits
    # a full interval before its first run, so on a fresh/restarted instance metadata enrichment
    # would be delayed up to 6 hours), then every 6 hours.
    sched.add_job(sync_all, "interval", hours=6, id="integration_sync",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(seconds=45))
    # Auto-hook queued related/wishlist titles as they appear in the index.
    sched.add_job(queued_hook_tick, "interval", minutes=5, id="queued_hooks",
                  max_instances=1, coalesce=True)
    # Auto-Kindle: mail newly fetched chapters of works on members' auto_kindle shelves.
    sched.add_job(auto_kindle_tick, "interval", minutes=10, id="auto_kindle",
                  max_instances=1, coalesce=True)
    sched.start()
    _scheduler = sched
    log.info("crawl scheduler started (tick=%ss)", tick_seconds)
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
