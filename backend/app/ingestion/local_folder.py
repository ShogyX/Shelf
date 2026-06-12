"""Watched-local-folder sync.

Scans a mapped directory, importing each supported file as a Work and keeping it
in sync with the filesystem:

  * new file            -> create a Work + chapters
  * changed file (mtime/size differs) -> re-parse + replace chapters
  * removed file        -> delete the Work

``upsert_media_work`` is the shared bridge from a parsed file to a Work, reused by
the local-import upload endpoint so EPUB/PDF/CBZ uploads behave identically.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..covers import save_cover
from ..models import Chapter, Source, WatchedFolder, Work
from .base import RawChapter
from .engine import ensure_source, store_chapter_content
from .media import ParsedMedia, is_supported, parse_media

log = logging.getLogger("shelf.local_folder")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def upsert_media_work(
    db: Session,
    src: Source,
    *,
    source_work_ref: str,
    parsed: ParsedMedia,
    cover_key: str,
    local_path: str | None = None,
    local_mtime: float | None = None,
    local_size: int | None = None,
) -> Work:
    """Create or replace a Work (and its chapters) from parsed media."""
    cover_url: str | None = None
    if parsed.cover:
        try:
            cover_url = save_cover(cover_key, parsed.cover[0], parsed.cover[1])
        except Exception:
            cover_url = None

    work = db.scalar(
        select(Work).where(Work.source_id == src.id, Work.source_work_ref == source_work_ref)
    )
    if work is None:
        # insert_or_reuse: the download-import path and a concurrent folder sync can both reach
        # here for the same just-promoted file; uq_work_source_ref makes the loser adopt the
        # winner's row instead of creating a duplicate Work. The new row needs its NOT NULL title
        # set NOW (it's flushed inside the savepoint); the real title is re-applied just below.
        from ..db import insert_or_reuse
        work, _created = insert_or_reuse(
            db, Work(source_id=src.id, source_work_ref=source_work_ref,
                     title=parsed.title or (local_path or source_work_ref)),
            select(Work).where(Work.source_id == src.id,
                               Work.source_work_ref == source_work_ref))

    work.title = parsed.title or (local_path or source_work_ref)
    work.author = parsed.author
    work.description = parsed.description
    if cover_url:
        work.cover_url = cover_url
    work.language = parsed.language or "en"
    work.status = "complete"
    work.hooked = False
    work.media_kind = parsed.kind
    work.total_chapters_known = len(parsed.chapters)
    work.local_path = local_path
    work.local_mtime = local_mtime
    work.local_size = local_size
    db.commit()
    db.refresh(work)

    # Replace chapters wholesale (cheap for local media; keeps sync simple + correct).
    for ch in list(work.chapters):
        db.delete(ch)
    db.commit()

    for pc in parsed.chapters:
        ch = Chapter(
            work_id=work.id,
            source_chapter_ref=f"local:{pc.index}",
            index=pc.index,
            title=pc.title,
            fetch_status="pending",
        )
        db.add(ch)
        db.flush()
        store_chapter_content(db, ch, RawChapter(title=pc.title, body=pc.body_html, fmt="html"))
    db.commit()
    db.refresh(work)
    return work


# Per-folder locks so the debounced watcher thread and the periodic rescan can't sync the SAME
# folder concurrently (which would double-place / double-email a freshly-discovered work and could
# poison a session with a duplicate-placement IntegrityError).
_folder_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _folder_lock(folder_id: int) -> threading.Lock:
    with _locks_guard:
        return _folder_locks.setdefault(folder_id, threading.Lock())


def _iter_files(root: str, recursive: bool):
    if recursive:
        # followlinks=False: don't let a symlink inside the folder escape to other paths.
        for dirpath, _dirs, files in os.walk(root, followlinks=False):
            for name in files:
                if is_supported(name):
                    yield os.path.join(dirpath, name)
    else:
        try:
            for name in os.listdir(root):
                full = os.path.join(root, name)
                if os.path.isfile(full) and is_supported(name):
                    yield full
        except OSError:
            return


def _send_book(db: Session, work: Work, delivery: dict | None, to: str | None, label: str) -> None:
    """Best-effort: email a discovered book (whole work) to `to`. Never raises."""
    from ..kindle import app_smtp, send_document, smtp_configured
    from ..routers.delivery import gather_epub

    to = (to or "").strip()
    if not to:
        return
    if (work.media_kind or "text") == "comic":
        return  # comics ship as CBZ via the manual Send action; an EPUB of pages won't render
    cfg = app_smtp(db)  # global (admin) SMTP server; `to` is the per-user/shelf recipient
    if not smtp_configured(cfg):
        return
    built = gather_epub(db, work, 1, None)
    if built is None:
        return
    epub_bytes, filename, _n, _last = built
    try:
        send_document(cfg, to_email=to, subject=f"{work.title}",
                      body=f"{work.title} — added to your shelf, sent from Shelf.",
                      attachment=epub_bytes, filename=filename)
    except Exception:  # noqa: BLE001 — a failed send must not break the folder sync
        log.exception("%s send failed for work %s", label, work.id)


def _fire_shelf_events(db: Session, folder: WatchedFolder, work_ids: list[int]) -> None:
    """Fire a shelf's automation events for newly-discovered works: push / Kindle / email,
    per the shelf's toggles + the owner's delivery settings. Best-effort."""
    from ..models import Bookshelf, UserSettings
    shelf = db.get(Bookshelf, folder.shelf_id) if folder.shelf_id else None
    if shelf is None:
        return
    us = db.scalar(select(UserSettings).where(UserSettings.user_id == folder.user_id))
    apprise = (us.apprise_url if us else None) or ""
    delivery = us.delivery_config if us else None
    kindle = (us.kindle_email if us else None) or ""
    email_to = ((delivery or {}).get("email_to") or "") if isinstance(delivery, dict) else ""
    for wid in work_ids:
        work = db.get(Work, wid)
        if work is None:
            continue
        if shelf.notify_on_add and apprise.strip():
            from ..notify import notify
            try:
                notify(apprise.strip(), "Shelf", f'New on “{shelf.name}”: {work.title}')
            except Exception:  # noqa: BLE001
                log.exception("notify failed for work %s", wid)
        if shelf.auto_kindle:
            _send_book(db, work, delivery, kindle, "auto-kindle")
        if shelf.notify_email:
            _send_book(db, work, delivery, email_to, "shelf-email")


def sync_folder(db: Session, folder: WatchedFolder) -> dict:
    """Reconcile one watched folder with the filesystem, serialized per folder so the watcher and
    the periodic rescan can't run it concurrently."""
    with _folder_lock(folder.id):
        return _do_sync_folder(db, folder)


def _do_sync_folder(db: Session, folder: WatchedFolder) -> dict:
    """Reconcile one watched folder with the filesystem. Returns a small summary. When the folder is
    mapped to a bookshelf, newly-imported works are placed on that shelf and its automation events
    (push / Kindle / email) fire on discovery — EXCEPT on the very first scan after mapping, which
    baselines existing files silently (so mapping a populated folder doesn't email the whole backlog)."""
    from ..library import add_to_library
    from ..models import BookshelfItem

    src = ensure_source(db, _local_folder_adapter_cls())
    summary = {"added": 0, "updated": 0, "removed": 0, "errors": 0}

    if not os.path.isdir(folder.path):
        folder.last_error = "Folder not found"
        db.commit()
        return summary

    shelf_mapped = bool(folder.shelf_id and folder.user_id)
    # First scan after a shelf mapping (no prior scan) → place silently, don't fire send/notify.
    baseline = shelf_mapped and folder.last_scan_at is None
    # The operator stock dir may live INSIDE a watched library folder (e.g. .../Books/Stock). Stocked
    # files are shared, operator-managed Works — never a user's deliberate library content — so they
    # must NOT be auto-added to the mapped user's library even though this folder covers them.
    from .stock import get_stock_dir
    _sd = get_stock_dir(db)
    _stock_prefix = os.path.join(os.path.abspath(_sd), "") if _sd else None

    def _under_stock(p: str) -> bool:
        return bool(_stock_prefix and os.path.abspath(p).startswith(_stock_prefix))

    newly_placed: list[int] = []
    seen_paths: set[str] = set()
    for path in _iter_files(folder.path, folder.recursive):
        seen_paths.add(path)
        try:
            st = os.stat(path)
        except OSError:
            continue
        ref = f"localfolder:{folder.id}:{path}"
        existing = db.scalar(
            select(Work).where(Work.source_id == src.id, Work.source_work_ref == ref)
        )
        if (
            existing is not None
            and existing.local_mtime == st.st_mtime
            and existing.local_size == st.st_size
        ):
            continue  # unchanged
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            parsed = parse_media(data, os.path.basename(path))
            work = upsert_media_work(
                db, src,
                source_work_ref=ref,
                parsed=parsed,
                cover_key=f"folder-{folder.id}-{os.path.basename(path)}",
                local_path=path,
                local_mtime=st.st_mtime,
                local_size=st.st_size,
            )
            summary["updated" if existing else "added"] += 1
            if shelf_mapped and not _under_stock(path):
                # Place on the shelf (idempotent); fire events only the first time it's placed.
                already = db.scalar(select(BookshelfItem.id).where(
                    BookshelfItem.shelf_id == folder.shelf_id, BookshelfItem.work_id == work.id))
                add_to_library(db, folder.user_id, work.id, shelf_id=folder.shelf_id)
                if already is None:
                    newly_placed.append(work.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("failed importing %s: %s", path, exc)
            summary["errors"] += 1

    # Remove works whose files vanished from this folder.
    prefix = f"localfolder:{folder.id}:"
    for work in db.scalars(
        select(Work).where(Work.source_id == src.id, Work.source_work_ref.like(prefix + "%"))
    ).all():
        if work.local_path and work.local_path not in seen_paths:
            db.delete(work)
            summary["removed"] += 1
    db.commit()

    folder.file_count = len(seen_paths)
    folder.last_scan_at = _utcnow()
    folder.last_error = None
    db.commit()
    if newly_placed and not baseline:
        # Sync runs off the event loop (watcher thread / scheduler executor), so blocking sends
        # here are fine; each event is best-effort and won't abort the scan.
        try:
            _fire_shelf_events(db, folder, newly_placed)
        except Exception:  # noqa: BLE001
            log.exception("shelf events failed for folder %s", folder.id)
    return summary


def _local_folder_adapter_cls():
    from .base import registry

    return registry.get("local_folder")
