"""CrawlScheduler (Stage 7).

An AsyncIOScheduler ticks periodically and drains due CrawlJobs *slowly*, fetching
only `chapters_per_tick` chapters per job per tick, within each source's rate budget.
Jobs persist a `cursor` so a backfill resumes after a restart, and content is
checksum-deduped so re-runs are idempotent.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import threading
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import Chapter, CrawlJob, Work
from .base import ChapterRef, PermanentFetchError, RateLimited
from .engine import adapter_for, get_fetcher, store_chapter_content
from .. import config_store

log = logging.getLogger("shelf.scheduler")
settings = get_settings()
_scheduler: AsyncIOScheduler | None = None


def scheduled_task(*, to_thread: bool = False):
    """Own a scheduler tick's boilerplate (F0.6): hand the body a fresh ``Session`` (SessionLocal),
    rollback + ``log.exception`` on ANY error (a tick must never escape and kill the scheduler), and
    guarantee ``close()``. ``to_thread=True`` runs a SYNC body off the event loop so blocking work
    doesn't stall it; async bodies stay on the loop (they already yield during I/O). The wrapped name
    stays a zero-arg coroutine, so APScheduler registration (``add_job(fn, ...)``) is unchanged.

    Apply to the UNIFORM ticks (``def fn(db): ...``); ticks with bespoke per-item handling, a return
    value, or mid-body snapshot release keep their own structure on purpose."""
    def deco(fn):
        name = fn.__name__
        is_async = inspect.iscoroutinefunction(fn)

        def _sync_call() -> None:
            db = SessionLocal()
            try:
                fn(db)
            except Exception:  # noqa: BLE001 — a tick must never propagate out of the scheduler
                log.exception("%s failed", name)
                db.rollback()
                _notify_job_failed(db, name)
            finally:
                db.close()

        @functools.wraps(fn)
        async def wrapper() -> None:
            if is_async:
                db = SessionLocal()
                try:
                    await fn(db)
                except Exception:  # noqa: BLE001
                    log.exception("%s failed", name)
                    db.rollback()
                    _notify_job_failed(db, name)
                finally:
                    db.close()
            elif to_thread:
                await asyncio.to_thread(_sync_call)
            else:
                _sync_call()
        return wrapper
    return deco


def _notify_job_failed(db: Session, name: str) -> None:
    """Alert admins that a scheduled job errored (rate-limited per job so a persistently-failing tick
    doesn't notify every interval). Best-effort — never let it disturb the scheduler."""
    try:
        from .. import notifications as notif
        notif.dispatch_soon(db, "ops.job_failed", audience="admin", title="Scheduler job failed",
                            body=f"The “{name}” background job raised an error (see logs).",
                            level="error", dedup_key=f"job:{name}")
    except Exception:  # noqa: BLE001
        pass


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
    """Seconds until the title's crawl window next opens (0 if open now / no window). Computed from
    the exact next ``start``:00 instant — the old whole-hours arithmetic (``or 24``) over-waited a
    full day at an hour boundary."""
    if _in_window(work, now):
        return 0.0
    s = (work.crawl_window_start or 0) % 24
    target = now.replace(hour=s, minute=0, second=0, microsecond=0)
    if target <= now:                       # start hour already passed today → next is tomorrow
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


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
    # Claim the run lease: the reaper can only revive this job once the lease lapses, and a
    # revival bumps the token — making every later commit from THIS run a detectable no-op.
    lease = _stamp_lease(db, job)
    db.commit()

    # Descramble jobs run their own pipeline (browser-capture repair of captured comic pages),
    # not the pending-chapter fetch loop below.
    if job.kind == "descramble":
        await _process_descramble_job(db, job, work)
        return

    # --- Per-title crawl policy: time window only (no daily request cap) -------
    now = _utcnow()
    wait = _seconds_until_window(work, now)
    if wait > 0:
        job.status = "scheduled"
        job.scheduled_for = now + timedelta(seconds=wait)
        job.last_error = "outside the title's allowed crawl hours; resuming later"
        db.commit()
        return

    # Refresh jobs re-check the source for new content + refreshed metadata, then FINISH — they hand
    # any pending chapters to a dedicated BACKFILL job rather than draining them as a refresh. The old
    # fall-through made a refresh job live on as a de-facto backfill, so schedule_refresh_jobs (which
    # skips a work with any open job) never re-created the periodic ~6h refresh and the cadence was
    # lost (I6).
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
        # Hand pending chapters to a backfill, then complete this refresh so the cadence resumes.
        pending_exists = db.scalar(
            select(Chapter.id).where(Chapter.work_id == work.id,
                                     Chapter.fetch_status == "pending").limit(1))
        if pending_exists and not work.crawl_paused and not _has_open_backfill(db, work.id):
            db.add(CrawlJob(work_id=work.id, kind="backfill", status="scheduled",
                            scheduled_for=_utcnow(), cursor={"next_index": 1}))
        job.status = "done"
        job.finished_at = _utcnow()
        db.commit()
        return

    # Batch size: one request per run when a per-title interval is set (so 'speed' is
    # honoured), else the global per-tick batch.
    per_request = bool(work.crawl_interval_s and work.crawl_interval_s > 0)
    if per_request:
        batch = 1
    else:
        from . import crawl_tuning
        batch = crawl_tuning.get_tuning(db)["chapters_per_tick"]
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
        # Single-writer check: if the reaper expired our lease and re-armed the job (another
        # runner may already own it), STOP — committing stale session state here would clobber
        # the new owner's status/cursor (the historical two-writer race).
        if not _renew_lease(db, job, lease):
            db.rollback()
            log.warning("lease lost mid-run work=%s job=%s — abandoning this run", work.id, job.id)
            return
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
        except RateLimited as exc:
            # The source is blocking/throttling us (e.g. comix Cloudflare after a render burst). It's
            # not this chapter's fault, so leave it pending and COOL THE WHOLE JOB DOWN — back off
            # with escalating delay and stop this run, instead of failing chapters and hammering the
            # block deeper. A successful fetch resets the escalation (the cursor is rewritten below).
            db.rollback()
            n = int((job.cursor or {}).get("rl_cooldowns", 0)) + 1
            backoff = min(_RL_COOLDOWN_CAP_S, _RL_COOLDOWN_BASE_S * (2 ** (n - 1)))
            job.cursor = {**(job.cursor or {}), "rl_cooldowns": n}
            job.status = "scheduled"
            job.scheduled_for = _utcnow() + timedelta(seconds=backoff)
            job.last_error = (f"rate-limited by source (chapter {ch.index} left pending); "
                              f"cooling down {int(backoff)}s [{exc}]")
            db.commit()
            log.warning("rate-limited work=%s — cooling down %ss (cooldown #%s): %s",
                        work.id, int(backoff), n, exc)
            return  # stop this run; the cooldown holds until scheduled_for
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
            # PER-CHAPTER attempt count (the job-global job.attempts conflated distinct chapters,
            # so a work with many bad chapters never tripped the 5-strike guard and a transient
            # burst re-churned the whole batch). Track consecutive failures of THIS chapter index
            # in the cursor; only it is failed at the threshold. Then STOP this batch — don't keep
            # churning the rest under an active fault — and resume next tick.
            cur = dict(job.cursor or {})
            n = (cur.get("fail_count", 0) + 1) if cur.get("fail_index") == ch.index else 1
            cur["fail_index"], cur["fail_count"] = ch.index, n
            job.attempts = n                      # keep the visible counter in sync (per-chapter now)
            job.last_error = f"chapter {ch.index}: {exc}"
            if n >= 5:
                ch.fetch_status = "failed"
                cur.pop("fail_index", None)
                cur.pop("fail_count", None)
                job.attempts = 0
            job.cursor = cur
            db.commit()
            log.warning("fetch failed work=%s chapter=%s (try %d): %s", work.id, ch.index, n, exc)
            break

    # Reschedule; honour the per-title interval as a minimum spacing between runs. Final
    # single-writer check first — a revived job's new schedule must not be overwritten.
    if not _renew_lease(db, job, lease):
        db.rollback()
        log.warning("lease lost at reschedule work=%s job=%s — dropping stale update",
                    work.id, job.id)
        return
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
    skipped = counts.get("skipped", 0)  # dead-end frontier probes — placeholders, not real chapters
    # Real rows = everything we listed EXCEPT the speculative dead-end placeholders. Counting the
    # placeholder would peg the total one above the real chapter count forever ("N/N+1").
    real_rows = sum(counts.values()) - skipped
    expected = work.total_chapters_expected or 0
    if failed == 0:
        # Caught up and clean: the real rows ARE the total. Keep a source-advertised ceiling only
        # when it genuinely exceeds the real rows (a still-releasing serial) — an 'expected' that
        # merely equals real_rows + the retired placeholders was inflated by them, so drop it back.
        work.total_chapters_known = real_rows
        work.total_chapters_expected = expected if expected > real_rows + skipped else real_rows
    else:
        # Outstanding failures — never report a total below what we already know about.
        work.total_chapters_known = max(work.total_chapters_known, real_rows)
        work.total_chapters_expected = max(expected, real_rows)
    db.commit()
    try:
        from .diagnose import apply_health, completeness

        apply_health(db, work, completeness(db, work))
    except Exception:  # noqa: BLE001 — health is best-effort
        log.exception("completeness check failed for work %s", work.id)
        db.rollback()
    log.info("job %s done (work %s, health=%s)", job.id, work.id, work.health)


# Captured comic chapters repaired per descramble run (each needs a slow browser render, so keep
# the batch small) and how long to wait before re-checking for newly-fetched chapters.
_DESCRAMBLE_BATCH = 2
_DESCRAMBLE_IDLE_RETRY_S = 300


def _has_open_backfill(db: Session, work_id: int) -> bool:
    return bool(db.scalar(
        select(CrawlJob.id).where(
            CrawlJob.work_id == work_id,
            CrawlJob.kind == "backfill",
            CrawlJob.status.in_(["scheduled", "running", "paused"]),
        )
    ))


async def _process_descramble_job(db: Session, job: CrawlJob, work: Work) -> None:
    """Repair scrambled pages in already-captured comic chapters (comix.to).

    Picks a small batch of fetched chapters not yet descramble-checked, runs cheap seam detection +
    (only when needed) a browser render that screenshots the descrambled <canvas> pages, and stamps
    each chapter ``descrambled_at``. Re-arms itself while the backfill is still producing chapters;
    finalizes once the whole work is checked."""
    from . import descramble

    if not descramble.is_comix(work):
        job.status = "done"
        job.finished_at = _utcnow()
        db.commit()
        return

    chapters = db.scalars(
        select(Chapter)
        .where(
            Chapter.work_id == work.id,
            Chapter.fetch_status == "fetched",
            Chapter.content_id.is_not(None),
            Chapter.descrambled_at.is_(None),
        )
        .order_by(Chapter.index)
        .limit(_DESCRAMBLE_BATCH)
    ).all()

    if not chapters:
        # Nothing left to check right now. If the backfill is still running, come back for the
        # chapters it hasn't produced yet; otherwise the work is fully checked → done.
        if _has_open_backfill(db, work.id):
            job.status = "scheduled"
            job.scheduled_for = _utcnow() + timedelta(seconds=_DESCRAMBLE_IDLE_RETRY_S)
            job.last_error = None
            db.commit()
        else:
            job.status = "done"
            job.finished_at = _utcnow()
            db.commit()
            log.info("descramble done work=%s", work.id)
        return

    fetcher = get_fetcher()
    for ch in chapters:
        try:
            fixed = await descramble.descramble_chapter(db, fetcher, work, ch)
            ch.descrambled_at = _utcnow()
            job.attempts = 0
            job.last_error = None
            db.commit()
            if fixed:
                log.info("descramble work=%s chapter=%s repaired %d page(s)", work.id, ch.index, fixed)
        except Exception as exc:  # noqa: BLE001 — browser/render hiccup
            db.rollback()
            job.attempts += 1
            job.last_error = f"chapter {ch.index}: {exc}"
            if job.attempts >= 3:
                # Give up on this stubborn chapter so the job doesn't loop on it forever.
                ch.descrambled_at = _utcnow()
                job.attempts = 0
            db.commit()
            log.warning("descramble failed work=%s chapter=%s: %s", work.id, ch.index, exc)
            # Back off briefly before the next run rather than hammering a flaky render.
            job.status = "scheduled"
            job.scheduled_for = _utcnow() + timedelta(seconds=settings.scheduler_tick_seconds * 4)
            db.commit()
            return

    job.status = "scheduled"
    job.scheduled_for = _utcnow() + timedelta(seconds=settings.scheduler_tick_seconds)
    db.commit()


# A 'running' job untouched for this long is presumed abandoned (e.g. a crash/restart
# killed it mid-fetch) and is re-armed by the reaper.
_STUCK_RUNNING_S = 600
# Run-lease duration. The runner renews the lease on every chapter it commits, so a LIVE run's
# lease never expires no matter how long the backfill is — only a crashed/hung run goes stale.
_LEASE_S = _STUCK_RUNNING_S


def _stamp_lease(db: Session, job: CrawlJob) -> str:
    """Claim the job for THIS run: fresh token + expiry. Returns the token the run must present
    on every renewal."""
    import uuid
    token = uuid.uuid4().hex[:32]
    job.lease_token = token
    job.lease_expires_at = _utcnow() + timedelta(seconds=_LEASE_S)
    return token


def _renew_lease(db: Session, job: CrawlJob, token: str) -> bool:
    """Extend the run's lease — or report that the run lost it (the reaper expired + re-armed the
    job and another runner may own it now). A runner that gets False must STOP committing: its
    session state is stale and writing it would clobber the new owner. Reads the CURRENT token
    fresh from the DB (the reaper commits from another session)."""
    current = db.execute(
        select(CrawlJob.lease_token).where(CrawlJob.id == job.id)
    ).scalar()
    if current != token:
        return False
    job.lease_expires_at = _utcnow() + timedelta(seconds=_LEASE_S)
    return True


def _lease_expired(job: CrawlJob, now: datetime) -> bool:
    """A running job is presumed dead only when its lease lapsed (NULL = legacy/pre-crash row)."""
    exp = _aware(job.lease_expires_at)
    return exp is None or exp < now
# Don't retry a work's failed chapters more often than this (anti-thrash backstop).
_FAILED_RETRY_S = 3600
# Rate-limit/anti-bot block cooldown: when a source (e.g. comix Cloudflare) blocks us, pause the
# job and resume later with exponential backoff (10 min → … → 6 h cap) instead of hammering through.
_RL_COOLDOWN_BASE_S = 600
_RL_COOLDOWN_CAP_S = 21600
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
                # Liveness is the LEASE, not elapsed wall time: a live runner renews its lease on
                # every chapter, so a long-but-healthy run is never yanked (the historical
                # two-writer race). Only a lapsed lease (crash/hang, or NULL legacy) is revived —
                # and the token is bumped so the abandoned coroutine's later commits no-op.
                if not _lease_expired(job, now):
                    continue
                import uuid
                job.lease_token = uuid.uuid4().hex[:32]
                job.lease_expires_at = None
                job.status, job.scheduled_for = "scheduled", now
                revived += 1
                continue
            if job.status in ("scheduled", "paused") and sched > now:
                # A deliberate rate-limit cooldown (source blocking us) must hold until it elapses —
                # don't pull it forward, or we'd hammer the block right back.
                if (job.cursor or {}).get("rl_cooldowns"):
                    continue
                # Parked only by a crawl-hours window → pull forward once it's open.
                if _seconds_until_window(work, now) <= 0:
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
            # A 'running' job whose lease is still LIVE is executing right now (another tick's
            # gather, a manual run) — re-picking it would start a second runner on the same work.
            # Only a lapsed lease (crashed/hung run) is eligible for pickup.
            if job.status == "running" and not _lease_expired(job, now):
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


def _library_work_ids(db: Session) -> set[int]:
    """Work ids that are in at least one user's library (a ``LibraryItem`` exists) — i.e. real
    'library items', as opposed to an orphaned shared Work no one keeps or stock not yet acquired."""
    from ..models import LibraryItem

    return set(db.scalars(select(LibraryItem.work_id).distinct()).all())


def schedule_refresh_jobs() -> None:
    """Auto-update EVERY actively-releasing library item — no per-shelf opt-in needed.

    Enqueues a periodic refresh job for each work that is hooked, still ``ongoing`` (in active
    release), trackable (a serialized/remote source that can gain chapters — static books are
    skipped), and in at least one user's library — so serialized titles always pull in their latest
    chapters automatically. The per-work ``crawl_paused`` flag is the opt-out (pause a single title);
    the shared refresh + the tracker only grab chapters NEWER than what we hold (honouring a partial
    'hooked from chapter N' start) and keep the chapter counts in step."""
    from .tracker import is_trackable

    db = SessionLocal()
    try:
        in_library = _library_work_ids(db)
        works = db.scalars(select(Work).where(Work.hooked.is_(True))).all()
        for work in works:
            # Only serialized/remote works can gain content; skip static books.
            if not is_trackable(work) or work.status != "ongoing":
                continue
            if work.crawl_paused:
                continue  # the per-title opt-out — a paused work isn't auto-refreshed
            if work.id not in in_library:
                continue  # not actually in anyone's library → nothing to keep current
            # Check for an open REFRESH job specifically — NOT any kind: a work being backfilled
            # (which can take days) must still get its periodic refresh on cadence; the two kinds
            # coexist (uq_crawl_active is per work+kind). The old any-kind check let a long backfill
            # suppress the refresh entirely (I6).
            open_refresh = db.scalar(
                select(CrawlJob.id).where(
                    CrawlJob.work_id == work.id,
                    CrawlJob.kind == "refresh",
                    CrawlJob.status.in_(["scheduled", "running", "paused"]),
                )
            )
            if open_refresh is None:
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


def schedule_descramble_jobs() -> None:
    """Enqueue a descramble job for any comix work that has captured chapters not yet checked for
    scrambled pages and no open descramble job. Covers hooks, backfill progress, and
    refresh-discovered chapters, and survives restarts (a finished job won't re-check new content
    on its own). Cheap: a single grouped query gates the whole sweep."""
    from .descramble import is_comix

    db = SessionLocal()
    try:
        # Work ids that have at least one fetched-but-unchecked captured chapter.
        candidate_ids = set(
            db.scalars(
                select(Chapter.work_id)
                .where(
                    Chapter.fetch_status == "fetched",
                    Chapter.content_id.is_not(None),
                    Chapter.descrambled_at.is_(None),
                )
                .distinct()
            ).all()
        )
        for work_id in candidate_ids:
            work = db.get(Work, work_id)
            if work is None or work.crawl_paused or not work.hooked or not is_comix(work):
                continue
            has_open = db.scalar(
                select(CrawlJob.id).where(
                    CrawlJob.work_id == work_id,
                    CrawlJob.kind == "descramble",
                    CrawlJob.status.in_(["scheduled", "running", "paused"]),
                )
            )
            if has_open:
                continue
            db.add(CrawlJob(work_id=work_id, kind="descramble", status="scheduled",
                            scheduled_for=_utcnow(), cursor={}))
        db.commit()
    except Exception:
        log.exception("schedule_descramble_jobs failed")
        db.rollback()
    finally:
        db.close()


_INTEGRITY_BATCH = 5  # works scanned per integrity tick (rotates by least-recently-checked)


@scheduled_task(to_thread=True)
def integrity_tick(db: Session) -> None:
    """Actively scan a few hooked works for chapter gaps — true index holes AND skipped
    chapter *numbers* (a contiguous-index sequential crawl that jumped a chapter) — and
    repair them. Rotates through the library by least-recently-checked so the whole
    library is covered over time without scanning everything at once. Runs off the loop
    (synchronous DB scan + repair) so it can't stall the crawl/HTTP loop."""
    from . import diagnose

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
    from ..models import CatalogGroup, CatalogWork, IndexedPage

    db = SessionLocal()
    done = 0
    try:
        # First, SALVAGE legacy covers that were localized into the LRU-swept imgcache: any whose file
        # still exists is moved into durable /covers/ (no network). Evicted ones (file gone) are left
        # for re-sourcing — books by the metadata backfill, comics by AniList enrichment. This stops
        # surviving covers from vanishing on the next sweep and is the network-free half of the heal.
        for model in (CatalogGroup, CatalogWork, Work):
            rows = db.execute(
                select(model.id, model.cover_url)
                .where(model.cover_url.like("%/imgcache/%")).limit(200)
            ).all()
            db.commit()
            for rid, url in rows:
                new = imagecache.migrate_imgcache_cover(url)
                if not new:
                    continue  # evicted → leave for the re-source paths
                db.execute(update(model).where(model.id == rid, model.cover_url == url)
                           .values(cover_url=new))
                db.commit()
                done += 1
        # CatalogGroup drives the Index cover, so it MUST be localized too (it was missing before, so
        # group covers stayed remote and flickered). Bigger batch so the ~40k remote covers catch up.
        for model in (CatalogGroup, CatalogWork, Work, IndexedPage):
            rows = db.execute(
                select(model.id, model.cover_url).where(model.cover_url.like("http%")).limit(120)
            ).all()
            db.commit()  # release the read snapshot before the slow downloads
            for rid, url in rows:
                res = imagecache.cache_cover(url)  # → durable /covers/ (never LRU-evicted); no txn held
                if res and res != imagecache.PERMANENT_FAIL:
                    new = res
                elif res == imagecache.PERMANENT_FAIL:
                    new = None  # give up → falls back to a generated cover
                else:
                    continue  # None (transient) → leave the remote URL for a later retry
                # Short, isolated write so a writer collision affects one row, not the batch.
                # Guard on the ORIGINAL remote URL: a concurrent enrich/regroup tick may have
                # written a new (deliberately-changed) cover_url while we were downloading — only
                # localize the exact URL we read, so we never resurrect a stale remote one.
                db.execute(update(model).where(model.id == rid, model.cover_url == url)
                           .values(cover_url=new))
                db.commit()
                done += 1
    except Exception:
        db.rollback()
        log.exception("cover cache tick failed")
    finally:
        db.close()
    return done


# When the cover backlog is empty, fall back from every-30s to this cadence so we don't run the 8
# unindexed cover_url scans (full-table) every tick for ~11 remaining remote covers (F07).
_COVER_IDLE_INTERVAL_S = 900.0
_cover_next_run_at: datetime | None = None


async def cache_images_tick() -> None:
    """Periodically localize remote cover images (covers discovered while indexing AND on
    hooked works). Image downloads are blocking → run off the event loop. Backs off hard once the
    backlog is empty so the batch's full-table cover_url scans don't run every 30s for nothing."""
    global _cover_next_run_at
    now = _utcnow()
    if _cover_next_run_at is not None and now < _cover_next_run_at:
        return  # idle backoff in effect → skip the scans entirely this tick
    try:
        done = await asyncio.to_thread(_cache_covers_batch)
    except Exception:
        log.exception("cache_images_tick failed")
        return
    # Found work → keep checking every tick to catch up; idle → back off until the idle interval.
    _cover_next_run_at = None if done else now + timedelta(seconds=_COVER_IDLE_INTERVAL_S)


async def imgcache_sweep_tick() -> None:
    """LRU-evict the on-disk image cache back under its size cap so it can't grow without bound
    (every cover + remote chapter <img> is cached permanently otherwise). No-op when the cap is 0.

    Covers whose cover_url was rewritten to a local /media/imgcache path are served as STATIC files
    (no re-fetch on miss), so evicting one 404s it permanently. Collect those referenced filenames
    and PIN them from eviction — chapter images + un-referenced covers remain freely evictable."""
    from .. import imagecache
    cap_mb = config_store.effective("imgcache_max_mb")
    if not (cap_mb and cap_mb > 0):
        return

    def _run() -> None:
        from ..models import CatalogGroup, CatalogWork, IndexedPage, Work
        pinned: set[str] = set()
        db = SessionLocal()
        try:
            # IndexedPage included: _cache_covers_batch also localizes IndexedPage.cover_url, and a
            # localized cover is served as a static file with NO re-fetch on miss — an evicted
            # IndexedPage-only cover would 404 forever. Pin them like the other cover-bearing models.
            for model in (CatalogGroup, CatalogWork, IndexedPage, Work):
                for (url,) in db.execute(
                    select(model.cover_url).where(model.cover_url.like("/media/imgcache/%"))
                ).all():
                    if url:
                        pinned.add(url.rsplit("/", 1)[-1])
        finally:
            db.close()
        imagecache.sweep(cap_mb * 1024 * 1024, pinned=pinned)

    try:
        await asyncio.to_thread(_run)
    except Exception:
        log.exception("imgcache_sweep_tick failed")


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


@scheduled_task(to_thread=True)
def request_stats_flush_tick(db: Session) -> None:
    """Flush the in-memory outbound-request counters to the request_stats table (the Settings → Index
    request dashboard reads from there). Cheap; runs frequently so the dashboard is near-live."""
    from .. import telemetry
    telemetry.flush(db)


@scheduled_task()
async def catalog_enrich_tick(db: Session) -> None:
    """Fill in genres/themes/popularity for discovered catalog rows, most-popular first, so the
    Index page can build category rows. Network-bound (source APIs / metadata providers) — its own
    bounded batch with internal politeness, so it's safe on the event loop."""
    from .catalog_enrichment import enrich_catalog_tick
    await enrich_catalog_tick(db)


@scheduled_task()
async def book_hot_set_tick(db: Session) -> None:
    """Advance the hybrid book catalog's hot-set seed by a bounded number of API requests
    (Open Library trending/subjects + Google Books). Network-bound + self-limited → safe on the
    event loop."""
    from .acquire import pipeline_configured
    from .book_catalog import sync_hot_set

    # Book-catalog items are only acquirable via the Prowlarr+SABnzbd pipeline and are hidden
    # from the Index when it isn't configured — so don't spend API calls seeding them then.
    if pipeline_configured(db):
        await sync_hot_set(db)


@scheduled_task()
async def metadata_backfill_tick(db: Session) -> None:
    """Long-tail catalog backfill: fill book rows still missing a cover, series tag, or synopsis
    from the metadata providers (Hardcover search + Open Library ISBN covers + provider detail
    fetches for the descriptions the bulk search APIs omit). Bounded + self-limited."""
    from .book_catalog import backfill_metadata
    from .catalog_enrichment import backfill_comix_covers

    await backfill_metadata(db)
    await backfill_comix_covers(db)  # fill comix rows that were ingested without a cover


@scheduled_task()
async def download_poll_tick(db: Session) -> None:
    """Advance active usenet downloads: reconcile against SABnzbd's queue/history and import any
    completions into the library. Network-bound + self-serialized → safe on the event loop."""
    from .downloads import poll_tick
    await poll_tick(db)


@scheduled_task()
def cleanup_download_jobs_tick(db: Session) -> None:
    """Prune finished (imported/failed) fetch jobs past their retention so the list doesn't grow
    without bound."""
    from .downloads import cleanup_jobs
    cleanup_jobs(db)


_AUTO_BACKUP_LAST_KEY = "auto_backup_last_at"


def scheduled_backup_tick() -> None:
    """Run an automatic backup when due, so an UNATTENDED instance isn't left with zero backups.
    Survives restarts by tracking the last run in app_settings (an interval-only APScheduler job
    would never fire on a frequently-restarted instance). Bounded by the retention prune that runs
    after each successful build. Runs hourly; the interval/level/keep are env-configurable."""
    from datetime import UTC, datetime

    from ..config import get_settings

    s = get_settings()
    if not config_store.effective("auto_backup_enabled"):
        return
    from .. import backups_store
    from ..db import SessionLocal
    from ..models import AppSetting
    db = SessionLocal()
    try:
        row = db.get(AppSetting, _AUTO_BACKUP_LAST_KEY)
        last = None
        if row and isinstance(row.value, str):
            try:
                last = datetime.fromisoformat(row.value)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
            except ValueError:
                last = None
        now = datetime.now(UTC)
        if last is not None and (now - last).total_seconds() < config_store.effective("auto_backup_interval_hours") * 3600:
            return
        # Stamp BEFORE building so a long build can't trigger overlapping starts on the next tick.
        if row is None:
            db.add(AppSetting(key=_AUTO_BACKUP_LAST_KEY, value=now.isoformat()))
        else:
            row.value = now.isoformat()
        db.commit()
        try:
            name = backups_store.start_build(config_store.effective("auto_backup_level"))
            log.info("auto-backup: started %s (level=%s, keep=%s)", name, config_store.effective("auto_backup_level"),
                     config_store.effective("auto_backup_keep"))
            from .. import notifications as notif
            notif.dispatch_event(db, "ops.backup", audience="admin", title="Backup completed",
                                 body=f"Automatic backup {name} started successfully.")
        except RuntimeError as exc:
            log.info("auto-backup: skipped — %s", exc)   # a build/restore is already running
    except Exception:  # noqa: BLE001
        log.exception("scheduled_backup_tick failed")
    finally:
        db.close()


_prev_health_ok = True


@scheduled_task(to_thread=True)
def monitor_tick(db: Session) -> None:
    """Watch instance health + outbound-request outcomes and alert admins. Health fires only on the
    ok→degraded TRANSITION (not every tick); the request-rate alerts are rate-limited via dedup keys."""
    global _prev_health_ok
    from .. import notifications as notif
    from .. import telemetry
    from ..routers.health import probe

    h = probe()
    ok = h.get("status") == "ok"
    if not ok and _prev_health_ok:
        why = h.get("db") and "database" or (h.get("disk") and "low disk space") or "see /health"
        notif.dispatch_event(db, "ops.health_degraded", audience="admin", title="Health degraded",
                             body=f"The instance is reporting degraded health ({why}).", level="error")
    _prev_health_ok = ok

    # Outbound request outcomes over the last 6h: a high blocked/error share is an anti-bot or
    # connectivity problem worth surfacing. Needs a minimum sample so a couple of failures at boot
    # don't alarm. Dedup keys cap each to one alert per cooldown while the condition persists.
    summ = telemetry.summary(db, hours=6)
    total = summ.get("total") or 0
    if total >= 50:
        by = {o["outcome"]: o["count"] for o in summ.get("by_outcome", [])}
        blocked = by.get("blocked", 0) / total
        errors = by.get("error", 0) / total
        if blocked >= 0.4:
            notif.dispatch_event(db, "ops.crawl_blocked", audience="admin", title="Crawl sources blocked",
                                 body=f"{round(blocked * 100)}% of recent outbound requests were blocked "
                                      "(anti-bot). Check the affected sources.", level="warn",
                                 dedup_key="ops.crawl_blocked")
        if errors >= 0.4:
            notif.dispatch_event(db, "ops.high_error_rate", audience="admin", title="High error rate",
                                 body=f"{round(errors * 100)}% of recent outbound requests errored.",
                                 level="warn", dedup_key="ops.high_error_rate")


@scheduled_task(to_thread=True)
def catalog_stock_link_tick(db: Session) -> None:
    """Mark each catalog (index) entry with its in-stock Work by matching titles against the on-disk
    files — so newly-crawled entries pick up existing stock, and acquire never has to match at
    runtime. Idempotent; skips already-correct hooks. Off the loop (sync DB scan)."""
    from .stock_link import link_catalog_to_stock
    link_catalog_to_stock(db)


@scheduled_task(to_thread=True)
def catalog_regroup_tick(db: Session) -> None:
    """Rebuild the persisted cross-source grouping (CatalogGroup/Tag/Category) the discovery rows
    read from. CPU + write heavy and skips when nothing changed → run off the event loop."""
    from .catalog_groups import regroup_catalog
    regroup_catalog(db, throttle=True)  # delta/time gate: don't full-rebuild for a tiny crawl delta (F01)


@scheduled_task(to_thread=True)
def catalog_reconcile_tick(db: Session) -> None:
    """Heal the catalog: rebuild entries for titles that were already crawled (their index page is
    still stored) but whose CatalogWork went missing — so they reappear in the Index without a
    re-fetch. Bounded + cursor-tracked, so it sweeps the fetched backlog once and then idles.
    Parses stored HTML (CPU) → run off the event loop."""
    from .catalog import reconcile_catalog_tick as _reconcile
    _reconcile(db)


@scheduled_task()
async def queued_hook_tick(db: Session) -> None:
    """Auto-hook queued titles (related series + Goodreads wishlist) once they appear in the
    index. Cheap when the queue is empty (single indexed-status query)."""
    from ..integrations import metadata_sync
    from ..models import QueuedHook

    # Wake for pending hooks AND for ones downloading via the pipeline (so they reconcile to
    # hooked/failed once the download finishes, even when nothing new is pending).
    if not db.scalar(
        select(QueuedHook.id)
        .where(QueuedHook.status.in_(("pending", "downloading"))).limit(1)
    ):
        return
    await metadata_sync.process_queued_hooks(db)


@scheduled_task()
async def missing_recheck_tick(db: Session) -> None:
    """Periodic, SPREAD-OUT re-check of titles in the missing-content ledger (Stage 2).

    Selects ``unavailable`` ContentRequest rows whose jittered ``next_check_at`` is now due, oldest
    first, capped at ``missing_recheck_batch`` per tick, and re-runs the acquire pipeline for each
    (as a system request, ``force=True`` so it bypasses its own gate and actually searches). A title
    that's now obtainable resolves via acquire's import/hook hooks; one still missing is re-marked
    unavailable with a FRESH jittered next_check_at by ``acquire``/``ledger.mark_unavailable``.

    Flood control: the per-tick batch cap + the ~30-min cadence + the ±25% jitter on next_check_at
    (which fans a burst of same-minute failures across a multi-day window) together bound how many
    re-check searches fire per unit time, so a large backlog never re-floods the services at once."""
    from .acquire import acquire, user_priority
    from .ledger import _next_check_at
    from ..models import CatalogWork, ContentRequest

    batch = max(1, int(config_store.effective("missing_recheck_batch")))
    due = db.scalars(
        select(ContentRequest).where(
            ContentRequest.status == "unavailable",
            ContentRequest.next_check_at.is_not(None),
            ContentRequest.next_check_at <= _utcnow(),
        ).order_by(ContentRequest.next_check_at).limit(batch)
    ).all()
    for row in due:
        cw = db.get(CatalogWork, row.catalog_work_id) if row.catalog_work_id else None
        if cw is None and row.norm_key:
            cw = db.scalar(select(CatalogWork).where(CatalogWork.norm_key == row.norm_key)
                           .order_by(CatalogWork.popularity.desc()))
        if cw is None:                       # the representative catalog row vanished → push it out
            row.next_check_at = _next_check_at()
            db.commit()
            continue
        # Push next_check_at forward BEFORE searching so a long/failed search can't leave the row
        # perpetually due (re-checked every tick). A "none" outcome re-marks it (fresh jitter) inside
        # acquire; a "downloading" outcome keeps this pushed-out time so an in-flight re-fetch isn't
        # re-kicked every tick (its import hook resolves the row when it lands).
        row.next_check_at = _next_check_at()
        db.commit()
        try:
            await acquire(db, cw, user_id=None, priority=user_priority(db, None), force=True)
        except Exception:  # noqa: BLE001 — one bad title must not stall the re-check batch
            db.rollback()
            log.exception("missing_recheck_tick: re-acquire failed for %r", row.title)


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
                # Global (admin) SMTP server; the per-user part is only the recipient address.
                from ..kindle import app_smtp
                us = db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
                cfg_cache[user_id] = (
                    app_smtp(db),
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
            except Exception as exc:  # noqa: BLE001 — one failed send mustn't abort the sweep
                log.exception("auto-kindle send failed user=%s work=%s", user_id, work_id)
                from .. import notifications as notif
                notif.dispatch_event(db, "kindle.failed", user_id=user_id,
                                     title="Kindle delivery failed",
                                     body=f"{work.title}: {exc}", level="warn")
                continue
            li.auto_kindle_through = last
            db.commit()
            log.info("auto-kindle sent user=%s work=%s chapters=%s through=%s",
                     user_id, work_id, n, last)
            from .. import notifications as notif
            notif.dispatch_event(db, "kindle.sent", user_id=user_id, title="Sent to Kindle",
                                 body=f"{n} new chapter(s) of “{work.title}” sent to your Kindle.")
    except Exception:  # noqa: BLE001
        log.exception("auto_kindle_tick failed")
        db.rollback()
    finally:
        db.close()


def pause_for_maintenance() -> bool:
    """Pause all scheduled crawl/refresh ticks (e.g. during a restore) so they don't fight the
    SQLite writer or act on a half-restored DB. Returns True if it actually paused (so the caller
    knows whether to resume). Running ticks finish; no new ones fire while paused."""
    if _scheduler is None:
        return False
    try:
        _scheduler.pause()
        log.info("scheduler paused for maintenance")
        return True
    except Exception:  # noqa: BLE001
        log.exception("could not pause scheduler")
        return False


def resume_after_maintenance() -> None:
    if _scheduler is None:
        return
    try:
        _scheduler.resume()
        log.info("scheduler resumed after maintenance")
    except Exception:  # noqa: BLE001
        log.exception("could not resume scheduler")


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


def reschedule_refresh(hours: int) -> None:
    """Re-apply the new-chapter-check cadence to the running scheduler (called when the operator
    edits it in Settings). No-op if the scheduler isn't started yet — startup reads it fresh."""
    if _scheduler is None:
        return
    try:
        _scheduler.reschedule_job("refresh_enqueue", trigger="interval",
                                  hours=max(1, int(hours)))
    except Exception:  # noqa: BLE001 — job may not exist yet
        log.exception("could not reschedule refresh_enqueue")


def _initial_tuning() -> dict:
    """The live crawl-tuning values, falling back to the static config default for the tick."""
    try:
        from . import crawl_tuning
        db = SessionLocal()
        try:
            return crawl_tuning.get_tuning(db)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        return {"tick_seconds": settings.scheduler_tick_seconds, "refresh_hours": 6}


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    sched = AsyncIOScheduler(timezone="UTC")
    tuning = _initial_tuning()
    tick_seconds = tuning["tick_seconds"]
    sched.add_job(tick, "interval", seconds=tick_seconds, id="crawl_tick",
                  max_instances=1, coalesce=True)
    # Check hooked titles for new chapter releases on the operator-editable cadence (default 6h).
    # Also run once shortly after startup: an interval trigger otherwise waits a FULL interval
    # before its first run, so after a restart a previously-crawled ongoing work could go up to 6h
    # before being checked for new chapters.
    sched.add_job(schedule_refresh_jobs, "interval", hours=tuning.get("refresh_hours", 6),
                  id="refresh_enqueue", max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(seconds=30))
    # Reaper: revive crawl jobs that died/stalled (crash, cleared rate-limit, orphaned work). Run
    # soon after startup so previously-crawled works with outstanding chapters resume promptly.
    sched.add_job(reap_stalled_jobs, "interval",
                  seconds=max(60, settings.scheduler_tick_seconds * 8),
                  id="job_reaper", max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(seconds=20))
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
    # Persist outbound-request telemetry deltas for the Settings → Index request dashboard.
    sched.add_job(request_stats_flush_tick, "interval", seconds=30, id="request_stats_flush",
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
    # Descramble: enqueue repair jobs for comix works with captured-but-unchecked chapters
    # (catches up the existing library + refresh-discovered chapters; survives restarts).
    sched.add_job(schedule_descramble_jobs, "interval", minutes=5, id="descramble_enqueue",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(seconds=75))
    # Discovery: enrich catalog rows with genres/themes/popularity (most-popular first), then
    # rebuild the persisted grouping the Index page's category rows read from.
    sched.add_job(catalog_enrich_tick, "interval", seconds=90, id="catalog_enrich",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(seconds=60))
    sched.add_job(catalog_regroup_tick, "interval", minutes=10, id="catalog_regroup",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(seconds=90))
    # Catalog heal: rebuild entries for already-crawled titles whose CatalogWork went missing
    # (sweeps the fetched-page backlog once from stored content, then idles). First pass soon
    # after startup so a wiped/partial catalog recovers without a manual re-index.
    sched.add_job(catalog_reconcile_tick, "interval", minutes=2, id="catalog_reconcile",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(seconds=100))
    # Hybrid book catalog: keep the popular hot-set seeded/fresh (bounded API budget per tick).
    sched.add_job(book_hot_set_tick, "interval", minutes=20, id="book_hot_set",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(seconds=120))
    # Long-tail backfill of missing covers / series tags from the other metadata providers.
    sched.add_job(metadata_backfill_tick, "interval", minutes=5, id="metadata_backfill",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(seconds=180))
    # Acquisition pipeline: poll SABnzbd for queued/finished downloads and import completions.
    sched.add_job(download_poll_tick, "interval", seconds=60, id="download_poll",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(seconds=30))
    # Library stocking: advance the operator's pre-fetch queue (bounded per tick; no-op when unset).
    from .stock import stock_libgen_tick, stock_tick
    sched.add_job(stock_tick, "interval", seconds=45, id="stock_worker",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(seconds=50))
    # Open-library (libgen) RECOVERY of stock the usenet pipeline missed — runs on its own schedule
    # (it downloads synchronously and can take a while) so it never blocks the stock worker above.
    sched.add_job(stock_libgen_tick, "interval", seconds=120, id="stock_libgen_worker",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(seconds=75))
    # Open-library fallback pipeline: download + verify queued libgen jobs (no-op unless configured).
    from .libgen import libgen_tick
    sched.add_job(libgen_tick, "interval", seconds=30, id="libgen_worker",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(seconds=40))
    # Missing-content ledger: periodically re-acquire titles known unavailable, due (jittered)
    # next_check_at first, a small batch per tick — spread out so a backlog never re-floods services.
    sched.add_job(missing_recheck_tick, "interval", minutes=30, id="missing_recheck",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(minutes=6))
    # Prune finished fetch jobs (imported/failed) past their retention so the list stays a recent view.
    sched.add_job(cleanup_download_jobs_tick, "interval", hours=6, id="download_cleanup",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(minutes=3))
    # Keep catalog entries marked with their in-stock Work (so acquire pulls stock, no runtime match).
    sched.add_job(catalog_stock_link_tick, "interval", hours=6, id="catalog_stock_link",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(minutes=5))
    # Automatic scheduled backup so an unattended instance always has recent recoverable state.
    # Checks hourly; the actual cadence (last-run survives restarts) + level + retention are env-set.
    sched.add_job(scheduled_backup_tick, "interval", hours=1, id="scheduled_backup",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(minutes=2))
    # Watch health + outbound-request outcomes; alert admins (opt-in) on degradation / blocking.
    sched.add_job(monitor_tick, "interval", minutes=15, id="monitor",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(minutes=4))
    # Bound the on-disk image cache (LRU eviction) so covers + chapter images can't fill the disk.
    sched.add_job(imgcache_sweep_tick, "interval", hours=2, id="imgcache_sweep",
                  max_instances=1, coalesce=True,
                  next_run_time=_utcnow() + timedelta(minutes=8))
    sched.start()
    _scheduler = sched
    log.info("crawl scheduler started (tick=%ss)", tick_seconds)
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        # wait=True lets an in-flight tick (notably the threadpool ones: a fetch/store-chapter
        # mid-commit, an import) finish cleanly instead of being abandoned on SIGTERM. The wait is
        # bounded by systemd TimeoutStopSec; the async-executor ticks run as loop tasks and aren't
        # blocked-joined, so this can't deadlock the shutdown.
        try:
            _scheduler.shutdown(wait=True)
        except Exception:  # noqa: BLE001 — shutdown must never raise out of lifespan teardown
            log.exception("scheduler shutdown failed")
        _scheduler = None
