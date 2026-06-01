from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Chapter, ReadingState, Work
from ..schemas import ContinueItem, ProgressIn, ProgressOut

router = APIRouter()


def _continue_chapter(db: Session, work_id: int, last_chapter_id: int | None) -> int | None:
    """Resolve the next unread, fetched chapter (or the last one if all read)."""
    if last_chapter_id is not None:
        last = db.get(Chapter, last_chapter_id)
        if last is not None:
            nxt = db.scalar(
                select(Chapter.id)
                .where(Chapter.work_id == work_id, Chapter.index > last.index,
                       Chapter.content_id.is_not(None))
                .order_by(Chapter.index)
                .limit(1)
            )
            return nxt or last_chapter_id
    # No progress yet: first fetched chapter.
    return db.scalar(
        select(Chapter.id)
        .where(Chapter.work_id == work_id, Chapter.content_id.is_not(None))
        .order_by(Chapter.index)
        .limit(1)
    )


@router.post("/works/{work_id}/progress", response_model=ProgressOut)
def save_progress(work_id: int, payload: ProgressIn, db: Session = Depends(get_db)) -> ProgressOut:
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    chapter = db.get(Chapter, payload.last_chapter_id)
    if chapter is None or chapter.work_id != work_id:
        raise HTTPException(400, "Chapter does not belong to this work")

    state = db.scalar(select(ReadingState).where(ReadingState.work_id == work_id))
    if state is None:
        state = ReadingState(work_id=work_id)
        db.add(state)
    state.last_chapter_id = payload.last_chapter_id
    state.scroll_fraction = payload.scroll_fraction
    state.paragraph_index = payload.paragraph_index
    # chapters_read = number of distinct chapters up to and including current index.
    state.chapters_read = db.scalar(
        select(func.count(Chapter.id)).where(
            Chapter.work_id == work_id, Chapter.index <= chapter.index
        )
    ) or 0
    db.commit()
    db.refresh(state)
    return ProgressOut(
        work_id=work_id,
        last_chapter_id=state.last_chapter_id,
        scroll_fraction=state.scroll_fraction,
        paragraph_index=state.paragraph_index,
        chapters_read=state.chapters_read,
        continue_chapter_id=_continue_chapter(db, work_id, state.last_chapter_id),
    )


@router.get("/works/{work_id}/progress", response_model=ProgressOut)
def get_progress(work_id: int, db: Session = Depends(get_db)) -> ProgressOut:
    if db.get(Work, work_id) is None:
        raise HTTPException(404, "Work not found")
    state = db.scalar(select(ReadingState).where(ReadingState.work_id == work_id))
    last_id = state.last_chapter_id if state else None
    return ProgressOut(
        work_id=work_id,
        last_chapter_id=last_id,
        scroll_fraction=state.scroll_fraction if state else 0.0,
        paragraph_index=state.paragraph_index if state else 0,
        chapters_read=state.chapters_read if state else 0,
        continue_chapter_id=_continue_chapter(db, work_id, last_id),
    )


@router.get("/continue-reading", response_model=list[ContinueItem])
def continue_reading(limit: int = 12, db: Session = Depends(get_db)) -> list[ContinueItem]:
    """Recently-read works with a resume target, newest first (for the dashboard)."""
    states = db.scalars(
        select(ReadingState)
        .where(ReadingState.last_chapter_id.is_not(None))
        .order_by(ReadingState.updated_at.desc())
        .limit(limit)
    ).all()
    items: list[ContinueItem] = []
    for st in states:
        work = db.get(Work, st.work_id)
        chapter = db.get(Chapter, st.last_chapter_id) if st.last_chapter_id else None
        if work is None or chapter is None:
            continue
        total = db.scalar(select(func.count(Chapter.id)).where(Chapter.work_id == work.id)) or 0
        # Percent through the whole work: completed chapters + intra-chapter fraction.
        through = (chapter.index - 1) + min(1.0, max(0.0, st.scroll_fraction))
        percent = round(100 * through / total, 1) if total else 0.0
        items.append(
            ContinueItem(
                work_id=work.id,
                title=work.title,
                author=work.author,
                cover_url=work.cover_url,
                chapter_id=chapter.id,
                chapter_index=chapter.index,
                chapter_title=chapter.title,
                paragraph_index=st.paragraph_index,
                scroll_fraction=st.scroll_fraction,
                chapters_read=st.chapters_read,
                total_chapters=total,
                percent=min(100.0, percent),
                updated_at=st.updated_at,
            )
        )
    return items
