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
        work = Work(source_id=src.id, source_work_ref=source_work_ref)
        db.add(work)

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


def _iter_files(root: str, recursive: bool):
    if recursive:
        for dirpath, _dirs, files in os.walk(root):
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


def sync_folder(db: Session, folder: WatchedFolder) -> dict:
    """Reconcile one watched folder with the filesystem. Returns a small summary."""
    src = ensure_source(db, _local_folder_adapter_cls())
    summary = {"added": 0, "updated": 0, "removed": 0, "errors": 0}

    if not os.path.isdir(folder.path):
        folder.last_error = "Folder not found"
        db.commit()
        return summary

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
            upsert_media_work(
                db, src,
                source_work_ref=ref,
                parsed=parsed,
                cover_key=f"folder-{folder.id}-{os.path.basename(path)}",
                local_path=path,
                local_mtime=st.st_mtime,
                local_size=st.st_size,
            )
            summary["updated" if existing else "added"] += 1
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
    return summary


def _local_folder_adapter_cls():
    from .base import registry

    return registry.get("local_folder")
