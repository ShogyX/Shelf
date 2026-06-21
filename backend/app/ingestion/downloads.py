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
    """The local directory SAB drops THIS category's completed downloads into — the parent of a
    recent job's per-download folder. Returns None when unknown OR when the parent isn't recognizably
    Shelf's own category dir.

    SAFETY: the sweep below DELETES unreferenced entries under this root, so it must never resolve to
    a drop zone shared with another consumer (this SAB instance is shared with Sonarr). SAB's standard
    layout is ``<complete>/<category>/<job>``, so the parent's basename equals our category. We REQUIRE
    that match: if the path doesn't nest by our category (e.g. Shelf points straight at a shared
    ``<complete>`` root), we return None and the GC no-ops rather than risk deleting another app's
    downloads. _job_dir() itself documents the same don't-climb-into-the-shared-zone invariant."""
    row = db.scalar(
        select(DownloadJob.storage_path)
        .where(DownloadJob.storage_path.is_not(None))
        .order_by(DownloadJob.id.desc())
    )
    local = map_path(row, _path_mappings(sab))
    d = _job_dir(local) if local else None
    if not d:
        return None
    root = os.path.dirname(d)
    if os.path.basename(root.rstrip("/")) != _category(sab):
        log.info("staging GC: skipped — drop-zone root %r is not our category %r (won't sweep a "
                 "possibly-shared directory)", root, _category(sab))
        return None
    return root


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
    referenced = {
        _job_dir(map_path(p, mappings))
        for (p,) in db.execute(select(DownloadJob.storage_path).where(DownloadJob.storage_path.is_not(None)))
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

        client = SABnzbdClient(sab.base_url, sab.api_key)
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
    """Best-effort: delete the finished download from SAB (history + leftover staging files) so the
    staging area doesn't accumulate. Never raises into the caller."""
    if not job.nzo_id:
        return
    try:
        client = SABnzbdClient(sab.base_url, sab.api_key)
        await client.delete_history(job.nzo_id, del_files=True)
    except Exception:  # noqa: BLE001
        log.debug("staging cleanup failed for %s", job.nzo_id, exc_info=True)


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

    client = SABnzbdClient(sab.base_url, sab.api_key)
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
        client = SABnzbdClient(sab.base_url, sab.api_key)
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
        except IntegrationError as exc:
            # Don't fail jobs on a (possibly transient) outage — but surface WHY they're stalled on
            # the jobs themselves, so a persistent outage is visible in the UI instead of downloads
            # appearing to run forever.
            log.warning("download poll: SAB unreachable: %s", exc)
            for j in jobs:
                j.error = f"SABnzbd unreachable: {exc}"
            db.commit()
            return {"active": len(jobs), "error": str(exc)}

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
                paused = (slot.status or "").lower() == "paused"
                if (primary.progress_mb_left is None
                        or slot.mb_left < primary.progress_mb_left - 0.01):
                    primary.progress_mb_left = slot.mb_left      # real byte progress → reset the clock
                    primary.progress_at = now
                elif (not paused and primary.progress_at is not None
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
_reconcile_lock = threading.Lock()


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
        client = SABnzbdClient(sab.base_url, sab.api_key)
        try:
            completed = [h for h in await client.history(limit=500, category=_category(sab))
                         if (h.status or "").lower() == "completed" and h.storage and h.nzo_id]
        except IntegrationError as exc:
            log.info("reconcile: SAB unreachable: %s", exc)
            return {"reconciled": 0, "error": str(exc)}
        if not completed:
            return {"reconciled": 0}
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
        return {"reconciled": reconciled, "candidates": len(failed)}
    finally:
        _reconcile_lock.release()
