from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..ingestion.extract import chapter_ref_number
from ..ingestion.textclean import clean_chapter_html
from ..library import assert_work_access
from ..models import Chapter, ChapterContent, User, Work
from ..schemas import ChapterListOut, ChapterOut, ReaderContentOut

router = APIRouter()


def _clean_chapter_content(db: Session, chapter: Chapter) -> bool:
    """Rewrite a chapter's stored HTML into the readable form (de-censored + reflowed paragraphs).
    No-op for already-clean content. Returns True if it changed. The source-content checksum
    (``raw_checksum``) is left untouched, so a later refresh sees the SOURCE unchanged and won't
    re-dirty the cleaned copy."""
    import hashlib

    from ..sanitize import count_words

    if chapter.content_id is None:
        return False
    content = db.get(ChapterContent, chapter.content_id)
    if content is None or not content.body:
        return False
    cleaned = clean_chapter_html(content.body)
    if cleaned == content.body:
        return False
    content.body = cleaned
    content.word_count = count_words(cleaned)
    content.checksum = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    return True


@router.get("/works/{work_id}/chapters", response_model=ChapterListOut)
def list_chapters(
    work_id: int,
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ChapterListOut:
    if db.get(Work, work_id) is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)  # library isolation: members (or admin) only
    total = db.scalar(select(func.count(Chapter.id)).where(Chapter.work_id == work_id)) or 0
    rows = db.scalars(
        select(Chapter)
        .where(Chapter.work_id == work_id)
        .order_by(Chapter.index)
        .limit(limit)
        .offset(offset)
    ).all()
    items = [
        ChapterOut(
            id=c.id, work_id=c.work_id, index=c.index,
            number=chapter_ref_number(c.title, c.source_chapter_ref, c.index),
            title=c.title, fetch_status=c.fetch_status, has_content=c.content_id is not None,
        )
        for c in rows
    ]
    return ChapterListOut(items=items, total=total, limit=limit, offset=offset)


@router.get("/chapters/{chapter_id}", response_model=ReaderContentOut)
def get_chapter(
    chapter_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> ReaderContentOut:
    """Reader-content endpoint (Stage 3): sanitized HTML + resolved nav links."""
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(404, "Chapter not found")
    assert_work_access(db, user, chapter.work_id)  # library isolation: members (or admin) only
    if chapter.content_id is None:
        raise HTTPException(409, "Chapter content not fetched yet")
    content = db.get(ChapterContent, chapter.content_id)

    prev_ch = db.scalar(
        select(Chapter.id)
        .where(Chapter.work_id == chapter.work_id, Chapter.index < chapter.index,
               Chapter.content_id.is_not(None))
        .order_by(Chapter.index.desc())
        .limit(1)
    )
    next_ch = db.scalar(
        select(Chapter.id)
        .where(Chapter.work_id == chapter.work_id, Chapter.index > chapter.index,
               Chapter.content_id.is_not(None))
        .order_by(Chapter.index)
        .limit(1)
    )
    from .imgproxy import rewrite_hotlinked

    return ReaderContentOut(
        chapter_id=chapter.id,
        work_id=chapter.work_id,
        index=chapter.index,
        title=chapter.title,
        # Route hotlink-protected comic images (e.g. webtoons) through the Referer proxy.
        html=rewrite_hotlinked(content.body if content else ""),
        word_count=content.word_count if content else 0,
        prev_chapter_id=prev_ch,
        next_chapter_id=next_ch,
    )


@router.post("/chapters/{chapter_id}/clean", response_model=ReaderContentOut)
def clean_chapter(
    chapter_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> ReaderContentOut:
    """Clean up THIS chapter's text (de-censor + reflow into readable paragraphs) and return the
    refreshed reader content. Idempotent — already-clean chapters are returned unchanged."""
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(404, "Chapter not found")
    assert_work_access(db, user, chapter.work_id)  # library isolation: members (or admin) only
    if chapter.content_id is None:
        raise HTTPException(409, "Chapter content not fetched yet")
    _clean_chapter_content(db, chapter)
    db.commit()
    return get_chapter(chapter_id, user, db)


@router.post("/works/{work_id}/clean")
def clean_work(
    work_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    """Clean up EVERY fetched chapter of a title in one pass. Returns how many were changed."""
    if db.get(Work, work_id) is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)  # library isolation: members (or admin) only
    chapters = db.scalars(
        select(Chapter).where(Chapter.work_id == work_id, Chapter.content_id.is_not(None))
    ).all()
    changed = sum(1 for c in chapters if _clean_chapter_content(db, c))
    db.commit()
    return {"cleaned": changed, "total": len(chapters)}
