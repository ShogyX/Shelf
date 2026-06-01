"""EPUB export + Send-to-Kindle delivery."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..epub_export import EpubChapter, build_epub
from ..kindle import resolve_smtp, send_document, smtp_configured
from ..models import Chapter, ChapterContent, UserSettings, Work
from ..schemas import SendToKindleIn, SendToKindleOut

router = APIRouter()
settings = get_settings()


def _smtp_cfg(db: Session):
    us = db.scalar(select(UserSettings).limit(1))
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
    db: Session = Depends(get_db),
) -> Response:
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    epub_bytes, filename, _ = _make_epub(db, work, start, limit)
    return Response(
        content=epub_bytes,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/kindle/status")
def kindle_status(db: Session = Depends(get_db)) -> dict:
    return {"smtp_configured": smtp_configured(_smtp_cfg(db))}


@router.post("/works/{work_id}/send-to-kindle", response_model=SendToKindleOut)
def send_to_kindle(
    work_id: int, payload: SendToKindleIn, db: Session = Depends(get_db)
) -> SendToKindleOut:
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    cfg = _smtp_cfg(db)
    if not smtp_configured(cfg):
        raise HTTPException(503, "Email delivery is not configured (SMTP).")

    us = db.scalar(select(UserSettings).limit(1))
    to = (payload.to or payload.kindle_email or (us.kindle_email if us else None) or "").strip()
    if "@" not in to:
        raise HTTPException(400, "A recipient email address is required.")

    # Remember Kindle addresses for next time (don't clobber with personal emails).
    if us is None:
        us = UserSettings(theme="system", reader_prefs={})
        db.add(us)
    if to.lower().endswith("kindle.com"):
        us.kindle_email = to
    db.commit()

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
