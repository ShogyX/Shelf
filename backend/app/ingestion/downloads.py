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
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..integrations import IntegrationError
from ..integrations.sabnzbd import SABnzbdClient
from ..models import (
    Bookshelf,
    CatalogWork,
    DownloadJob,
    Integration,
    UserSettings,
    WatchedFolder,
    Work,
)
from . import broken, language, verify

log = logging.getLogger("shelf.downloads")

ACTIVE_STATUSES = ("queued", "downloading", "completed", "retry")
# A grab stuck in queue/history limbo (SAB lost it) longer than this is failed, not retried forever.
_STALE_AFTER = timedelta(hours=12)

# Serialize the poll/import tick (scheduled tick + any manual trigger) so a completion isn't
# imported twice by concurrent runs.
_poll_lock = threading.Lock()
# Serialize grabs so two near-simultaneous requests for the same book can't both pass the
# "one active grab" check and double-enqueue. asyncio (not threading) since callers are async.
_grab_lock = asyncio.Lock()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime:
    """SQLite returns naive datetimes (no tz stored); normalize to UTC-aware so arithmetic against
    _utcnow() doesn't raise 'can't subtract offset-naive and offset-aware'."""
    if dt is None:
        return _utcnow()
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def get_sabnzbd(db: Session) -> Integration | None:
    return db.scalar(
        select(Integration).where(Integration.kind == "sabnzbd", Integration.enabled.is_(True))
    )


def _category(integ: Integration) -> str:
    return ((integ.config or {}).get("category") or "shelf").strip() or "shelf"


def _path_mappings(integ: Integration) -> list[dict]:
    return (integ.config or {}).get("path_mappings") or []


def _library_dir(integ: Integration) -> str | None:
    """Shelf-local directory verified downloads are PROMOTED into (the watched library). When unset,
    downloads are imported in place from where SAB dropped them (no separate staging)."""
    p = ((integ.config or {}).get("library_path") or "").strip()
    return p or None


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


CANDIDATE_CAP = 6   # most releases we'll try (download+verify) before giving up on a book


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


def map_path(remote: str | None, mappings: list[dict]) -> str | None:
    """Translate a SABnzbd-host path into the path Shelf reads (remote→local mount), longest
    remote prefix first. Returns the input unchanged when nothing matches."""
    if not remote:
        return remote
    for m in sorted(mappings, key=lambda x: len(x.get("remote", "")), reverse=True):
        r, l = (m.get("remote") or ""), (m.get("local") or "")
        if r and remote.startswith(r):
            return l + remote[len(r):]
    return remote


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


async def _enqueue(job: DownloadJob, cand: dict, client: SABnzbdClient, cat: str,
                   priority: int | None = None) -> None:
    """Hand one candidate's NZB to SABnzbd and stamp the job with it (does not commit)."""
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
    job.status = "queued"


async def grab_release(
    db: Session, catalog_work: CatalogWork, scored=None, *, candidates: list[dict] | None = None,
    user_id: int | None = None, shelf_id: int | None = None, kind: str = "manual",
) -> DownloadJob:
    """Send a matched release to SABnzbd and record a DownloadJob. Idempotent per (book, user):
    a second user requesting an in-flight book PIGGYBACKS on the same download (no second grab)
    and still gets it imported into their library. Serialized so concurrent requests for the same
    book can't double-enqueue.

    ``candidates`` is the ranked cascade (serializable candidate dicts) to try in order — the first
    is enqueued now and the rest are stored so the poll/import path can advance to the next one if
    this download fails or fails content verification. Passing a single ``scored`` builds a
    one-element cascade (manual single-release grab)."""
    if catalog_work.hooked_work_id:
        raise IntegrationError("this title is already in the library")
    async with _grab_lock:
        # Dedup across the whole title cluster (same norm_key), not just this exact row — the
        # acquire/queued-hook paths may pick different CatalogWork rows for the same logical book.
        if catalog_work.norm_key:
            member_ids = list(db.scalars(
                select(CatalogWork.id).where(CatalogWork.norm_key == catalog_work.norm_key)
            ).all())
        else:
            member_ids = [catalog_work.id]
        active = db.scalars(
            select(DownloadJob).where(
                DownloadJob.catalog_work_id.in_(member_ids),
                DownloadJob.status.in_(ACTIVE_STATUSES),
            )
        ).all()
        for j in active:
            if j.user_id == user_id:
                return j  # this user already has a grab in flight for this book

        sab = get_sabnzbd(db)
        if sab is None:
            raise IntegrationError("no SABnzbd downloader is configured")

        if active:
            # A download for this book is already running for someone else — attach this user to it
            # (shared nzo, no second SAB enqueue). The poll/import path adds it to their library too.
            # Followers carry no candidate cascade of their own; the primary drives the cascade and
            # the poll path re-points followers if it advances to a new nzo.
            p = active[0]
            job = DownloadJob(
                catalog_work_id=catalog_work.id, user_id=user_id, target_shelf_id=shelf_id,
                title=catalog_work.title, release_title=p.release_title, indexer=p.indexer,
                size=p.size, fmt=p.fmt, nzo_id=p.nzo_id, sab_category=p.sab_category,
                release_key=p.release_key, status=p.status, grab_kind=kind,
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            return job

        # Build the candidate cascade (explicit list, or a one-element list from `scored`).
        cands = list(candidates) if candidates else ([_candidate_from_scored(scored)] if scored else [])
        cands = [c for c in cands if c.get("download_url")][:CANDIDATE_CAP]
        if not cands:
            raise IntegrationError("this release has no download URL")
        cat = _category(sab)
        # Persist the job BEFORE enqueuing so a commit failure can't leave an untracked SAB
        # download running forever (orphan); fill in the nzo right after the enqueue succeeds.
        job = DownloadJob(
            catalog_work_id=catalog_work.id, user_id=user_id, target_shelf_id=shelf_id,
            title=catalog_work.title, sab_category=cat, status="queued", grab_kind=kind,
            candidates=cands, attempt=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        client = SABnzbdClient(sab.base_url, sab.api_key)
        try:
            await _enqueue(job, cands[0], client, cat, _priority(sab))
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = f"grab failed: {exc}"
            db.commit()
            raise IntegrationError(str(exc)) from exc
        db.commit()
        log.info("grab queued: %r → SAB %s (cat=%s, %d candidate(s))",
                 job.title, job.nzo_id, cat, len(cands))
        return job


async def auto_grab(db: Session, catalog_work: CatalogWork, *,
                    user_id: int | None = None, shelf_id: int | None = None,
                    context: dict | None = None, speculative: bool = True) -> DownloadJob | None:
    """Match `catalog_work` against Prowlarr and grab it as a candidate cascade: the confidently
    auto-grabbable releases first, then (when ``speculative``) accepted-but-lower-confidence ones —
    each tried in turn, downloaded, and CONTENT-VERIFIED, so a wrong/dead release is discarded and
    the next is tried. Returns the DownloadJob, or None when there's no plausible release at all.
    ``context`` (series name + full author + volume) relaxes the gate for a known series volume.

    When ``speculative`` is False only auto-grabbable releases are used (no download-to-verify of
    uncertain matches) — kept for callers that must not spend bandwidth on guesses."""
    from . import release_matcher as rm
    ranked = await rm.find_releases(db, catalog_work, context=context)
    cands = rm.candidate_dicts(ranked, cap=CANDIDATE_CAP, include_speculative=speculative)
    if not cands:
        return None
    return await grab_release(db, catalog_work, candidates=cands,
                              user_id=user_id, shelf_id=shelf_id, kind="auto")


def _job_dir(path: str | None) -> str | None:
    """The download's OWN folder from the SAB-reported (mapped) storage path: the path itself when
    it's a directory, or its parent when SAB reported the unpacked file inside it. Deliberately does
    NOT climb further — climbing to the shared drop-zone root could make verification scan and match
    a file from a DIFFERENT download. A missing job folder returns None (treated as not-yet-visible
    → retry), never the parent zone."""
    p = (path or "").rstrip("/")
    if not p:
        return None
    if os.path.isdir(p):
        return p
    parent = os.path.dirname(p)
    if parent and os.path.isdir(parent):
        return parent
    return None


def _safe_name(s: str | None) -> str:
    """A filesystem-safe per-book subfolder name from a title."""
    s = re.sub(r"[^\w .,'()\-]+", " ", (s or "")).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:120] if s else ""


def _promote(src_file: str, lib_dir: str | None, want_title: str) -> str | None:
    """Move a verified file out of staging into the library under a per-book subfolder. Returns the
    final path. When no library dir is configured, returns the file in place (import without
    staging). None on a move error."""
    if not lib_dir:
        return src_file
    try:
        dest_dir = os.path.join(lib_dir, _safe_name(want_title) or "book")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(src_file))
        if os.path.abspath(src_file) == os.path.abspath(dest):
            return dest
        if os.path.exists(dest):
            os.remove(dest)             # a prior attempt left a copy → overwrite
        shutil.move(src_file, dest)
        return dest
    except OSError:
        log.exception("promote failed: %s → %s", src_file, lib_dir)
        return None


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


def _import_completed(db: Session, job: DownloadJob, sab: Integration) -> str:
    """Verify a finished STAGING download, and on success promote the verified file into the library,
    import it, and link it to the catalog book + requester's library. Returns the verdict and sets
    job.status: 'imported' (done), 'retry' (verify/visibility failed → cascade should advance),
    'failed' (verified but couldn't be placed). Files in staging are not touched by any other
    automation until they're confirmed correct here."""
    from ..library import add_to_library
    from .local_folder import sync_folder

    cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
    want_title = (cw.title if (cw and cw.title) else None) or job.title
    want_author = cw.author if cw else None
    want_language = language.canonicalize(cw.language) if (cw and cw.language) else None

    staging_local = map_path(job.storage_path, _path_mappings(sab))
    staging_dir = _job_dir(staging_local)
    if not staging_dir:
        # Not visible yet — almost always transient (mount/NFS lag, SAB still finalizing). Do NOT
        # treat this as a wrong-book verify failure (which would blacklist a GOOD release and delete
        # it); just wait and re-poll. The stale window bounds how long we wait.
        if _utcnow() - _aware(job.created_at) > _STALE_AFTER:
            job.status = "failed"
            job.error = f"completed download never became visible (path {staging_local!r})"
            db.commit()
            return "failed"
        job.status = "downloading"
        job.error = f"awaiting visibility of completed download ({staging_local!r})"
        db.commit()
        log.info("import: path not visible yet, will re-poll: %s", staging_local)
        return "wait"

    # Look INSIDE the download: only content that really is the requested book — in the requested
    # language — is accepted.
    vr = verify.verify_download(staging_dir, want_title, want_author,
                                min_confidence=_verify_floor(sab), want_language=want_language)
    if not vr.ok or not vr.path:
        job.status = "retry"
        job.error = f"content mismatch ({vr.reason}; conf {vr.confidence:.2f})"
        db.commit()
        log.info("verify FAILED %r: %s (conf %.2f)", want_title, vr.reason, vr.confidence)
        return "retry"

    lib = _library_dir(sab)
    promoted = _promote(vr.path, lib, want_title)
    if not promoted:
        job.status = "failed"
        job.error = "verified but could not promote into the library"
        db.commit()
        return "failed"

    # Import from the library (or, with no library configured, from the staging dir in place) and
    # link by the EXACT promoted path — deterministic, no fragile title-overlap matching.
    import_root = lib or staging_dir
    folder = ensure_watched_folder(db, import_root)
    if folder is not None:
        try:
            sync_folder(db, folder)
        except Exception:  # noqa: BLE001
            log.exception("folder sync during import failed")

    src = _local_source(db)
    work = db.scalar(select(Work).where(Work.source_id == src.id, Work.local_path == promoted))
    if work is None:  # fall back to a same-dir filename match (path normalization differences)
        base = os.path.basename(promoted)
        same_dir = db.scalars(select(Work).where(
            Work.source_id == src.id,
            Work.local_path.like(os.path.dirname(promoted).rstrip("/") + "/%"),
        )).all()
        work = next((w for w in same_dir if os.path.basename(w.local_path or "") == base), None)
    if work is None:
        job.status = "failed"
        job.error = f"verified+promoted but import produced no Work for {promoted!r}"
        db.commit()
        log.warning("import produced no Work for %s", promoted)
        return "failed"

    job.work_id = work.id
    job.verified = True
    job.status = "imported"
    job.completed_at = _utcnow()
    if cw is not None and cw.hooked_work_id is None:
        cw.hooked_work_id = work.id
    _apply_series(work, cw)
    if job.user_id:
        try:
            add_to_library(db, job.user_id, work.id, shelf_id=job.target_shelf_id)
        except Exception:  # noqa: BLE001 — shelf placement must not undo a durable import
            db.rollback()
            log.exception("add_to_library failed for job %s", job.id)
            job.work_id = work.id
            job.verified = True
            job.status = "imported"
            job.completed_at = _utcnow()
            if cw is not None and cw.hooked_work_id is None:
                cw.hooked_work_id = work.id
    db.commit()
    log.info("imported (verified %.2f) %r → work %s", vr.confidence, job.title, work.id)
    _notify_import(db, job, work)
    return "imported"


async def _grab_next(db: Session, job: DownloadJob, sab: Integration, *, reason: str) -> bool:
    """Advance the cascade after the current candidate failed: mark it broken, clean its staging
    download, then enqueue the next not-broken candidate. Returns True if a next candidate was
    enqueued (job still active), False if the cascade is exhausted (job set failed)."""
    cur = _current_candidate(job)
    if cur:
        broken.mark_broken(db, cur, reason=reason)
    await _cleanup_staging(job, sab)

    cands = job.candidates or []
    client = SABnzbdClient(sab.base_url, sab.api_key)
    nxt = job.attempt + 1
    while nxt < len(cands):
        if cands[nxt].get("key") in broken.broken_keys(db):  # already known-dead → skip
            nxt += 1
            continue
        try:
            await _enqueue(job, cands[nxt], client, _category(sab), _priority(sab))  # set on success
        except Exception as exc:  # noqa: BLE001 — this candidate won't enqueue; mark + try next
            broken.mark_broken(db, cands[nxt], reason=f"enqueue failed: {exc}")
            nxt += 1
            continue
        job.attempt = nxt   # advance only after a successful enqueue (no half-updated attempt)
        db.commit()
        log.info("cascade advance %r → candidate %d/%d (after: %s)",
                 job.title, nxt + 1, len(cands), reason)
        return True
    job.status = "failed"
    job.error = (reason or "download failed")[:1000]
    db.commit()
    return False


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


def _fail_followers(db: Session, followers: list[DownloadJob], error: str | None) -> None:
    for f in followers:
        f.status = "failed"
        f.error = (error or "download failed")[:1000]
    db.commit()


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


def _notify_import(db: Session, job: DownloadJob, work: Work) -> None:
    """Push a notification when an auto-fetched title lands on a shelf with notify-on-add set."""
    if not (job.user_id and job.target_shelf_id):
        return
    shelf = db.get(Bookshelf, job.target_shelf_id)
    if not shelf or shelf.user_id != job.user_id or not shelf.notify_on_add:
        return
    us = db.scalar(select(UserSettings).where(UserSettings.user_id == job.user_id))
    url = (us.apprise_url if us else None) or ""
    if not url.strip():
        return
    from ..notify import notify
    try:
        notify(url.strip(), "Shelf", f'Downloaded to "{shelf.name}": {work.title}')
    except Exception:  # noqa: BLE001 — a failed push must not break the import
        log.exception("notify failed")


async def poll_tick(db: Session) -> dict:
    """Advance active downloads: reconcile against the SAB queue/history and import completions.
    Serialized so a completion is never imported twice."""
    if not _poll_lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        jobs = db.scalars(
            select(DownloadJob).where(DownloadJob.status.in_(ACTIVE_STATUSES))
        ).all()
        if not jobs:
            return {"active": 0}
        sab = get_sabnzbd(db)
        if sab is None:
            return {"active": len(jobs), "error": "no sabnzbd"}
        client = SABnzbdClient(sab.base_url, sab.api_key)
        try:
            queue = {s.nzo_id: s for s in await client.queue()}
            # Generous history window so a completion isn't rotated out before a poll observes it.
            history = {s.nzo_id: s for s in await client.history(limit=500)}
        except IntegrationError as exc:
            log.info("download poll: SAB unreachable: %s", exc)
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
                for j in group:
                    if j.status != "downloading":
                        j.status = "downloading"
                db.commit()
                continue
            if primary.nzo_id and primary.nzo_id in history:
                h = history[primary.nzo_id]
                st = (h.status or "").lower()
                if st == "completed":
                    primary.storage_path = h.storage  # SAB-reported path; mapped to local at import
                    verdict = _import_completed(db, primary, sab)
                    if verdict == "imported":
                        # Clean staging only when promotion MOVED the file into the library; in
                        # in-place mode (no library_path) del_files would delete the imported file.
                        if _library_dir(sab):
                            await _cleanup_staging(primary, sab)
                        _propagate_import(db, primary, followers)
                        imported += 1 + len(followers)
                    elif verdict == "retry":
                        if await _grab_next(db, primary, sab, reason=primary.error or "verify failed"):
                            _repoint_followers(db, followers, primary)
                        else:
                            _fail_followers(db, followers, primary.error)
                            failed += 1 + len(followers)
                    elif verdict == "wait":
                        pass  # completed but not visible yet → leave active, re-poll next tick
                    else:  # failed (verified but unplaceable, or never became visible)
                        _fail_followers(db, followers, primary.error)
                        failed += 1 + len(followers)
                elif st == "failed":
                    msg = h.fail_message or "download failed"
                    if await _grab_next(db, primary, sab, reason=msg):
                        _repoint_followers(db, followers, primary)
                    else:
                        _fail_followers(db, followers, msg)
                        failed += 1 + len(followers)
                # else still post-processing (extracting/verifying) → leave as downloading
                continue
            # Not in queue or history: SAB no longer knows it. Fail it once it's clearly stale.
            if _utcnow() - _aware(primary.created_at) > _STALE_AFTER:
                primary.status = "failed"
                primary.error = "SABnzbd no longer tracks this download"
                _fail_followers(db, followers, primary.error)
                db.commit()
                failed += 1 + len(followers)
        return {"active": len(jobs), "imported": imported, "failed": failed}
    finally:
        _poll_lock.release()
