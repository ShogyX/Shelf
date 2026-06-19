"""Shared post-download IMPORT CORE — the verify → promote → import → link → notify pipeline that
every acquisition route funnels a finished download through.

Three download routes share this core:
  * downloads.py  (usenet / SABnzbd)   — its ``poll_tick`` calls ``import_completed`` on completion.
  * torrents.py   (qBittorrent)        — its ``_finish`` calls ``import_completed`` after the VT gate.
  * libgen.py     (open-library HTTP)  — uses ``promote`` + ``notify_import`` around its own
                                         file-verify path (``_import_file``).

The pipeline reads ALL of its configuration off the ``Integration`` row it is handed (the SABnzbd or
qBittorrent integration), so the same code serves usenet and torrent downloads unchanged.

PUBLIC API
----------
``import_completed(db, job, integ) -> str``
    Verify a finished STAGING download and, on success, promote the verified file into the library,
    import it, and link it to the catalog book + requester's library. Sets ``job.status`` and returns
    one of the VERDICT_* values below.

``promote(src_file, lib_dir, want_title) -> str | None``
    Move a verified file out of staging into the library under a per-book subfolder. Returns the final
    path (the file in place when no library dir is configured), or None on a move error.

``notify_import(db, job, work) -> None``
    Notify the requesting user that an auto-fetched title finished downloading + landed in their
    library. No-op for jobs with no requesting user (e.g. operator STOCK fetches).

VERDICT PROTOCOL
----------------
``import_completed`` returns a verdict string the caller's poll loop switches on. The values are the
documented contract between the import core and the three routes' poll ticks:

  * VERDICT_IMPORTED ("imported") — DONE. The file verified, was promoted + imported, and is linked to
        the catalog book + requester library. ``job.status == "imported"``. The caller cleans up
        staging (when the file was MOVED out) and propagates the import to any piggybacking followers.
  * VERDICT_RETRY    ("retry")    — the content did NOT verify as the requested book, OR it verified
        but produced no importable Work. The release is wrong/unusable; the caller should mark it
        broken and ADVANCE its candidate cascade to the next release (usenet), or fail + mark broken
        (torrent). ``job.status == "retry"``.
  * VERDICT_WAIT     ("wait")     — the completed download is not visible on shared storage yet
        (mount/NFS lag, SAB still finalizing). Transient: NOT a verify failure. The caller leaves the
        job active and re-polls next tick. ``job.status == "downloading"``. (Bounded by the stale
        window: a download that never becomes visible eventually flips to VERDICT_FAILED.)
  * VERDICT_FAILED   ("failed")   — TERMINAL placement failure: the file verified but could not be
        promoted into the library, or it never became visible within the stale window. ``job.status
        == "failed"``. The caller notifies the user of a permanent failure (this path does not go
        through the cascade-advance, which is where the user is normally told).

These four strings are the EXACT literals the routes compare against; they are exported as constants
so call sites can reference the protocol by name rather than by bare string.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Bookshelf, CatalogWork, DownloadJob, Integration, Work
from . import language, ledger, verify

log = logging.getLogger("shelf.downloads")

# --- import-core verdict protocol (see module docstring for the contract) --------------------------
VERDICT_IMPORTED = "imported"
VERDICT_RETRY = "retry"
VERDICT_WAIT = "wait"
VERDICT_FAILED = "failed"

# Active (pollable) job statuses, shared by every route's poll tick + dedup query.
ACTIVE_STATUSES = ("queued", "downloading", "completed", "retry")
# Most releases a single book will try (download+verify) before giving up. Shared cascade cap.
CANDIDATE_CAP = 6
# A completed download that never becomes visible within this window is failed, not waited on forever.
_STALE_AFTER = timedelta(hours=12)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime:
    """SQLite returns naive datetimes (no tz stored); normalize to UTC-aware so arithmetic against
    _utcnow() doesn't raise 'can't subtract offset-naive and offset-aware'."""
    if dt is None:
        return _utcnow()
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


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


def _path_mappings(integ: Integration) -> list[dict]:
    return (integ.config or {}).get("path_mappings") or []


def _library_dir(integ: Integration) -> str | None:
    """Shelf-local directory verified downloads are PROMOTED into (the watched library). When unset,
    downloads are imported in place from where SAB dropped them (no separate staging)."""
    p = ((integ.config or {}).get("library_path") or "").strip()
    return p or None


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


# Serialize promotions per destination path: the SAB-poll path (under _poll_lock) and the libgen
# import path (NOT under _poll_lock) can promote a verified file for the SAME book into the same
# lib_dir/<title>/<basename> concurrently — remove-then-move interleavings clobbered or spuriously
# failed a verified download. One process-wide lock is enough (promotions are rare + fast).
_promote_lock = threading.Lock()


def promote(src_file: str, lib_dir: str | None, want_title: str) -> str | None:
    """Move a verified file out of staging into the library under a per-book subfolder. Returns the
    final path. When no library dir is configured, returns the file in place (import without
    staging). None on a move error.

    ATOMIC against concurrent promoters: the file is staged to a unique temp sibling in the
    destination dir, then os.replace()d into place (atomic on POSIX) — never remove-then-move,
    which had a window where a concurrent promote/import saw no file or half a file."""
    if not lib_dir:
        return src_file
    try:
        dest_dir = os.path.join(lib_dir, _safe_name(want_title) or "book")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(src_file))
        if os.path.abspath(src_file) == os.path.abspath(dest):
            return dest
        with _promote_lock:
            tmp = dest + f".promote-{os.getpid()}-{threading.get_ident()}.part"
            try:
                shutil.move(src_file, tmp)      # cross-device-safe staging next to the dest
                os.replace(tmp, dest)           # atomic swap — overwrites any prior copy in one step
            finally:
                if os.path.exists(tmp):         # failed between move and replace → don't leak
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        return dest
    except OSError:
        log.exception("promote failed: %s → %s", src_file, lib_dir)
        return None


def import_completed(db: Session, job: DownloadJob, sab: Integration) -> str:
    """Verify a finished STAGING download, and on success promote the verified file into the library,
    import it, and link it to the catalog book + requester's library. Returns the verdict and sets
    job.status: 'imported' (done), 'retry' (verify/visibility failed → cascade should advance),
    'failed' (verified but couldn't be placed). Files in staging are not touched by any other
    automation until they're confirmed correct here."""
    # Late import breaks the import cycle (downloads imports this module) AND preserves the
    # downloads-level monkeypatch seams: ensure_watched_folder / _local_source / _apply_series /
    # _notify_import / _verify_floor / _target_dir are resolved through the downloads module object so
    # a test that patches dl.<name> sees its patch honoured here.
    from . import downloads
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
            return VERDICT_FAILED
        job.status = "downloading"
        job.error = f"awaiting visibility of completed download ({staging_local!r})"
        db.commit()
        log.info("import: path not visible yet, will re-poll: %s", staging_local)
        return VERDICT_WAIT

    # Turn any Kindle-format files (mobi/azw3) in the download into EPUB first, so a release that only
    # came as mobi can still be verified + imported (no-op when no converter / no such files).
    try:
        from . import convert
        if convert.convert_in_dir(staging_dir):
            log.info("converted mobi/azw3 → epub in %s", staging_dir)
    except Exception:  # noqa: BLE001 — conversion is best-effort
        log.exception("mobi conversion pass failed")

    # Look INSIDE the download: only content that really is the requested book — in the requested
    # language — is accepted. Pass the work's alternate titles + ISBNs (already persisted on extra by
    # matchmeta/enrichment) so a book grabbed under its native/romaji title, or identifiable by ISBN,
    # verifies against the right signals instead of being failed on a single English title.
    cw_extra = (cw.extra or {}) if (cw and isinstance(cw.extra, dict)) else {}
    want_titles = [t for t in (cw_extra.get("alt_titles") or []) if t] or None
    want_isbns = cw_extra.get("isbn") or None
    vr = verify.verify_download(staging_dir, want_title, want_author,
                                min_confidence=downloads._verify_floor(sab), want_language=want_language,
                                want_titles=want_titles, want_isbns=want_isbns)
    if not vr.ok or not vr.path:
        job.status = "retry"
        job.error = f"content mismatch ({vr.reason}; conf {vr.confidence:.2f})"
        db.commit()
        log.info("verify FAILED %r: %s (conf %.2f)", want_title, vr.reason, vr.confidence)
        return VERDICT_RETRY

    lib = downloads._target_dir(db, sab, job)
    promoted = promote(vr.path, lib, want_title)
    if not promoted:
        job.status = "failed"
        job.error = "verified but could not promote into the library"
        db.commit()
        return VERDICT_FAILED

    # Import from the library (or, with no library configured, from the staging dir in place) and
    # link by the EXACT promoted path — deterministic, no fragile title-overlap matching.
    import_root = lib or staging_dir
    folder = downloads.ensure_watched_folder(db, import_root)
    if folder is not None:
        try:
            sync_folder(db, folder)
        except Exception:  # noqa: BLE001
            log.exception("folder sync during import failed")

    src = downloads._local_source(db)
    work = db.scalar(select(Work).where(Work.source_id == src.id, Work.local_path == promoted))
    if work is None:  # fall back to a same-dir filename match (path normalization differences)
        base = os.path.basename(promoted)
        same_dir = db.scalars(select(Work).where(
            Work.source_id == src.id,
            Work.local_path.like(os.path.dirname(promoted).rstrip("/") + "/%"),
        )).all()
        work = next((w for w in same_dir if os.path.basename(w.local_path or "") == base), None)
    if work is None:
        # Promoted but no Work (unsupported/odd file the verify metadata-read didn't catch). Remove
        # the orphan and return "retry" so the cascade marks this release broken and tries the next
        # candidate — and a future re-search won't re-grab the discarded one.
        # Deletion happens UNDER the folder's sync lock with a final re-check: a concurrent
        # watchdog/periodic sync may be importing this exact file right now — deleting it out
        # from under that sync stranded a Work pointing at a vanished file (and discarded a
        # perfectly good verified download).
        from .local_folder import _folder_lock
        lock = _folder_lock(folder.id) if folder is not None else threading.Lock()
        with lock:
            work = db.scalar(select(Work).where(
                Work.source_id == src.id, Work.local_path == promoted))
            if work is None:
                try:
                    if os.path.isfile(promoted):
                        os.remove(promoted)
                        d = os.path.dirname(promoted)
                        if os.path.isdir(d) and not os.listdir(d):
                            os.rmdir(d)
                except OSError:
                    pass
                job.status = "retry"
                job.error = f"import produced no Work (unimportable file) for {promoted!r}"
                db.commit()
                log.warning("import produced no Work for %s → retry next candidate", promoted)
                return VERDICT_RETRY
        # The re-check found it — a concurrent sync owned the import; fall through as success.

    job.work_id = work.id
    job.verified = True
    job.status = "imported"
    job.error = None  # clear any stale transient-stall note (e.g. "SABnzbd unreachable")
    job.completed_at = _utcnow()
    if cw is not None and cw.hooked_work_id is None:
        cw.hooked_work_id = work.id
    downloads._apply_series(work, cw)
    if job.user_id:
        try:
            add_to_library(db, job.user_id, work.id, shelf_id=job.target_shelf_id)
        except Exception:  # noqa: BLE001 — shelf placement must not undo a durable import
            db.rollback()
            log.exception("add_to_library failed for job %s", job.id)
            job.work_id = work.id
            job.verified = True
            job.status = "imported"
            job.error = None
            job.completed_at = _utcnow()
            if cw is not None and cw.hooked_work_id is None:
                cw.hooked_work_id = work.id
            downloads._apply_series(work, cw)  # rollback above discarded the series tag set before add_to_library
    db.commit()
    if cw is not None:  # title obtained → clear any missing-content gate (Stage 1)
        ledger.mark_resolved(db, cw)
    log.info("imported (verified %.2f) %r → work %s", vr.confidence, job.title, work.id)
    if (job.grab_kind or "") == "stock":  # flip the StockItem to 'stocked' + hook the group
        from .stock import on_stock_imported
        on_stock_imported(db, job)
    else:
        downloads._notify_import(db, job, work)
    return VERDICT_IMPORTED


def notify_import(db: Session, job: DownloadJob, work: Work) -> None:
    """Notify the requesting user that an auto-fetched title finished downloading + landed in their
    library. Channel routing + per-event opt-in are handled by the notifications engine."""
    if not job.user_id:
        return
    from .. import notifications as notif
    shelf = db.get(Bookshelf, job.target_shelf_id) if job.target_shelf_id else None
    where = f' to "{shelf.name}"' if (shelf and shelf.user_id == job.user_id) else ""
    notif.dispatch_soon(db, "download.completed", user_id=job.user_id,
                        title="Download completed", body=f"{work.title} — added to your library{where}")
