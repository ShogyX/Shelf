"""EPUB export + Send-to-Kindle delivery + bulk library download."""
from __future__ import annotations

import io
import re
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user, require_permission
from ..config import get_settings
from ..db import get_db
from ..epub_export import (
    EpubChapter,
    build_epub,
    build_kindle_comic_epub,
    extract_image_srcs,
    resolve_image_bytes,
)
from ..library import assert_work_access, in_library
from ..kindle import resolve_smtp, send_document, smtp_configured
from ..models import Bookshelf, BookshelfItem, Chapter, ChapterContent, User, UserSettings, Work
from ..schemas import BulkDownloadIn, SendToKindleIn, SendToKindleOut

router = APIRouter()
settings = get_settings()


def _user_settings(db: Session, user_id: int) -> UserSettings | None:
    return db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))


def _smtp_cfg(db: Session, user_id: int):
    us = _user_settings(db, user_id)
    return resolve_smtp(settings, us.delivery_config if us else None)


def _safe_filename(title: str) -> str:
    base = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "_")[:80] or "book"
    return base


def _gather(db: Session, work: Work, start: int, limit: int | None) -> list[EpubChapter]:
    q = (
        select(Chapter)
        .where(Chapter.work_id == work.id, Chapter.content_id.is_not(None), Chapter.index >= start)
        .order_by(Chapter.index)
    )
    if limit:
        q = q.limit(limit)
    out: list[EpubChapter] = []
    for ch in db.scalars(q).all():
        content = db.get(ChapterContent, ch.content_id)
        if content is None:
            continue
        out.append(EpubChapter(index=ch.index, title=ch.title, body_html=content.body))
    return out


def gather_epub(
    db: Session, work: Work, start: int, limit: int | None
) -> tuple[bytes, str, int, int] | None:
    """Build an EPUB of the work's fetched chapters from ``start``. Returns
    ``(bytes, filename, count, last_index)`` or ``None`` when nothing is fetched in range.
    Shared by the HTTP export and the auto-kindle scheduler tick (which must not raise)."""
    chapters = _gather(db, work, start, limit)
    if not chapters:
        return None
    last = chapters[-1].index
    epub_bytes = build_epub(
        title=work.title,
        author=work.author,
        language=work.language or "en",
        cover_url=work.cover_url,
        chapters=chapters,
        identifier=f"shelf-{work.id}-{start}-{last}",
    )
    suffix = "" if start == 1 and not limit else f"_ch{start}-{last}"
    filename = f"{_safe_filename(work.title)}{suffix}.epub"
    return epub_bytes, filename, len(chapters), last


def gather_cbz(
    db: Session, work: Work, start: int, limit: int | None
) -> tuple[bytes, str, int] | None:
    """Build a CBZ (comic archive) of a comic/manga/webtoon work's page images, in reading
    order. CBZ is the universally-supported format for image content — an EPUB of webp pages
    won't render in most readers/Kindle. Returns ``(bytes, filename, page_count)`` or ``None``
    when no page images are available."""
    chapters = _gather(db, work, start, limit)
    if not chapters:
        return None
    buf = io.BytesIO()
    cache: dict[str, tuple[bytes, str, str] | None] = {}
    pages = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for ch in chapters:
            for j, src in enumerate(extract_image_srcs(ch.body_html), 1):
                got = resolve_image_bytes(src, cache)
                if got is None:
                    continue
                data, ext, _mime = got
                pages += 1
                # Chapter- then page-padded so a CBZ reader's filename sort = reading order.
                zf.writestr(f"{ch.index:05d}_{j:04d}.{ext}", data)
    if pages == 0:
        return None
    return buf.getvalue(), f"{_safe_filename(work.title)}.cbz", pages


_KINDLE_MAX_BYTES = 45 * 1024 * 1024  # stay under Amazon's ~50MB email-attachment cap


def gather_kindle_comic(
    db: Session, work: Work, start: int, limit: int | None
) -> tuple[bytes, str, int] | None:
    """Build a Kindle-ready fixed-layout EPUB of a comic's pages. Returns ``(bytes, filename,
    page_count)`` or ``None`` when no page images are available; raises 413 if it exceeds the
    email cap."""
    chapters = _gather(db, work, start, limit)
    if not chapters:
        return None
    cache: dict[str, tuple[bytes, str, str] | None] = {}
    images: list[bytes] = []
    for ch in chapters:
        for src in extract_image_srcs(ch.body_html):
            got = resolve_image_bytes(src, cache)
            if got is not None:
                images.append(got[0])
    if not images:
        return None
    built = build_kindle_comic_epub(
        title=work.title, author=work.author, language=work.language or "en",
        identifier=f"shelf-{work.id}-{start}", images=images,
    )
    if built is None:
        return None
    epub_bytes, pages = built
    if len(epub_bytes) > _KINDLE_MAX_BYTES:
        raise HTTPException(
            413, "Too large to email to Kindle (~50MB cap) — send a smaller chapter range."
        )
    return epub_bytes, f"{_safe_filename(work.title)}.epub", pages


def gather_export(
    db: Session, work: Work, start: int, limit: int | None
) -> tuple[bytes, str, int] | None:
    """Build the right artifact for a work's media kind: CBZ for comics, EPUB for text.
    Returns ``(bytes, filename, count)`` or ``None`` when nothing is downloadable yet."""
    if (work.media_kind or "text") == "comic":
        return gather_cbz(db, work, start, limit)
    built = gather_epub(db, work, start, limit)
    if built is None:
        return None
    epub_bytes, filename, count, _last = built
    return epub_bytes, filename, count


def _make_epub(db: Session, work: Work, start: int, limit: int | None) -> tuple[bytes, str, int]:
    chapters = _gather(db, work, start, limit)
    if not chapters:
        raise HTTPException(409, "No fetched chapters to export in that range.")
    last = chapters[-1].index
    epub_bytes = build_epub(
        title=work.title,
        author=work.author,
        language=work.language or "en",
        cover_url=work.cover_url,
        chapters=chapters,
        identifier=f"shelf-{work.id}-{start}-{last}",
    )
    suffix = "" if start == 1 and not limit else f"_ch{start}-{last}"
    filename = f"{_safe_filename(work.title)}{suffix}.epub"
    return epub_bytes, filename, len(chapters)


@router.get("/works/{work_id}/export.epub")
def export_epub(
    work_id: int,
    start: int = Query(1, ge=1),
    limit: int | None = Query(None, ge=1),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)  # library isolation: members (or admin) only
    epub_bytes, filename, _ = _make_epub(db, work, start, limit)
    return Response(
        content=epub_bytes,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/works/{work_id}/download")
def download_work(
    work_id: int,
    start: int = Query(1, ge=1),
    limit: int | None = Query(None, ge=1),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Single-work download in the format that fits its contents: CBZ for comic/manga/webtoon
    page images, EPUB for text."""
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)  # library isolation: members (or admin) only
    built = gather_export(db, work, start, limit)
    if built is None:
        raise HTTPException(409, "No downloadable content in that range.")
    data, filename, _ = built
    media = "application/zip" if filename.endswith(".cbz") else "application/epub+zip"
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_BULK_MAX = 100  # cap one download so a huge library can't build thousands of EPUBs at once


def _bulk_zip(db: Session, user: User, work_ids: list[int], shelf_id: int | None) -> Response:
    """Build a ZIP of EPUBs for the given works (and/or a whole shelf). Only works in the caller's
    library (or any, for admins) are included; works with no fetched chapters are skipped."""
    work_ids = list(dict.fromkeys(work_ids or []))  # de-dup, preserve order
    if shelf_id is not None:
        shelf = db.get(Bookshelf, shelf_id)
        if shelf is None or shelf.user_id != user.id:
            raise HTTPException(404, "Bookshelf not found")
        shelf_works = db.scalars(
            select(BookshelfItem.work_id).where(BookshelfItem.shelf_id == shelf_id)
        ).all()
        for wid in shelf_works:
            if wid not in work_ids:
                work_ids.append(wid)
    if not work_ids:
        raise HTTPException(400, "Select at least one work or a shelf to download.")
    if len(work_ids) > _BULK_MAX:
        raise HTTPException(413, f"Too many works at once (max {_BULK_MAX}). Narrow your selection.")

    buf = io.BytesIO()
    included = 0
    seen_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for wid in work_ids:
            work = db.get(Work, wid)
            if work is None:
                continue
            if user.role != "admin" and not in_library(db, user.id, wid):
                continue  # silently skip works not in the caller's library
            built = gather_export(db, work, 1, None)  # CBZ for comics, EPUB for text
            if built is None:
                continue  # nothing fetched yet
            epub_bytes, filename, _ = built
            # Avoid duplicate names within the archive.
            name = filename
            n = 2
            while name in seen_names:
                name = filename.replace(".epub", f"_{n}.epub")
                n += 1
            seen_names.add(name)
            zf.writestr(name, epub_bytes)
            included += 1
    if included == 0:
        raise HTTPException(409, "None of the selected works have downloadable chapters yet.")
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="shelf-library.zip"'},
    )


@router.post("/library/download")
def bulk_download(
    payload: BulkDownloadIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    """JSON-body bulk download (programmatic clients / API)."""
    return _bulk_zip(db, user, payload.work_ids or [], payload.shelf_id)


@router.get("/library/download")
def bulk_download_get(
    ids: str | None = Query(None, description="comma-separated work ids"),
    shelf_id: int | None = Query(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Same as the POST, but driven by query params so the browser can fetch it via a plain
    ``<a download>`` link. That keeps the download inside the user's click gesture — programmatic
    blob downloads after an async fetch are silently dropped by iOS Safari."""
    work_ids = [int(x) for x in (ids or "").split(",") if x.strip().lstrip("-").isdigit()]
    return _bulk_zip(db, user, work_ids, shelf_id)


@router.get("/kindle/status")
def kindle_status(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    return {"smtp_configured": smtp_configured(_smtp_cfg(db, user.id))}


@router.post("/works/{work_id}/send-to-kindle", response_model=SendToKindleOut, dependencies=[Depends(require_permission("send.kindle"))])
def send_to_kindle(
    work_id: int, payload: SendToKindleIn,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> SendToKindleOut:
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)  # library isolation: members (or admin) only
    cfg = _smtp_cfg(db, user.id)
    if not smtp_configured(cfg):
        raise HTTPException(503, "Email delivery is not configured (SMTP).")

    us = _user_settings(db, user.id)
    to = (payload.to or payload.kindle_email or (us.kindle_email if us else None) or "").strip()
    if "@" not in to:
        raise HTTPException(400, "A recipient email address is required.")

    # Remember Kindle addresses for next time (don't clobber with personal emails).
    if us is None:
        us = UserSettings(user_id=user.id, theme="system", reader_prefs={})
        db.add(us)
    if to.lower().endswith("kindle.com"):
        us.kindle_email = to
    db.commit()

    # Comics go as a fixed-layout image EPUB (Kindle can't read CBZ/WebP); text as a normal EPUB.
    if (work.media_kind or "text") == "comic":
        built = gather_kindle_comic(db, work, payload.start, payload.limit)
        if built is None:
            raise HTTPException(409, "No comic pages to send in that range.")
        epub_bytes, filename, n = built
    else:
        epub_bytes, filename, n = _make_epub(db, work, payload.start, payload.limit)
    try:
        send_document(
            cfg,
            to_email=to,
            subject=work.title,
            body=f"{work.title} — sent from Shelf.",
            attachment=epub_bytes,
            filename=filename,
        )
    except Exception as exc:  # SMTP/auth/network
        raise HTTPException(502, f"Failed to send: {exc}") from exc
    return SendToKindleOut(sent=True, chapters=n, to=to)
