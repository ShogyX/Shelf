"""Download orchestration: grab a matched release via SABnzbd, then import it on completion.

Flow: a matched catalog book + a chosen release → hand the NZB to SABnzbd (the configured
category) → a poll tick tracks the SAB queue/history by nzo_id → on completion the file (which
lands on shared storage SABnzbd writes to and Shelf reads) is imported by the watched-folder
sync and linked back to the catalog book + the requester's library.

State machine: queued → downloading → completed → imported | failed. Idempotent: one active
grab per catalog book; re-grabbing an already-hooked/active title is a no-op.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import threading
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..integrations import IntegrationError
from ..integrations.sabnzbd import SABnzbdClient
from ..models import (
    CatalogWork,
    DownloadJob,
    Integration,
    UsenetGrab,
    WatchedFolder,
    Work,
)
from . import broken, ledger, verify
# The shared post-download IMPORT CORE lives in import_core.py. Its low-level primitives + verdict
# protocol are re-exported here so this module's orchestration (and the existing test/monkeypatch
# seams that reference dl.<name>) keep working unchanged. import_core imports THIS module lazily
# (inside import_completed), so this top-level import does not create a cycle.
from .import_core import (  # noqa: F401 — re-exported for callers/tests that use dl.<name>
    ACTIVE_STATUSES,
    CANDIDATE_CAP,
    VERDICT_FAILED,
    VERDICT_IMPORTED,
    VERDICT_RETRY,
    VERDICT_WAIT,
    _aware,
    _job_dir,
    _library_dir,
    _path_mappings,
    _promote_lock,
    _safe_name,
    _STALE_AFTER,
    _utcnow,
    map_path,
)
from .import_core import import_completed as _import_completed
from .import_core import notify_import as _notify_import
from .import_core import promote as _promote  # noqa: F401 — backward-compat alias (tests use dl._promote)

log = logging.getLogger("shelf.downloads")
# An IN-QUEUE download whose remaining bytes haven't moved for this long is wedged (no peers / stuck
# postproc) → advance to the next candidate instead of holding it (and its group) for _STALE_AFTER.
_STALL_AFTER = timedelta(minutes=30)
# How long a finished fetch job (imported / failed) is kept before the cleanup tick prunes it, so the
# fetch-jobs list reflects recent activity instead of growing without bound.
JOB_RETENTION = timedelta(days=14)
# Per-listing daily download cap: at most this many grabs of the SAME release (by stable
# release_key) within a rolling window. A grab that would exceed it is DEFERRED to when a slot
# frees, not refused — so we never hammer the indexer/usenet account with duplicate pulls.
DEFAULT_MAX_GRABS_PER_DAY = 2
_GRAB_WINDOW = timedelta(days=1)


class _GrabRateLimited(Exception):
    """Raised internally when a candidate can't be enqueued yet because its listing hit the daily
    cap. Carries the time a slot frees up so the job can be deferred until then."""

    def __init__(self, not_before: datetime) -> None:
        super().__init__("listing daily download cap reached")
        self.not_before = not_before


def _max_grabs_per_day(sab: Integration | None) -> int:
    cfg = (sab.config if sab else None) or {}
    try:
        return max(1, int(cfg.get("max_grabs_per_day", DEFAULT_MAX_GRABS_PER_DAY)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_GRABS_PER_DAY


def _grab_blocked_until(db: Session, release_key: str | None, *, limit: int) -> datetime | None:
    """If `release_key` has already been grabbed `limit` times in the last window, return the time
    the oldest in-window grab ages out (when a slot frees). Otherwise None (a grab is allowed now)."""
    if not release_key:
        return None
    since = _utcnow() - _GRAB_WINDOW
    times = db.scalars(
        select(UsenetGrab.created_at)
        # strict ">": a grab exactly one window old has aged out, so a job deferred to that instant
        # is never immediately re-deferred at the boundary.
        .where(UsenetGrab.release_key == release_key, UsenetGrab.created_at > since)
        .order_by(UsenetGrab.created_at)
    ).all()
    if len(times) < limit:
        return None
    # Need enough of the oldest to expire that the in-window count drops below `limit`.
    return _aware(times[len(times) - limit]) + _GRAB_WINDOW

def _record_source_exhausted(db: Session, cw: CatalogWork, job: DownloadJob, source: str,
                             *, transient: bool = False) -> None:
    """Wave B worker hook: the download cascade for ``source`` finished. Normally TERMINAL
    (``exhausted`` — every candidate broke/unverified). But ``transient=True`` (the cascade ended only
    because the download BACKEND was unreachable, e.g. SAB down mid-advance) records ``unavailable``
    with the 6h retry instead — a brief outage must NOT permanently lock the source out (R22). Guarded
    ``if req is not None`` (a job whose title has no ledger row — e.g. an operator stock job — has
    nothing to record). Ebook-only v1: an audiobook job (fmt='audio') never touches per-source state."""
    if (job.fmt or "") == "audio":
        return
    req = ledger._get(db, cw)
    if req is None:
        return
    from . import source_state
    if transient:
        source_state.record(db, req, source, "unavailable", reason="blocked",
                            retry_at=_utcnow() + ledger._TRANSIENT_RECHECK)
    else:
        source_state.record(db, req, source, "exhausted", reason="all_broken")


# Serialize the poll/import tick (scheduled tick + any manual trigger) so a completion isn't
# imported twice by concurrent runs.
_poll_lock = threading.Lock()
# Serialize grabs so two near-simultaneous requests for the same book can't both pass the
# "one active grab" check and double-enqueue. asyncio (not threading) since callers are async.
_grab_lock = asyncio.Lock()


def get_sabnzbd(db: Session) -> Integration | None:
    return db.scalar(
        select(Integration).where(Integration.kind == "sabnzbd", Integration.enabled.is_(True))
    )


def _job_retention(db: Session) -> timedelta:
    """Retention window for finished fetch jobs — operator-overridable via the SABnzbd integration
    config (``job_retention_days``), defaulting to JOB_RETENTION."""
    sab = get_sabnzbd(db)
    cfg = (sab.config if sab else None) or {}
    try:
        return timedelta(days=max(1, int(cfg.get("job_retention_days", JOB_RETENTION.days))))
    except (TypeError, ValueError):
        return JOB_RETENTION


def cleanup_jobs(db: Session, *, retention: timedelta | None = None) -> dict:
    """Prune FINISHED fetch jobs (imported / failed) older than ``retention`` so the fetch-jobs list
    stays a recent-activity view instead of growing forever. ``retention`` defaults to the
    operator-configured window (``_job_retention``). In-flight jobs (queued/downloading/deferred/
    retry/searching) are never touched. Stock items that referenced a pruned job keep their terminal
    status — the reconcile only looks up a job for items still in flight."""
    if retention is None:
        retention = _job_retention(db)
    cutoff = _utcnow() - retention
    # Use the completion time when known, else creation time (a failed job may lack completed_at).
    stamp = func.coalesce(DownloadJob.completed_at, DownloadJob.created_at)
    res = db.execute(
        delete(DownloadJob).where(DownloadJob.status.in_(("imported", "failed")), stamp < cutoff)
    )
    db.commit()
    n = res.rowcount or 0
    if n:
        log.info("download cleanup: pruned %s finished fetch job(s) older than %s days",
                 n, retention.days)
    swept = sweep_orphan_staging(db)
    return {"pruned": n, "staging_removed": swept}


# Staging dirs older than this with NO tracking download_job are safe to remove (the grace window
# avoids racing an in-flight SAB download whose job row hasn't been written yet).
STAGING_ORPHAN_GRACE = timedelta(hours=2)


def _staging_root(db: Session, sab: Integration) -> str | None:
    """Shelf's dedicated staging dir that SAB drops THIS category's completed downloads into
    (``.shelf-staging``). Returns None when none is found.

    Robust to the most RECENT job being a libgen job (empty ``storage_path``) or a torrent job (the
    qBittorrent save dir): the old code keyed off the single latest non-null ``storage_path``, so
    whenever one of those was newest — and libgen is by far the most frequent grab — it returned None or
    the wrong dir and the GC + disk-import no-op'd, letting ``.shelf-staging`` leak to tens of GB. We now
    iterate recent jobs and return the first whose path resolves to a Shelf-exclusive ``.*-staging`` dir.

    SAFETY: the sweep below DELETES under this root, so it must be Shelf-exclusive (this SAB/qBit are
    shared with Sonarr). A ``.*-staging`` name is Shelf's own; we deliberately DON'T match a bare
    ``<complete>/<category>`` here — ``shelf`` is also the qBittorrent save subfolder, so matching the
    category alone could resolve to (and sweep) the torrent client's download dir."""
    mappings = _path_mappings(sab)
    rows = db.execute(
        select(DownloadJob.storage_path)
        .where(DownloadJob.storage_path.is_not(None), DownloadJob.storage_path != "")
        .order_by(DownloadJob.id.desc()).limit(200)
    ).all()
    for (p,) in rows:
        local = (map_path(p, mappings) or "").rstrip("/")
        if not local:
            continue
        cur = os.path.dirname(local)
        for _ in range(3):       # job-folder parent, plus a level of slack for a file-inside path
            if not cur or cur == "/":
                break
            base = os.path.basename(cur)
            if base.startswith(".") and base.endswith("-staging") and os.path.isdir(cur):
                return cur
            cur = os.path.dirname(cur)
    return None


def sweep_orphan_staging(db: Session, *, max_remove: int = 500) -> int:
    """Filesystem GC for the SAB staging area. ``_cleanup_staging`` deletes via SAB by nzo_id, but a
    job pruned by retention, a job whose nzo SAB already purged, a libgen/no-nzo job, or a download
    interrupted by a restart all leave their staging folder behind forever — a real, observed leak
    (tens of GB). Remove staging folders that no current DownloadJob references and that are older
    than the grace window. Best-effort; never raises into the caller."""
    sab = get_sabnzbd(db)
    if sab is None:
        return 0
    root = _staging_root(db, sab)
    if not root or not os.path.isdir(root):
        return 0
    mappings = _path_mappings(sab)
    # Protect only IN-FLIGHT jobs' staging (queued/downloading/completed/retry). An imported job's
    # staging is leftover (the file was promoted to the library) and a failed job's is dead weight —
    # both are sweepable once past the grace window, instead of piling up until the job row is pruned.
    referenced = {
        _job_dir(map_path(p, mappings))
        for (p,) in db.execute(select(DownloadJob.storage_path).where(
            DownloadJob.storage_path.is_not(None),
            DownloadJob.status.in_(ACTIVE_STATUSES)))
        if _job_dir(map_path(p, mappings))
    }
    cutoff = datetime.now().timestamp() - STAGING_ORPHAN_GRACE.total_seconds()
    removed = 0
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return 0
    for entry in entries:
        if removed >= max_remove:
            break
        path = entry.path
        if path in referenced:
            continue
        try:
            if entry.stat().st_mtime >= cutoff:   # within grace — may be an in-flight download
                continue
            if entry.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
            removed += 1
        except OSError:
            continue
    if removed:
        log.info("staging GC: removed %s orphaned staging entr%s under %s",
                 removed, "y" if removed == 1 else "ies", root)
    return removed


def _category(integ: Integration) -> str:
    return ((integ.config or {}).get("category") or "shelf").strip() or "shelf"


def _target_dir(db: Session, integ: Integration, job: DownloadJob) -> str | None:
    """Where this job's verified file is promoted. Operator STOCK fetches go to the dedicated stock
    directory (kept apart from user downloads); everything else uses the SABnzbd library_path."""
    if (job.grab_kind or "") == "stock":
        from .stock import get_stock_dir
        sd = get_stock_dir(db)
        if sd:
            return sd
    return _library_dir(integ)


def _verify_floor(integ: Integration) -> float:
    """Minimum content-match confidence for a download to be accepted as the requested book."""
    try:
        return float((integ.config or {}).get("verify_min", verify._VERIFY_MIN))
    except (TypeError, ValueError):
        return verify._VERIFY_MIN


# SAB priority for book grabs. Books are tiny and the whole point is a fast download→verify loop, so
# default to High (1) — they shouldn't sit behind big media downloads. Configurable per deployment.
_DEFAULT_PRIORITY = 1


def _priority(integ: Integration) -> int | None:
    val = (integ.config or {}).get("priority", _DEFAULT_PRIORITY)
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return _DEFAULT_PRIORITY


FUZZ_CANDIDATE_CAP = 25   # fuzz casts a wide net: try every loose match, not just the top few
# Cascade early-abort floor. When advancing the cascade after a failure, if EVERY remaining candidate
# carries a match confidence below this floor, they're all weak speculative matches that are very
# unlikely to be the requested book — grinding through them just burns download+verify cycles. We
# stop the cascade and fail the job (so the ledger marks it + the libgen fallback can run) rather than
# try them. Set EQUAL to release_matcher.MATCH_FLOOR (0.6) so anything the matcher actually ACCEPTED
# as a candidate (confidence >= the floor) is ALWAYS still tried — only sub-floor tails (which only
# the deliberately-wide fuzz path produces) are abandoned. The old 0.65 created a dead band
# [0.60,0.65): a perfect-title release whose author was merely absent from the release name landed at
# exactly 0.60 (author-miss penalty) and was abandoned BEFORE download — discarding correct books.
CASCADE_ABORT_FLOOR = 0.6


def _candidate_from_scored(scored) -> dict:
    """A serializable candidate descriptor from a ScoredRelease (single-release / manual grab)."""
    r = scored.release
    info = getattr(scored, "info", None)
    return {
        "title": getattr(r, "title", None),
        "download_url": getattr(r, "download_url", None),
        "guid": getattr(r, "guid", None),
        "indexer": getattr(r, "indexer", None),
        "size": int(getattr(r, "size", 0) or 0),
        "fmt": getattr(info, "fmt", None),
        "confidence": float(getattr(scored, "confidence", 1.0) or 0.0),
        "auto_ok": bool(getattr(scored, "auto_ok", True)),
        "is_multi": bool(getattr(info, "is_boxset", False)),
        "key": broken.release_key(r),
    }


def _current_candidate(job: DownloadJob) -> dict | None:
    cands = job.candidates or []
    if 0 <= job.attempt < len(cands):
        return cands[job.attempt]
    return None


def _remaining_all_doomed(job: DownloadJob, broken_keys: set, *, start: int) -> bool:
    """True when EVERY still-usable candidate at index >= ``start`` is a weak speculative match
    (explicit confidence below CASCADE_ABORT_FLOOR) — so advancing the cascade would only burn
    download+verify cycles on releases very unlikely to be the requested book. Conservative by
    design: a candidate with NO confidence field, or any at/above the floor, counts as plausibly
    correct and is NOT considered doomed (we never abort while one might still be right). Returns
    False when there are no remaining usable candidates (that's normal exhaustion, handled elsewhere),
    so this only short-circuits a tail that is present but uniformly weak."""
    cands = job.candidates or []
    usable = [c for c in cands[max(0, start):]
              if c.get("download_url") and c.get("key") not in broken_keys]
    if not usable:
        return False
    for c in usable:
        conf = c.get("confidence")
        if conf is None or float(conf) >= CASCADE_ABORT_FLOOR:
            return False
    return True


def _local_source(db: Session):
    from .engine import ensure_source
    return ensure_source(db, _local_folder_adapter_cls())


def _local_folder_adapter_cls():
    from .base import registry
    return registry.get("local_folder")


def ensure_watched_folder(db: Session, local_root: str) -> WatchedFolder | None:
    """Ensure a watched folder covers `local_root` (the SAB drop zone Shelf reads), creating one
    if needed so completed downloads are imported. Returns the covering folder, or None if the
    path isn't visible to Shelf."""
    if not local_root or not os.path.isdir(local_root):
        return None
    for f in db.scalars(select(WatchedFolder)).all():
        # An existing enabled folder at or above local_root already covers it.
        if f.enabled and (local_root == f.path or local_root.startswith(f.path.rstrip("/") + "/")):
            return f
    folder = WatchedFolder(path=local_root, display_name="Downloads (SABnzbd)",
                           recursive=True, enabled=True)
    db.add(folder)
    try:
        db.commit()
    except IntegrityError:
        # Raced with another import creating the same folder (path is unique) — reuse it.
        db.rollback()
        return db.scalar(select(WatchedFolder).where(WatchedFolder.path == local_root))
    db.refresh(folder)
    try:
        from .watcher import manager
        manager.add(folder.id, folder.path, folder.recursive)
    except Exception:  # noqa: BLE001 — watching is best-effort; the periodic rescan still covers it
        log.exception("failed to attach watcher for %s", local_root)
    return folder


async def _enqueue(db: Session, job: DownloadJob, cand: dict, client: SABnzbdClient, cat: str,
                   priority: int | None = None) -> None:
    """Hand one candidate's NZB to SABnzbd, stamp the job with it, and record the grab in the
    per-listing ledger (does not commit). The caller must have already cleared the daily cap."""
    url = cand.get("download_url")
    if not url:
        raise IntegrationError("this release has no download URL")
    res = await client.add_url(url, category=cat, nzbname=cand.get("title"), priority=priority)
    job.nzo_id = (res.get("nzo_ids") or [None])[0]
    job.release_title = cand.get("title")
    job.release_key = cand.get("key")
    job.indexer = cand.get("indexer")
    job.size = int(cand.get("size") or 0)
    job.fmt = cand.get("fmt")
    job.storage_path = None
    job.not_before = None
    job.status = "queued"
    job.error = None
    if cand.get("key"):  # ledger: count this pull toward the listing's daily cap
        db.add(UsenetGrab(release_key=cand["key"], nzo_id=job.nzo_id))


async def _enqueue_available(db: Session, job: DownloadJob, client: SABnzbdClient,
                             sab: Integration, *, start: int) -> str:
    """Enqueue the first candidate at index >= `start` that is neither broken nor over its daily
    download cap, recording the grab and setting job.attempt. Returns:
      * "queued"   — a candidate was enqueued (job is active);
      * "deferred" — the only remaining candidates are rate-limited; job.status/not_before are set to
                     the soonest a slot frees (the poll tick re-enqueues then);
      * "exhausted" — no usable candidate remains (caller fails the job).
    Prefers an available ALTERNATIVE listing over waiting; only defers when every remaining
    candidate is capped."""
    cands = job.candidates or []
    bad = broken.broken_keys(db)
    cat = _category(sab)
    prio = _priority(sab)
    limit = _max_grabs_per_day(sab)
    soonest: datetime | None = None
    last_err: IntegrationError | None = None
    i = max(0, start)
    while i < len(cands):
        cand = cands[i]
        key = cand.get("key")
        if not cand.get("download_url") or (key in bad):
            i += 1
            continue
        blocked = _grab_blocked_until(db, key, limit=limit)
        if blocked is not None:
            soonest = blocked if soonest is None else min(soonest, blocked)
            i += 1
            continue
        try:
            await _enqueue(db, job, cand, client, cat, prio)
        except IntegrationError as exc:  # SAB unreachable / rejected — try the next, don't blacklist
            last_err = exc
            i += 1
            continue
        job.attempt = i
        return "queued"
    if soonest is not None:
        job.status = "deferred"
        job.not_before = soonest
        job.error = (f"Rate-limited: at most {limit} downloads/day per release. "
                     f"Scheduled to retry after {soonest:%Y-%m-%d %H:%M} UTC.")
        return "deferred"
    if last_err is not None:
        raise last_err  # every usable candidate hit an infra error → let the caller fail the job
    return "exhausted"


async def grab_release(
    db: Session, catalog_work: CatalogWork, scored=None, *, candidates: list[dict] | None = None,
    user_id: int | None = None, shelf_id: int | None = None, kind: str = "manual",
    variant: str = "ebook",
) -> DownloadJob:
    """Send a matched release to SABnzbd and record a DownloadJob. Idempotent per (book, user):
    a second user requesting an in-flight book PIGGYBACKS on the same download (no second grab)
    and still gets it imported into their library. Serialized so concurrent requests for the same
    book can't double-enqueue.

    ``candidates`` is the ranked cascade (serializable candidate dicts) to try in order — the first
    is enqueued now and the rest are stored so the poll/import path can advance to the next one if
    this download fails or fails content verification. Passing a single ``scored`` builds a
    one-element cascade (manual single-release grab)."""
    is_audio = variant == "audiobook"
    # An audiobook is a SEPARATE Work from the ebook, so a hooked ebook (catalog_work.hooked_work_id)
    # must NOT block fetching its audiobook.
    if catalog_work.hooked_work_id and not is_audio:
        raise IntegrationError("this title is already in the library")
    async with _grab_lock:
        # Dedup across the whole title cluster, not just this exact row — the acquire/queued-hook paths
        # may pick different CatalogWork rows for the same logical book. Match on norm_key AND the
        # canonical identity_key (S-DUP-5): cross-language/subtitle variants of one work carry DIFFERENT
        # norm_keys but converge on the same identity_key (MERGE-3), so a norm_key-only gate lets a
        # second user (or a drifted re-request) spawn a duplicate grab. ponytail: column matches only —
        # scanning the JSON enrich_ref/isbn set isn't worth a table scan on this hot path; the canonical
        # identity_key covers the common drift.
        conds = []
        if catalog_work.norm_key:
            conds.append(CatalogWork.norm_key == catalog_work.norm_key)
        if catalog_work.identity_key:
            conds.append(CatalogWork.identity_key == catalog_work.identity_key)
        if conds:
            member_ids = list(db.scalars(select(CatalogWork.id).where(or_(*conds))).all())
        else:
            member_ids = [catalog_work.id]
        # A "deferred" job (held back by the daily cap) counts for dedup too — otherwise a
        # re-request would spawn a second primary that grabs the SAME listing immediately and
        # defeats the cap. It must NOT count as pollable-active (see ACTIVE_STATUSES).
        active = db.scalars(
            select(DownloadJob).where(
                DownloadJob.catalog_work_id.in_(member_ids),
                DownloadJob.status.in_(ACTIVE_STATUSES + ("deferred",)),
            )
        ).all()
        # Dedup is per-variant: an audiobook grab and an ebook grab for the same title are independent
        # ('Both' = two parallel jobs), so only collapse against an in-flight job of the SAME kind.
        active = [j for j in active if ((j.fmt or "") == "audio") == is_audio]
        for j in active:
            if j.user_id == user_id:
                return j  # this user already has a grab in flight (or deferred) for this book

        sab = get_sabnzbd(db)
        if sab is None:
            raise IntegrationError("no SABnzbd downloader is configured")

        if active:
            # A grab for this book already exists for someone else — attach this user to it (shared
            # nzo, no second SAB enqueue). The poll/import path adds it to their library too.
            # Followers carry no candidate cascade of their own; the primary drives the cascade and
            # the poll path re-points followers if it advances to a new nzo. Prefer a truly-active
            # primary over a deferred one; if the primary is deferred, the follower waits with it.
            p = next((j for j in active if j.status != "deferred"), active[0])
            job = DownloadJob(
                catalog_work_id=catalog_work.id, user_id=user_id, target_shelf_id=shelf_id,
                title=catalog_work.title, release_title=p.release_title, indexer=p.indexer,
                size=p.size, fmt=p.fmt, nzo_id=p.nzo_id, sab_category=p.sab_category,
                release_key=p.release_key, status=p.status, not_before=p.not_before, grab_kind=kind,
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            return job

        # Build the candidate cascade (explicit list, or a one-element list from `scored`).
        cands = list(candidates) if candidates else ([_candidate_from_scored(scored)] if scored else [])
        cap = FUZZ_CANDIDATE_CAP if kind == "fuzz" else CANDIDATE_CAP
        cands = [c for c in cands if c.get("download_url")][:cap]
        if not cands:
            raise IntegrationError("this release has no download URL")
        cat = _category(sab)
        # Persist the job BEFORE enqueuing so a commit failure can't leave an untracked SAB
        # download running forever (orphan); fill in the nzo right after the enqueue succeeds.
        job = DownloadJob(
            catalog_work_id=catalog_work.id, user_id=user_id, target_shelf_id=shelf_id,
            title=catalog_work.title, sab_category=cat, status="queued", grab_kind=kind,
            candidates=cands, attempt=0,
            # Stamp the audio marker up front (before enqueue sets it from the candidate) so dedup +
            # import routing recognize an audiobook job even while it's still 'queued'/'deferred'.
            fmt=("audio" if is_audio else None),
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        client = SABnzbdClient(sab.base_url, sab.api_key, kind="sabnzbd", config=sab.config)
        try:
            result = await _enqueue_available(db, job, client, sab, start=0)
        except IntegrationError as exc:  # infra failure (e.g. SAB unreachable) — nothing blacklisted
            job.status = "failed"
            job.error = f"grab failed: {exc}"
            db.commit()
            raise IntegrationError(str(exc)) from exc
        if result == "exhausted":
            job.status = "failed"
            job.error = "this release has no usable download URL"
            db.commit()
            raise IntegrationError(job.error)
        db.commit()
        if result == "deferred":
            log.info("grab deferred (daily cap): %r → %s", job.title, job.not_before)
        else:
            log.info("grab queued: %r → SAB %s (cat=%s, %d candidate(s))",
                     job.title, job.nzo_id, cat, len(cands))
        return job


async def auto_grab(db: Session, catalog_work: CatalogWork, *,
                    user_id: int | None = None, shelf_id: int | None = None,
                    context: dict | None = None, speculative: bool = True,
                    variant: str = "ebook") -> DownloadJob | None:
    """Match `catalog_work` against Prowlarr and grab it as a candidate cascade: the confidently
    auto-grabbable releases first, then (when ``speculative``) accepted-but-lower-confidence ones —
    each tried in turn, downloaded, and CONTENT-VERIFIED, so a wrong/dead release is discarded and
    the next is tried. Returns the DownloadJob, or None when there's no plausible release at all.
    ``context`` (series name + full author + volume) relaxes the gate for a known series volume.

    When ``speculative`` is False only auto-grabbable releases are used (no download-to-verify of
    uncertain matches) — kept for callers that must not spend bandwidth on guesses."""
    from . import release_matcher as rm
    ranked = await rm.find_releases(db, catalog_work, context=context, variant=variant)
    cands = rm.candidate_dicts(ranked, cap=CANDIDATE_CAP, include_speculative=speculative)
    if not cands:
        return None
    return await grab_release(db, catalog_work, candidates=cands,
                              user_id=user_id, shelf_id=shelf_id, kind="auto", variant=variant)


async def _cleanup_staging(job: DownloadJob, sab: Integration) -> None:
    """Best-effort: remove this download from SAB — from the ACTIVE QUEUE *and* history + files — so it
    can't keep downloading and later land as an orphan, and staging doesn't accumulate. Called on
    cascade-advance (abandoning a candidate that may still be downloading — hence the queue delete) and
    after import (the item is in history). An nzo is in exactly one of queue/history, so the other call
    is a harmless no-op. Never raises into the caller."""
    if not job.nzo_id:
        return
    client = SABnzbdClient(sab.base_url, sab.api_key, kind="sabnzbd", config=sab.config)
    for op in (client.queue_delete, client.delete_history):
        try:
            await op(job.nzo_id, del_files=True)
        except Exception:  # noqa: BLE001
            log.debug("staging cleanup (%s) failed for %s", op.__name__, job.nzo_id, exc_info=True)


async def _grab_next(db: Session, job: DownloadJob, sab: Integration, *, reason: str) -> str:
    """Advance the cascade after the current candidate failed: mark it broken, clean its staging
    download, then enqueue the next usable candidate. Returns "queued" (a next candidate is now
    downloading), "deferred" (the only remaining candidates are over the daily cap — job held until
    a slot frees), or "failed" (cascade exhausted)."""
    cur = _current_candidate(job)
    if cur:
        broken.mark_broken(db, cur, reason=reason)
    await _cleanup_staging(job, sab)

    # Early-abort: if every remaining candidate is a weak speculative match (confidence below
    # CASCADE_ABORT_FLOOR), don't grind through them — fail the job now so the ledger marks the title
    # and the libgen fallback can run. Skip for fuzz, which DELIBERATELY tries the low-confidence long
    # tail and lets post-download verification decide. Conservative: never aborts while a candidate at
    # or above the floor (or one with no confidence recorded) remains.
    if job.grab_kind != "fuzz" and _remaining_all_doomed(
            job, broken.broken_keys(db), start=job.attempt + 1):
        job.status = "failed"
        job.error = (f"{reason}; remaining candidates are all low-confidence speculative matches "
                     f"(< {CASCADE_ABORT_FLOOR}) — abandoning the cascade")[:1000]
        db.commit()
        cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
        if cw is not None:
            ledger.mark_unavailable(db, cw, reason="all_broken", provider="pipeline")
            _record_source_exhausted(db, cw, job, "pipeline")
        log.info("cascade early-abort %r: remaining tail all < %.2f conf", job.title,
                 CASCADE_ABORT_FLOOR)
        return "failed"

    client = SABnzbdClient(sab.base_url, sab.api_key, kind="sabnzbd", config=sab.config)
    # Hold _grab_lock around the cap-check + enqueue AND the COMMIT: the commit must happen inside
    # the lock so the new/advanced job is visible to a concurrent grab_release's dedup query (which
    # runs in a different session) before the lock is released — otherwise that grab_release misses
    # the in-flight primary and enqueues a duplicate of the same listing (C2).
    infra_fail = False
    async with _grab_lock:
        try:
            result = await _enqueue_available(db, job, client, sab, start=job.attempt + 1)
        except IntegrationError as exc:  # SAB unreachable while advancing → fail this job, but it's a
            result = "exhausted"          # TRANSIENT backend outage (not all-broke) → retry the source
            reason = f"{reason}; next-candidate enqueue failed: {exc}"
            infra_fail = True
        if result == "queued":
            db.commit()
            log.info("cascade advance %r → candidate %d (after: %s)", job.title, job.attempt + 1,
                     reason)
            return "queued"
        if result == "deferred":
            db.commit()
            log.info("cascade deferred (daily cap) %r → %s", job.title, job.not_before)
            return "deferred"
        job.status = "failed"
        if job.grab_kind == "fuzz":
            job.error = ("Fuzz: downloaded every match found and none was the requested book — "
                         "no acquisition method has this title.")
        else:
            job.error = (reason or "download failed")[:1000]
        db.commit()
        # Per-TITLE ledger: the usenet cascade is exhausted for this title — record it unavailable so
        # further searches/grabs are gated until the periodic, jittered re-check is due (Stage 1).
        cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
        if cw is not None:
            led_reason = "no_match" if job.grab_kind == "fuzz" else "all_broken"
            ledger.mark_unavailable(db, cw, reason=led_reason, provider="pipeline")
            _record_source_exhausted(db, cw, job, "pipeline", transient=infra_fail)
        return "failed"


def _propagate_import(db: Session, primary: DownloadJob, followers: list[DownloadJob]) -> None:
    """A primary download imported — link its Work to each piggybacking follower's user/library
    without re-downloading or re-verifying."""
    from ..library import add_to_library
    for f in followers:
        f.work_id = primary.work_id
        f.verified = True
        f.status = "imported"
        f.completed_at = _utcnow()
        if f.user_id and primary.work_id:
            try:
                add_to_library(db, f.user_id, primary.work_id, shelf_id=f.target_shelf_id)
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("add_to_library failed for follower job %s", f.id)
                f.work_id = primary.work_id
                f.status = "imported"
    db.commit()
    for f in followers:
        w = db.get(Work, f.work_id) if f.work_id else None
        if w is not None:
            _notify_import(db, f, w)


def _repoint_followers(db: Session, followers: list[DownloadJob], primary: DownloadJob) -> None:
    """The primary advanced to a new nzo — point its piggybacking followers at it."""
    for f in followers:
        f.nzo_id = primary.nzo_id
        f.release_title = primary.release_title
        f.release_key = primary.release_key
        f.status = "queued"
    db.commit()


def _notify_failed(db: Session, job: DownloadJob) -> None:
    """Notify the requesting user (download.failed) and ops (ops.download_failed) that a download
    permanently failed. Defensive; channel routing + opt-in handled by the notifications engine."""
    from .. import notifications as notif
    cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
    title = (cw.title if cw else None) or "A title"
    body = f"{title}: {job.error or 'download failed'}"
    # The user-facing notice only applies to user-initiated jobs; the admin ops alert ALWAYS fires
    # (stock jobs have user_id=None and operators still need to know they failed).
    if getattr(job, "user_id", None):
        notif.dispatch_soon(db, "download.failed", user_id=job.user_id,
                            title="Download failed", body=body, level="warn")
    notif.dispatch_soon(db, "ops.download_failed", audience="admin", title="Download failed",
                        body=body, level="warn", dedup_key="ops.download_failed")


def _fail_followers(db: Session, followers: list[DownloadJob], error: str | None) -> None:
    for f in followers:
        f.status = "failed"
        f.error = (error or "download failed")[:1000]
        _notify_failed(db, f)
    db.commit()


def _defer_followers(db: Session, followers: list[DownloadJob], primary: DownloadJob) -> None:
    """The primary was held back by the daily cap — hold its piggybacking followers too; the resume
    path re-points them onto the primary's new nzo once it grabs."""
    for f in followers:
        f.status = "deferred"
        f.not_before = primary.not_before
        f.error = primary.error
    db.commit()


def _book_cluster_ids(db: Session, job: DownloadJob) -> list[int]:
    """Catalog-row ids for the same logical book (its whole norm_key cluster)."""
    cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
    nk = cw.norm_key if cw else None
    if nk:
        return list(db.scalars(select(CatalogWork.id).where(CatalogWork.norm_key == nk)).all())
    return [job.catalog_work_id] if job.catalog_work_id else []


async def _resume_deferred(db: Session, sab: Integration, client: SABnzbdClient) -> int:
    """Re-enqueue deferred grabs whose cap window has now passed. A grab that's still over the cap is
    simply re-deferred to the new soonest-free time. Returns how many became active."""
    now = _utcnow()
    due = db.scalars(
        select(DownloadJob).where(
            DownloadJob.status == "deferred",
            DownloadJob.candidates.is_not(None),     # only primaries carry the cascade
            DownloadJob.not_before.is_not(None),
            DownloadJob.not_before <= now,
        )
    ).all()
    resumed = 0
    for job in due:
        followers = _deferred_followers(db, job)
        try:
            # start at job.attempt: a deferred INITIAL grab never advanced (attempt 0, candidate
            # still good); a deferred CASCADE step left attempt on the now-broken candidate, which
            # the walker skips. Hold _grab_lock so this enqueue can't race a concurrent grab_release
            # past the daily-cap check (they share the same asyncio event loop).
            async with _grab_lock:
                result = await _enqueue_available(db, job, client, sab, start=job.attempt)
        except IntegrationError as exc:
            job.status = "failed"
            job.error = f"grab failed on resume: {exc}"
            _notify_failed(db, job)
            _fail_followers(db, followers, job.error)  # don't strand the piggybackers
            db.commit()
            continue
        if result == "queued":
            _repoint_followers(db, followers, job)  # bring the piggybackers onto the new nzo
            db.commit()
            resumed += 1
        elif result == "deferred":
            db.commit()  # still capped → not_before bumped to the new soonest-free time
        else:  # exhausted
            job.status = "failed"
            job.error = job.error or "no usable release on resume"
            _notify_failed(db, job)
            _fail_followers(db, followers, job.error)
            db.commit()
    return resumed


def _deferred_followers(db: Session, primary: DownloadJob) -> list[DownloadJob]:
    """Deferred piggybackers (no cascade of their own) for the same logical book as `primary`."""
    ids = _book_cluster_ids(db, primary)
    if not ids:
        return []
    return list(db.scalars(select(DownloadJob).where(
        DownloadJob.catalog_work_id.in_(ids),
        DownloadJob.status == "deferred",
        DownloadJob.candidates.is_(None),
        DownloadJob.id != primary.id,
    )).all())


def _live_primary(db: Session, job: DownloadJob) -> DownloadJob | None:
    """An ACTIVE job that carries a candidate cascade for the same logical book (the real primary),
    other than `job`. Used to re-home a piggyback follower whose primary advanced to a new nzo in a
    tick that ran after the follower was created (grab/poll race) — so it isn't wrongly failed."""
    cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
    nk = cw.norm_key if cw else None
    if nk:
        ids = list(db.scalars(select(CatalogWork.id).where(CatalogWork.norm_key == nk)).all())
    else:
        ids = [job.catalog_work_id]
    rows = db.scalars(select(DownloadJob).where(
        DownloadJob.catalog_work_id.in_(ids),
        DownloadJob.status.in_(ACTIVE_STATUSES),
        DownloadJob.id != job.id,
    )).all()
    return next((r for r in rows if r.candidates), None)


def _apply_series(work: Work, cw: CatalogWork | None) -> None:
    """Copy series name + position from the catalog row onto the imported Work so the library can
    group the series and order its volumes."""
    if cw is None or not isinstance(cw.extra, dict):
        return
    s = cw.extra.get("series")
    if isinstance(s, str) and s.strip():
        work.series = s.strip()[:255]
        p = cw.extra.get("series_position")
        if isinstance(p, (int, float)):
            work.series_position = float(p)
        sid = cw.extra.get("series_id")
        if sid:
            work.series_id = str(sid)[:64]   # stable canonical series id (Project 2)


async def poll_tick(db: Session) -> dict:
    """Advance active downloads: reconcile against the SAB queue/history and import completions.
    Serialized so a completion is never imported twice."""
    if not _poll_lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        # Prune ledger rows well past the cap window so the table can't grow unbounded (kept a few
        # windows for safety margin; only in-window rows affect the cap).
        db.execute(delete(UsenetGrab).where(UsenetGrab.created_at < _utcnow() - 3 * _GRAB_WINDOW))
        db.commit()
        # The open-library (libgen) pipeline has its own worker — exclude its jobs from the SAB poller.
        jobs = db.scalars(
            select(DownloadJob).where(DownloadJob.status.in_(ACTIVE_STATUSES),
                                      DownloadJob.grab_kind.not_in(("libgen", "torrent")))
        ).all()
        # Deferred grabs whose daily-cap window has now passed need re-enqueuing even when nothing
        # is otherwise active. Skip the SAB round-trip entirely when there's neither.
        due_deferred = db.scalar(
            select(DownloadJob.id).where(
                DownloadJob.status == "deferred", DownloadJob.not_before <= _utcnow()
            ).limit(1)
        )
        if not jobs and not due_deferred:
            return {"active": 0}
        sab = get_sabnzbd(db)
        if sab is None:
            return {"active": len(jobs), "error": "no sabnzbd"}
        client = SABnzbdClient(sab.base_url, sab.api_key, kind="sabnzbd", config=sab.config)
        resumed = 0
        if due_deferred:
            try:
                resumed = await _resume_deferred(db, sab, client)
            except IntegrationError as exc:
                log.info("download poll: resume deferred skipped: %s", exc)
            jobs = db.scalars(
                select(DownloadJob).where(DownloadJob.status.in_(ACTIVE_STATUSES),
                                          DownloadJob.grab_kind.not_in(("libgen", "torrent")))  # own workers
            ).all()
        if not jobs:
            return {"active": 0, "resumed": resumed}
        try:
            # Read the WHOLE queue, not just the first page: a job still queued past the default
            # 100-slot window must not be mistaken for one SAB dropped (which would fail it as stale
            # while it's actually alive — orphaning a download that later completes unimported).
            # Scope to OUR category so a shared SAB's other apps (Sonarr) don't bloat every poll.
            queue = {s.nzo_id: s for s in await client.queue_all(category=_category(sab))}
            # Generous history window so a completion isn't rotated out before a poll observes it.
            # Filter by OUR category: this SAB instance is shared with other apps (e.g. Sonarr),
            # whose history entries would otherwise consume the window and rotate Shelf completions
            # out of sight — after which they'd be failed as "no longer tracked" without importing.
            history = {s.nzo_id: s
                       for s in await client.history(limit=500, category=_category(sab))}
            # Is the WHOLE queue paused? During a global pause SAB reports slots as 'Queued' (not
            # 'paused'), so without this every near-complete download would trip the 30-min stall timer
            # and get abandoned mid-cascade — the exact churn that orphans hundreds of completions when
            # the queue is later resumed. Best-effort: a failure here just leaves stall detection as-is.
            try:
                globally_paused = await client.is_paused()
            except Exception:  # noqa: BLE001 — best-effort; on any failure assume not paused (old behavior)
                globally_paused = False
        except IntegrationError as exc:
            # A transient blip must NOT fail live jobs — surface WHY they're stalled and re-poll next
            # tick. But a job whose whole life predates the stale window during an outage this long is
            # genuinely stuck (SAB won't be answering for it either): converge it to the SAME terminal
            # 'failed' the reachable path uses for a job SAB "no longer tracks", so it can't sit in
            # 'downloading' forever (which also blocks re-requesting it — grab_release's ACTIVE dedup).
            # reconcile_completed_tick / operator-retry still recover it if the completion reappears.
            log.warning("download poll: SAB unreachable: %s", exc)
            now = _utcnow()
            failed = 0
            for j in jobs:
                j.error = f"SABnzbd unreachable: {exc}"
                if now - _aware(j.created_at) > _STALE_AFTER:
                    j.status = "failed"
                    j.error = "SABnzbd unreachable past the stale window — giving up (recovers if it reappears)"
                    _notify_failed(db, j)
                    failed += 1
            db.commit()
            return {"active": len(jobs), "failed": failed, "error": str(exc)}

        # Group jobs that share an nzo (a piggybacking group) so a completion/failure is handled
        # ONCE: the primary (the job carrying the candidate cascade) drives verify/import/advance and
        # the result is propagated to its followers.
        groups: dict[str, list[DownloadJob]] = {}
        for job in jobs:
            groups.setdefault(job.nzo_id or f"_id{job.id}", []).append(job)

        imported = failed = 0
        for nzo, group in groups.items():
            primary = next((j for j in group if j.candidates), group[0])
            followers = [j for j in group if j.id != primary.id]
            # A group with no cascade at all is a lone follower whose real primary moved to a new nzo
            # (created during a tick that advanced the primary). If that primary is still active,
            # re-home this follower onto it rather than processing/failing it independently.
            if not primary.candidates and (not primary.nzo_id or primary.nzo_id not in queue):
                lp = _live_primary(db, primary)
                if lp is not None:
                    _repoint_followers(db, group, lp)
                    continue
            if primary.nzo_id and primary.nzo_id in queue:
                slot = queue[primary.nzo_id]
                for j in group:
                    if j.status != "downloading":
                        j.status = "downloading"
                    if j.error and j.error.startswith("SABnzbd unreachable"):
                        j.error = None  # outage over — SAB tracks it again
                # Stall detection: track remaining MB; if it hasn't decreased for _STALL_AFTER and the
                # slot isn't paused, the download is wedged (no peers / stuck postproc) — advance to
                # the next candidate instead of holding the job (and its group) until the 12h age cap.
                now = _utcnow()
                paused = globally_paused or (slot.status or "").lower() == "paused"
                if (paused or primary.progress_mb_left is None
                        or slot.mb_left < primary.progress_mb_left - 0.01):
                    # Real byte progress OR a deliberate pause (global or per-slot) → (re)set the stall
                    # clock. A pause is NOT a stall: keeping the clock fresh means the paused span never
                    # counts toward the 30-min window, so a near-complete download isn't abandoned the
                    # instant the queue resumes.
                    primary.progress_mb_left = slot.mb_left
                    primary.progress_at = now
                elif (primary.progress_at is not None
                      and now - _aware(primary.progress_at) > _STALL_AFTER):
                    db.commit()
                    gn = await _grab_next(db, primary, sab,
                                          reason=f"stalled at {slot.mb_left:.0f}MB left (no progress)")
                    if gn == "queued":
                        _repoint_followers(db, followers, primary)
                    elif gn == "deferred":
                        _defer_followers(db, followers, primary)
                    else:
                        _fail_followers(db, followers, primary.error)
                        failed += 1 + len(followers)
                    continue
                db.commit()
                continue
            if primary.nzo_id and primary.nzo_id in history:
                h = history[primary.nzo_id]
                st = (h.status or "").lower()
                if st == "completed":
                    primary.storage_path = h.storage  # SAB-reported path; mapped to local at import
                    # _import_completed does heavy BLOCKING work — os.walk, full-file reads, zip/pdf
                    # verification, an ebook-convert subprocess (300s timeout), and cross-fs moves.
                    # Run it OFF the event loop so it can't stall every other crawl/download tick. The
                    # loop awaits it, so this `db` session is only used inside the worker for that span.
                    verdict = await asyncio.to_thread(_import_completed, db, primary, sab)
                    if verdict == "imported":
                        # Clean staging only when promotion MOVED the file into the library; in
                        # in-place mode (no library_path) del_files would delete the imported file.
                        if _library_dir(sab):
                            await _cleanup_staging(primary, sab)
                        _propagate_import(db, primary, followers)
                        imported += 1 + len(followers)
                    elif verdict == "retry":
                        gn = await _grab_next(db, primary, sab, reason=primary.error or "verify failed")
                        if gn == "queued":
                            _repoint_followers(db, followers, primary)
                        elif gn == "deferred":
                            _defer_followers(db, followers, primary)
                        else:
                            _fail_followers(db, followers, primary.error)
                            failed += 1 + len(followers)
                    elif verdict == "wait":
                        pass  # completed but not visible yet → leave active, re-poll next tick
                    else:  # failed (verified but unplaceable, or never became visible)
                        _notify_failed(db, primary)  # this path doesn't go through _grab_next, which
                        _fail_followers(db, followers, primary.error)  # is where the user is normally told
                        failed += 1 + len(followers)
                elif st == "failed":
                    msg = h.fail_message or "download failed"
                    gn = await _grab_next(db, primary, sab, reason=msg)
                    if gn == "queued":
                        _repoint_followers(db, followers, primary)
                    elif gn == "deferred":
                        _defer_followers(db, followers, primary)
                    else:
                        _fail_followers(db, followers, msg)
                        failed += 1 + len(followers)
                # else still post-processing (extracting/verifying) → leave as downloading
                continue
            # Not in queue or history: SAB no longer knows it. Fail it once it's clearly stale —
            # but FIRST re-check for a live sibling primary (the real download may have advanced to a
            # different nzo in a grab/poll race). Re-home the whole group onto it rather than failing
            # a follower whose primary is still active (I5).
            if _utcnow() - _aware(primary.created_at) > _STALE_AFTER:
                lp = _live_primary(db, primary)
                if lp is not None:
                    _repoint_followers(db, group, lp)
                    db.commit()
                    continue
                primary.status = "failed"
                primary.error = "SABnzbd no longer tracks this download"
                _notify_failed(db, primary)
                _fail_followers(db, followers, primary.error)
                db.commit()
                failed += 1 + len(followers)
        return {"active": len(jobs), "imported": imported, "failed": failed, "resumed": resumed}
    finally:
        _poll_lock.release()


# Operator-retry reconciliation: how far back to consider a FAILED job worth re-importing. A retry
# weeks later is unusual; bound it so we never re-scan ancient history forever.
_RECONCILE_MAX_AGE = timedelta(days=14)
# A staging folder not modified for this long is settled (not an in-flight download mid-write), so the
# disk-staging pass may safely try to import it.
_STAGING_SETTLE_S = 600
# Orphan-completion reaper safety rails. Only reap a completion that's been orphaned at least this long
# (so the 60s poller + a manual operator retry have ample time to claim it), and bound how many a single
# run deletes (first-line guard against a mis-classification ever turning into a mass delete).
_REAP_MIN_AGE_S = 24 * 3600
_REAP_CAP = 50
_reconcile_lock = threading.Lock()


def _is_orphan_completion(nzo_id: str, name: str, *, live_nzos: set[str],
                          failed_nzos: set[str], failed_names: set[str]) -> bool:
    """Is a COMPLETED SAB history item an orphan — safe to reap? True when nothing live or recoverable
    accounts for it: not owned by a live job (active OR just-imported) and matching (by nzo or normalized
    release name) no recent FAILED job the recovery pass could still import. Such a completion — a
    superseded cascade candidate that finished after Shelf moved on, a duplicate of an already-imported
    title, or a since-pruned job — will never be imported and only piles up in SAB's history + the staging
    folder. Gated by the SAME keys the recovery pass uses, so nothing recoverable is ever an orphan."""
    return not (nzo_id in live_nzos or nzo_id in failed_nzos or _norm_release(name) in failed_names)


def _to_remote(local: str, mappings: list[dict]) -> str:
    """Translate a local path back to the SABnzbd-host (remote) form — the reverse of map_path — so a
    folder discovered on disk can be set as a job's storage_path for import_completed to re-map."""
    for m in sorted(mappings, key=lambda x: len(x.get("local", "")), reverse=True):
        r, l = (m.get("remote") or ""), (m.get("local") or "")
        if l and local.startswith(l):
            return r + local[len(l):]
    return local


def _norm_release(s: str | None) -> str:
    """Normalize a release/job name for loose matching (strip dir + extension, dots/underscores →
    spaces, lowercase). Used ONLY to PAIR a SAB completion with a failed job — the content is still
    verified by import_completed, so a loose pair that's actually wrong is rejected at verify."""
    if not s:
        return ""
    s = os.path.basename(str(s).strip())
    s = re.sub(r"\.(nzb|par2|rar|zip|epub|pdf|mobi|azw3|cbz|cbr|m4b|mp3)$", "", s, flags=re.I)
    s = re.sub(r"[._]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


_UNTRACKED_IMPORT_CAP = 100  # untracked completions matched+imported per reconcile run (bounds file IO).
                             # Sized to out-drain SAB's completion rate so .shelf-staging doesn't pile up.


def _clean_release_title(name: str | None) -> str:
    """Best-effort 'Title' from a release/download name for catalog matching: drop an 'Author - '
    prefix and format/quality/scene tokens so it can be FTS-matched against catalog titles."""
    t = os.path.basename(str(name or "").strip())
    t = re.sub(r"\.(nzb|par2|rar|zip|epub|pdf|mobi|azw3|cbz|cbr|m4b|mp3|flac)$", "", t, flags=re.I)
    if " - " in t:
        t = t.split(" - ", 1)[1]            # 'Author - Title ...' -> 'Title ...'
    t = re.sub(r"\b(MP3|M4B|EPUB|MOBI|AZW3|PDF|FLAC|retail|unabridged|abridged|audiobook|\d{2,3}kbps?"
               r"|WEB|WEBRip|\d{4})\b", " ", t, flags=re.I)
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t)  # drop bracketed groups/qualifiers
    t = re.sub(r"[._]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _untracked_match(db: Session, local_dir: str, name: str, is_audio: bool, floor: float):
    """Decide what an UNTRACKED completed download is, so it can be imported instead of reaped:
      * find the catalog_work it belongs to — FTS-match the cleaned release name to the catalog, then
        CONFIRM the on-disk content against each candidate with verify (guards against a wrong hook);
      * if nothing confirms, fall back to a STANDALONE import keyed on the file's own embedded title
        (or the cleaned name) so the download still lands in the library, just unhooked.
    Returns (catalog_work | None, want_title). Blocking — verify reads files; call via to_thread."""
    from . import catalog, verify
    cleaned = _clean_release_title(name)
    best, best_conf, best_title = None, 0.0, ""
    for cw in (catalog.find_rows(db, q=cleaned, limit=8) if cleaned else []):
        if cw.hooked_work_id is not None or not cw.title:
            continue  # already satisfied → don't re-import a duplicate onto it
        vr = (verify.verify_audiobook(local_dir, cw.title, cw.author) if is_audio
              else verify.verify_download(local_dir, cw.title, cw.author, min_confidence=floor))
        if vr.ok and vr.path and vr.confidence > best_conf:
            best, best_conf, best_title = cw, vr.confidence, cw.title
    if best is not None:
        return best, best_title
    # No catalog match → standalone: title from the file's own metadata, else the cleaned release name.
    title = cleaned or _norm_release(name)
    if not is_audio:
        files = verify.find_book_files(local_dir)
        meta = verify.read_book_meta(files[0]) if files else None
        if meta and meta.get("title"):
            title = meta["title"]
    return None, title


async def _import_untracked_completion(db: Session, local_dir: str, name: str, sab: Integration,
                                       *, nzo_id: str | None = None, storage_path: str | None = None) -> bool:
    """Match an untracked completed download to the catalog and import it (hooked + request resolved),
    or import it as a STANDALONE library Work when nothing matches — so a download that finished with no
    live/failed job isn't just reaped/swept. Works from a SAB history item (pass nzo_id + storage_path)
    OR straight off a disk staging folder (pass storage_path only; the caller removes the folder on
    success). Content is still verified by import_completed, so a wrong catalog guess is rejected.
    Returns True iff a Work was imported."""
    from . import verify
    is_audio = bool(verify.find_audio_files(local_dir)) and not verify.find_book_files(local_dir)
    floor = _verify_floor(sab)
    cw, want_title = await asyncio.to_thread(_untracked_match, db, local_dir, name, is_audio, floor)
    if not want_title:
        return False
    # Download tracker: don't import a duplicate of an edition we already hold (beyond the one-English
    # -plus-one-Norwegian-per-format rule). EBOOKS only — the file's own declared language is reliable,
    # so a Norwegian edition is never mistaken for the English one; audiobooks lack a trustworthy
    # language here and are left to the periodic dedup tick.
    if not is_audio:
        from . import dedup
        bfiles = verify.find_book_files(local_dir)
        mk = "comic" if bfiles and os.path.splitext(bfiles[0])[1].lower() in (".cbz", ".cbr") else "text"
        blang = ((cw.language if cw is not None else None)
                 or (verify.file_language(bfiles[0], fallback_detect=True) if bfiles else None))
        if await asyncio.to_thread(dedup.edition_exists, db, title=want_title,
                                   author=(cw.author if cw is not None else None), media_kind=mk, lang=blang):
            log.info("untracked import: already hold %r (%s) — skipping duplicate", want_title, blang or "en")
            return False
    job = DownloadJob(
        catalog_work_id=(cw.id if cw is not None else None),
        title=want_title, release_title=name, nzo_id=nzo_id, storage_path=storage_path,
        grab_kind=("stock" if cw is not None else "untracked"),   # 'stock' → hook the catalog group
        fmt=("audio" if is_audio else None), status="completed",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    verdict = await asyncio.to_thread(_import_completed, db, job, sab)
    if verdict == "imported":
        if nzo_id and _library_dir(sab):
            await _cleanup_staging(job, sab)   # SAB-tracked → drop the SAB copy (disk caller rmtrees)
        return True
    db.commit()   # not imported (verify rejected / unplaceable) → leave the job; caller may reap
    return False


async def reconcile_completed_tick(db: Session) -> dict:
    """Recover OPERATOR-RETRIED downloads. A download Shelf marked FAILED (its initial grab failed)
    that an operator later RETRIED in SAB — and which then succeeded — leaves completed files in SAB's
    history/folder that the normal poller (active jobs only) never imports. Periodically match SAB's
    completed history against recent FAILED jobs (by nzo_id, else release name) and import the matches.
    Content is verified by import_completed, so a loose name pair that's wrong is simply not imported.
    Serialized; the heavy import runs off the event loop (same as poll_tick)."""
    if not _reconcile_lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        sab = get_sabnzbd(db)
        if sab is None:
            return {"reconciled": 0}
        failed = db.scalars(
            select(DownloadJob).where(
                DownloadJob.status == "failed",
                DownloadJob.grab_kind.not_in(("libgen", "torrent")),   # those have their own workers
                DownloadJob.created_at >= _utcnow() - _RECONCILE_MAX_AGE,
            )
        ).all()
        if not failed:
            return {"reconciled": 0}
        client = SABnzbdClient(sab.base_url, sab.api_key, kind="sabnzbd", config=sab.config)
        try:
            completed = [h for h in await client.history(limit=500, category=_category(sab))
                         if (h.status or "").lower() == "completed" and h.storage and h.nzo_id]
        except IntegrationError as exc:
            log.info("reconcile: SAB unreachable: %s", exc)
            completed = []   # SAB down → skip the history pass, but still scan the staging dir on disk
        # nzo_ids an ACTIVE job already owns → leave those to poll_tick (don't steal its completion).
        active_nzos = set(db.scalars(
            select(DownloadJob.nzo_id).where(DownloadJob.status.in_(ACTIVE_STATUSES),
                                             DownloadJob.nzo_id.is_not(None))
        ).all())
        # Group failed jobs by nzo (primary + piggybacking followers); index by normalized name too.
        by_nzo: dict[str, list[DownloadJob]] = {}
        by_name: dict[str, DownloadJob] = {}
        for j in failed:
            if j.nzo_id:
                by_nzo.setdefault(j.nzo_id, []).append(j)
            k = _norm_release(j.release_title or j.title)
            if k:
                by_name.setdefault(k, j)
        reconciled = 0
        for h in completed:
            if h.nzo_id in active_nzos:
                continue
            group = by_nzo.get(h.nzo_id)
            if not group:
                nm = _norm_release(h.name)
                group = [by_name[nm]] if nm in by_name else []
            group = [j for j in group if j.status == "failed"]
            if not group:
                continue
            primary = next((j for j in group if j.candidates), group[0])
            followers = [j for j in group if j.id != primary.id]
            if primary.storage_path == h.storage:
                continue   # already attempted importing THIS exact completion for this job
            primary.storage_path = h.storage
            primary.nzo_id = h.nzo_id            # adopt the succeeded nzo (a re-add may have re-minted it)
            db.commit()
            verdict = await asyncio.to_thread(_import_completed, db, primary, sab)
            if verdict == "imported":
                if _library_dir(sab):
                    await _cleanup_staging(primary, sab)
                _propagate_import(db, primary, followers)   # link/notify any piggybacking requesters
                reconciled += 1
                log.info("reconcile: imported operator-retried %r (nzo=%s, %d follower(s))",
                         primary.title, h.nzo_id, len(followers))
            else:
                db.commit()   # storage_path now records the attempt so we don't loop on bad files
                log.info("reconcile: completed files for %r did not import (%s)", primary.title, verdict)

        # Disk-staging pass ("watch the staging dir"): SAB may purge its history while the completed
        # files still sit in the staging folder. Scan the staging dir directly and import any SETTLED
        # folder matching a FAILED job (same verify gate → routes ebooks to the library and audiobooks
        # to the audiobook path). Orphan / wrong-content folders are left for the GC to sweep, so the
        # staging area can't pile up. Only failed-job folders are touched, so in-flight downloads
        # (active jobs, handled by poll_tick) are never disturbed.
        root = _staging_root(db, sab)
        mappings = _path_mappings(sab)
        if root and os.path.isdir(root):
            now = datetime.now().timestamp()
            # Release names an ACTIVE job still owns — poll_tick imports those completions, so the disk
            # untracked pass below must NOT also grab them (avoid a double import).
            active_names = {n for n in (
                _norm_release(r) for r in db.scalars(
                    select(func.coalesce(DownloadJob.release_title, DownloadJob.title))
                    .where(DownloadJob.status.in_(ACTIVE_STATUSES))).all()) if n}
            disk_untracked = 0
            try:
                entries = list(os.scandir(root))
            except OSError:
                entries = []
            for entry in entries:
                if not entry.is_dir():
                    continue
                try:
                    if entry.stat().st_mtime > now - _STAGING_SETTLE_S:
                        continue   # recently written → maybe an in-flight download; leave it
                except OSError:
                    continue
                remote = _to_remote(entry.path, mappings)
                job = by_name.get(_norm_release(entry.name))
                if job is not None and job.status == "failed":
                    if job.storage_path == remote:
                        continue   # already attempted this exact folder
                    job.storage_path = remote
                    db.commit()
                    verdict = await asyncio.to_thread(_import_completed, db, job, sab)
                    if verdict == "imported":
                        shutil.rmtree(entry.path, ignore_errors=True)   # SAB no longer tracks it → clean directly
                        reconciled += 1
                        log.info("reconcile(disk): imported %r from staging", job.title)
                    else:
                        db.commit()
                    continue
                # No failed-job match → an untracked completion whose SAB history rotated out (or whose
                # job was pruned). Match it to the catalog + import (else standalone) so its content lands
                # in the library instead of lingering until the orphan sweep DELETES un-imported content.
                # Skip anything an ACTIVE job still owns (poll_tick handles those). Capped to bound IO.
                if disk_untracked < _UNTRACKED_IMPORT_CAP and _norm_release(entry.name) not in active_names:
                    if await _import_untracked_completion(db, entry.path, entry.name, sab,
                                                          storage_path=remote):
                        shutil.rmtree(entry.path, ignore_errors=True)
                        disk_untracked += 1
                        reconciled += 1
                        log.info("reconcile(disk): imported untracked %r from staging", entry.name[:80])

        # Reap orphaned completions. The poller imports ACTIVE jobs' completions and the passes above
        # recover FAILED-job ones; anything still sitting Completed in OUR category that matches neither
        # will never be imported (a superseded cascade candidate that finished after Shelf moved on, a
        # duplicate of an already-imported title, or a job since pruned) — and unlike the torrent path's
        # _reap_orphans, nothing removed it, so it lingered in SAB history + the staging folder. Delete
        # it (history + files). Several safety rails because this is destructive on a SHARED SAB:
        #   • re-query live ownership NOW (not the top-of-fn snapshot): poll_tick runs under a SEPARATE
        #     lock and may have grabbed/imported during our awaits — a freshly live nzo must be spared;
        #   • only reap completions older than _REAP_MIN_AGE_S (poller + manual retries get their chance);
        #   • per-item category guard (belt-and-suspenders vs a loose server-side category filter);
        #   • del_files ONLY for files that actually resolve under OUR staging root, in promote mode;
        #   • a per-run cap so any mis-classification can't cascade into a mass delete.
        reaped = 0
        cat = _category(sab)
        promote_mode = _library_dir(sab) is not None
        failed_nzos, failed_names = set(by_nzo), set(by_name)
        live_nzos = set(db.scalars(select(DownloadJob.nzo_id).where(
            DownloadJob.status.in_(ACTIVE_STATUSES + ("imported",)),
            DownloadJob.nzo_id.is_not(None))).all())
        now_ts = datetime.now().timestamp()
        root_str = (root or "").rstrip("/")
        imported_untracked = 0
        for h in completed:
            if reaped >= _REAP_CAP:
                log.info("reconcile: reap cap (%d) hit — leaving the rest for the next run", _REAP_CAP)
                break
            if (h.category or "").strip().lower() != cat.lower():
                continue
            if not _is_orphan_completion(h.nzo_id, h.name, live_nzos=live_nzos,
                                         failed_nzos=failed_nzos, failed_names=failed_names):
                continue
            local = (map_path(h.storage, mappings) or "") if h.storage else ""
            under_staging = bool(root_str and local and
                                 (local == root_str or local.startswith(root_str + "/")))
            # Import the untracked completion (match to the catalog, else standalone) BEFORE reaping —
            # a download that finished with no live/failed job should land in the library, not be
            # deleted. Only for files under OUR staging (never another app's), capped per run to bound
            # the file IO. On failure it falls through to the age-gated reap below.
            if (imported_untracked < _UNTRACKED_IMPORT_CAP and under_staging
                    and local and os.path.isdir(local)):
                if await _import_untracked_completion(db, local, h.name or "", sab,
                                                      nzo_id=h.nzo_id, storage_path=h.storage):
                    imported_untracked += 1
                    reconciled += 1
                    log.info("reconcile: imported untracked completion %r", (h.name or "")[:80])
                    continue
            if not h.completed or now_ts - h.completed < _REAP_MIN_AGE_S:
                continue   # too fresh (or unknown age → fail safe) — let the poller / a retry claim it
            del_files = promote_mode and under_staging
            try:
                await client.delete_history(h.nzo_id, del_files=del_files)
                reaped += 1
                log.info("reconcile: reaped orphaned completion %r (nzo=%s, del_files=%s)",
                         h.name[:80], h.nzo_id, del_files)
            except IntegrationError:
                log.debug("reconcile: reap failed for %s", h.nzo_id, exc_info=True)
        if reaped:
            log.info("reconcile: reaped %d orphaned SAB completion(s)", reaped)
        return {"reconciled": reconciled, "reaped": reaped, "candidates": len(failed)}
    finally:
        _reconcile_lock.release()


def _demo() -> None:
    """Self-check: orphan-completion gating (this decides what gets DELETED, so verify the guards)."""
    LIVE, FN, FNAME = {"nzoA"}, {"nzoC"}, {_norm_release("Some Release NMR")}
    kw = dict(live_nzos=LIVE, failed_nzos=FN, failed_names=FNAME)
    # Anything live or recoverable is KEPT (never reaped):
    assert not _is_orphan_completion("nzoA", "x", **kw)                 # a live (active/imported) job owns it
    assert not _is_orphan_completion("nzoC", "x", **kw)                 # a failed job's nzo
    assert not _is_orphan_completion("nzoZ", "Some Release NMR", **kw)  # matches a failed job by name
    # Tracked by nothing → orphan (reaped):
    assert _is_orphan_completion("nzoZ", "Unrelated Title", **kw)
    print("downloads orphan-reaper self-check ok")


if __name__ == "__main__":
    _demo()
