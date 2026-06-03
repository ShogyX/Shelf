from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..library import assert_work_access
from ..models import Chapter, ReadingState, User, Work
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
    return db.scalar(
        select(Chapter.id)
        .where(Chapter.work_id == work_id, Chapter.content_id.is_not(None))
        .order_by(Chapter.index)
        .limit(1)
    )


def get_state(db: Session, user_id: int, work_id: int) -> ReadingState | None:
    return db.scalar(
        select(ReadingState).where(
            ReadingState.work_id == work_id, ReadingState.user_id == user_id
        )
    )


def save_progress_for(db: Session, user_id: int, work_id: int, payload: ProgressIn) -> ProgressOut:
    """Core per-user progress write — reused by the API and shelfcli."""
    chapter = db.get(Chapter, payload.last_chapter_id)
    if chapter is None or chapter.work_id != work_id:
        raise HTTPException(400, "Chapter does not belong to this work")
    state = get_state(db, user_id, work_id)
    if state is None:
        state = ReadingState(work_id=work_id, user_id=user_id)
        db.add(state)
    state.last_chapter_id = payload.last_chapter_id
    state.scroll_fraction = payload.scroll_fraction
    state.paragraph_index = payload.paragraph_index
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


def get_progress_for(db: Session, user_id: int, work_id: int) -> ProgressOut:
    state = get_state(db, user_id, work_id)
    last_id = state.last_chapter_id if state else None
    return ProgressOut(
        work_id=work_id,
        last_chapter_id=last_id,
        scroll_fraction=state.scroll_fraction if state else 0.0,
        paragraph_index=state.paragraph_index if state else 0,
        chapters_read=state.chapters_read if state else 0,
        continue_chapter_id=_continue_chapter(db, work_id, last_id),
    )


@router.post("/works/{work_id}/progress", response_model=ProgressOut)
def save_progress(
    work_id: int, payload: ProgressIn,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> ProgressOut:
    if db.get(Work, work_id) is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)  # library isolation: members (or admin) only
    return save_progress_for(db, user.id, work_id, payload)


@router.get("/works/{work_id}/progress", response_model=ProgressOut)
def get_progress(
    work_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> ProgressOut:
    if db.get(Work, work_id) is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)  # library isolation: members (or admin) only
    return get_progress_for(db, user.id, work_id)


@router.delete("/works/{work_id}/progress")
def clear_progress(
    work_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    """Remove this work from the user's Continue Reading (clears their reading state). The work
    stays in the library; only the resume marker is dropped."""
    if db.get(Work, work_id) is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)  # library isolation: members (or admin) only
    state = get_state(db, user.id, work_id)
    if state is not None:
        db.delete(state)
        db.commit()
    return {"cleared": work_id}


@router.get("/continue-reading", response_model=list[ContinueItem])
def continue_reading(
    limit: int = 12, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[ContinueItem]:
    """The current user's recently-read works with a resume target, newest first."""
    states = db.scalars(
        select(ReadingState)
        .where(ReadingState.user_id == user.id, ReadingState.last_chapter_id.is_not(None))
        .order_by(ReadingState.updated_at.desc())
        .limit(limit)
    ).all()
    # Batch the lookups (was an N+1: a Work get, a Chapter get, and a COUNT per state).
    work_ids = [st.work_id for st in states]
    chap_ids = [st.last_chapter_id for st in states if st.last_chapter_id]
    works = {w.id: w for w in db.scalars(select(Work).where(Work.id.in_(work_ids))).all()} if work_ids else {}
    chapters = {c.id: c for c in db.scalars(select(Chapter).where(Chapter.id.in_(chap_ids))).all()} if chap_ids else {}
    totals = dict(
        db.execute(
            select(Chapter.work_id, func.count(Chapter.id))
            .where(Chapter.work_id.in_(work_ids)).group_by(Chapter.work_id)
        ).all()
    ) if work_ids else {}
    items: list[ContinueItem] = []
    for st in states:
        work = works.get(st.work_id)
        chapter = chapters.get(st.last_chapter_id) if st.last_chapter_id else None
        if work is None or chapter is None:
            continue
        total = totals.get(work.id, 0)
        through = (chapter.index - 1) + min(1.0, max(0.0, st.scroll_fraction))
        percent = round(100 * through / total, 1) if total else 0.0
        items.append(
            ContinueItem(
                work_id=work.id, title=work.title, author=work.author, cover_url=work.cover_url,
                chapter_id=chapter.id, chapter_index=chapter.index, chapter_title=chapter.title,
                paragraph_index=st.paragraph_index, scroll_fraction=st.scroll_fraction,
                chapters_read=st.chapters_read, total_chapters=total,
                percent=min(100.0, percent), updated_at=st.updated_at,
            )
        )
    return items
