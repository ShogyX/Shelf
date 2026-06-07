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

log = logging.getLogger("shelf.downloads")

ACTIVE_STATUSES = ("queued", "downloading", "completed")
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


def get_sabnzbd(db: Session) -> Integration | None:
    return db.scalar(
        select(Integration).where(Integration.kind == "sabnzbd", Integration.enabled.is_(True))
    )


def _category(integ: Integration) -> str:
    return ((integ.config or {}).get("category") or "shelf").strip() or "shelf"


def _path_mappings(integ: Integration) -> list[dict]:
    return (integ.config or {}).get("path_mappings") or []


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


async def grab_release(
    db: Session, catalog_work: CatalogWork, scored, *,
    user_id: int | None = None, shelf_id: int | None = None, kind: str = "manual",
) -> DownloadJob:
    """Send a matched release to SABnzbd and record a DownloadJob. Idempotent per (book, user):
    a second user requesting an in-flight book PIGGYBACKS on the same download (no second grab)
    and still gets it imported into their library. Serialized so concurrent requests for the same
    book can't double-enqueue."""
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
            p = active[0]
            job = DownloadJob(
                catalog_work_id=catalog_work.id, user_id=user_id, target_shelf_id=shelf_id,
                title=catalog_work.title, release_title=p.release_title, indexer=p.indexer,
                size=p.size, fmt=p.fmt, nzo_id=p.nzo_id, sab_category=p.sab_category,
                status=p.status, grab_kind=kind,
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            return job

        release = scored.release
        url = getattr(release, "download_url", None)
        if not url:
            raise IntegrationError("this release has no download URL")
        cat = _category(sab)
        # Persist the job BEFORE enqueuing so a commit failure can't leave an untracked SAB
        # download running forever (orphan); fill in the nzo right after the enqueue succeeds.
        job = DownloadJob(
            catalog_work_id=catalog_work.id, user_id=user_id, target_shelf_id=shelf_id,
            title=catalog_work.title, release_title=getattr(release, "title", None),
            indexer=getattr(release, "indexer", None), size=int(getattr(release, "size", 0) or 0),
            fmt=getattr(scored.info, "fmt", None), sab_category=cat, status="queued", grab_kind=kind,
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        client = SABnzbdClient(sab.base_url, sab.api_key)
        try:
            res = await client.add_url(url, category=cat, nzbname=getattr(release, "title", None))
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = f"grab failed: {exc}"
            db.commit()
            raise IntegrationError(str(exc)) from exc
        job.nzo_id = (res.get("nzo_ids") or [None])[0]
        db.commit()
        log.info("grab queued: %r → SAB %s (cat=%s)", job.title, job.nzo_id, cat)
        return job


async def auto_grab(db: Session, catalog_work: CatalogWork, *,
                    user_id: int | None = None, shelf_id: int | None = None) -> DownloadJob | None:
    """Match `catalog_work` against Prowlarr and, if a release clears the strict auto-grab gate,
    grab the best one. Returns the DownloadJob, or None if nothing was confidently matched."""
    from . import release_matcher as rm
    ranked = await rm.find_releases(db, catalog_work)
    best = next((s for s in ranked if s.auto_ok), None)
    if best is None:
        return None
    return await grab_release(db, catalog_work, best, user_id=user_id, shelf_id=shelf_id, kind="auto")


def _deepest_existing(path: str | None) -> str | None:
    """Walk up to the deepest directory that exists. SABnzbd may sanitize the job-folder name on
    disk (brackets/parens) or report a file path, so the exact storage path often won't exist —
    but its parent (the category drop zone) does."""
    p = (path or "").rstrip("/")
    if p and not os.path.isdir(p):
        p = os.path.dirname(p)
    while p and p not in ("/", "") and not os.path.isdir(p):
        p = os.path.dirname(p)
    return p or None


def _title_overlap(a: str, b: str) -> float:
    from .extract import norm_title
    ta, tb = set(norm_title(a).split()), set(norm_title(b).split())
    return len(ta & tb) / len(ta | tb) if (ta and tb) else 0.0


def _import_completed(db: Session, job: DownloadJob, sab: Integration) -> None:
    """Import a finished download: sync the watched folder covering its storage, then link the
    matching imported Work to the catalog book + the requester's library. Robust to SAB folder-name
    sanitization — we sync the drop zone and match the resulting Work to the book by title."""
    from ..library import add_to_library
    from .local_folder import sync_folder

    local_dir = map_path(job.storage_path, _path_mappings(sab))
    job_subdir = local_dir.rstrip("/") if (local_dir and os.path.isdir(local_dir)) else None
    # Watch the STABLE drop zone, not the transient per-job folder: when the job folder exists, the
    # drop zone is its parent; when SAB sanitized the name away, the deepest existing ancestor is it.
    books_root = (os.path.dirname(job_subdir) or job_subdir) if job_subdir else _deepest_existing(local_dir)
    if not books_root:
        job.status = "failed"
        job.error = f"completed download not visible to Shelf (path {local_dir!r})"
        db.commit()
        log.warning("import failed (path not visible): %s", local_dir)
        return

    folder = ensure_watched_folder(db, books_root)
    if folder is not None:
        try:
            sync_folder(db, folder)
        except Exception:  # noqa: BLE001
            log.exception("folder sync during import failed")

    src = _local_source(db)
    # Prefer files under the exact job subdir (if it survived on disk); else match across the drop
    # zone. When matching the zone, key off the on-disk FILENAME vs the RELEASE name (very specific —
    # avoids linking an older/other book already in the zone), gated by a book-title sanity check.
    scope = (job_subdir + "/%") if job_subdir else (books_root.rstrip("/") + "/%")
    candidates = db.scalars(
        select(Work).where(Work.source_id == src.id, Work.local_path.like(scope))
    ).all()
    work = None
    if job_subdir and candidates:
        work = max(candidates, key=lambda w: w.local_size or 0)
    elif candidates:
        from .extract import norm_title
        rel_toks = set(norm_title(job.release_title or job.title).split())
        best = 0.0
        for w in candidates:
            fname = os.path.basename(w.local_path or "")
            ftoks = set(norm_title(fname).split())
            if not rel_toks or not ftoks:
                continue
            ov = len(rel_toks & ftoks) / len(rel_toks | ftoks)
            # filename must match the release AND the work's title must match the book.
            if ov > best and _title_overlap(job.title, w.title or "") >= 0.5:
                best, work = ov, w
        if best < 0.5:
            work = None
    if work is None:
        job.status = "failed"
        job.error = f"download imported but no file matched the title in {books_root!r}"
        db.commit()
        log.warning("import found no matching work under %s", books_root)
        return

    job.work_id = work.id
    job.status = "imported"
    job.completed_at = _utcnow()
    cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
    if cw is not None and cw.hooked_work_id is None:
        cw.hooked_work_id = work.id
    if job.user_id:
        try:
            add_to_library(db, job.user_id, work.id, shelf_id=job.target_shelf_id)
        except Exception:  # noqa: BLE001 — shelf placement must not undo a durable import
            db.rollback()
            log.exception("add_to_library failed for job %s", job.id)
            job.work_id = work.id
            job.status = "imported"
            job.completed_at = _utcnow()
            if cw is not None and cw.hooked_work_id is None:
                cw.hooked_work_id = work.id
    db.commit()
    log.info("imported %r → work %s", job.title, work.id)
    _notify_import(db, job, work)


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

        imported = failed = 0
        for job in jobs:
            nzo = job.nzo_id
            if nzo and nzo in queue:
                if job.status != "downloading":
                    job.status = "downloading"
                    db.commit()
                continue
            if nzo and nzo in history:
                h = history[nzo]
                st = (h.status or "").lower()
                if st == "completed":
                    job.storage_path = h.storage  # SAB-reported path; mapped to local at import
                    _import_completed(db, job, sab)
                    if job.status == "imported":
                        imported += 1
                    elif job.status == "failed":
                        failed += 1
                elif st == "failed":
                    job.status = "failed"
                    job.error = h.fail_message or "download failed"
                    db.commit()
                    failed += 1
                # else still post-processing (extracting/verifying) → leave as downloading
                continue
            # Not in queue or history: SAB no longer knows it. Fail it once it's clearly stale.
            if _utcnow() - job.created_at > _STALE_AFTER:
                job.status = "failed"
                job.error = "SABnzbd no longer tracks this download"
                db.commit()
                failed += 1
        return {"active": len(jobs), "imported": imported, "failed": failed}
    finally:
        _poll_lock.release()
