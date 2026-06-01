from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Chapter, CrawlJob, ReadingState, Work
from ..schemas import WorkDetailOut, WorkOut

router = APIRouter()


def _fetched_count(db: Session, work_id: int) -> int:
    return db.scalar(
        select(func.count(Chapter.id)).where(
            Chapter.work_id == work_id, Chapter.fetch_status == "fetched"
        )
    ) or 0


def _total_count(db: Session, work_id: int) -> int:
    return db.scalar(select(func.count(Chapter.id)).where(Chapter.work_id == work_id)) or 0


@router.get("/works", response_model=list[WorkOut])
def list_works(
    q: str | None = Query(None, description="Filter by title / author / description"),
    db: Session = Depends(get_db),
) -> list[WorkOut]:
    stmt = select(Work).order_by(Work.created_at.desc())
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(Work.title.ilike(like), Work.author.ilike(like), Work.description.ilike(like))
        )
    works = db.scalars(stmt).all()
    out: list[WorkOut] = []
    for w in works:
        item = WorkOut.model_validate(w)
        item.chapters_fetched = _fetched_count(db, w.id)
        out.append(item)
    return out


@router.get("/works/{work_id}", response_model=WorkDetailOut)
def get_work(work_id: int, db: Session = Depends(get_db)) -> WorkDetailOut:
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    state = db.scalar(select(ReadingState).where(ReadingState.work_id == work_id))
    detail = WorkDetailOut.model_validate(work)
    detail.chapters_total = _total_count(db, work_id)
    detail.chapters_fetched = _fetched_count(db, work_id)
    if state:
        detail.chapters_read = state.chapters_read
        detail.last_chapter_id = state.last_chapter_id
        detail.scroll_fraction = state.scroll_fraction
    return detail


@router.delete("/works/{work_id}")
def delete_work(work_id: int, db: Session = Depends(get_db)) -> dict:
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    # Remove the work's crawl jobs too (no orphan "work missing" rows).
    for job in db.scalars(select(CrawlJob).where(CrawlJob.work_id == work_id)).all():
        db.delete(job)
    db.delete(work)
    db.commit()
    return {"deleted": work_id}
