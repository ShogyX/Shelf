from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..ingestion import diagnose, tracker
from ..models import CatalogWork, Chapter, CrawlJob, IndexedPage, ReadingState, User, Work
from ..schemas import (
    CheckAllUpdatesOut,
    WorkDetailOut,
    WorkHealthOut,
    WorkOut,
    WorkUpdateOut,
)

router = APIRouter()


def _health_out(work_id: int, report: dict) -> WorkHealthOut:
    return WorkHealthOut(
        work_id=work_id,
        health=report["health"],
        detail=report.get("detail"),
        fetched=report.get("fetched", 0),
        failed=report.get("failed", 0),
        pending=report.get("pending", 0),
        listed=report.get("listed", 0),
        advertised=report.get("advertised"),
        gaps=report.get("gaps", []),
        actions=report.get("actions", []),
    )


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
def get_work(
    work_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> WorkDetailOut:
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    state = db.scalar(
        select(ReadingState).where(
            ReadingState.work_id == work_id, ReadingState.user_id == user.id
        )
    )
    detail = WorkDetailOut.model_validate(work)
    detail.chapters_total = _total_count(db, work_id)
    detail.chapters_fetched = _fetched_count(db, work_id)
    if state:
        detail.chapters_read = state.chapters_read
        detail.last_chapter_id = state.last_chapter_id
        detail.scroll_fraction = state.scroll_fraction
    return detail


@router.post("/works/check-updates", response_model=CheckAllUpdatesOut)
async def check_all_updates(db: Session = Depends(get_db)) -> CheckAllUpdatesOut:
    """Re-check every trackable hooked title for new chapters / refreshed metadata."""
    summary = await tracker.check_all(db)
    return CheckAllUpdatesOut(**summary)


@router.post("/works/{work_id}/check-updates", response_model=WorkUpdateOut)
async def check_updates(work_id: int, db: Session = Depends(get_db)) -> WorkUpdateOut:
    """Re-check one hooked title now: refresh metadata and enqueue any new chapters."""
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    result = await tracker.check_work(db, work)
    return WorkUpdateOut(**result)


@router.get("/works/{work_id}/diagnose", response_model=WorkHealthOut)
def diagnose_work(work_id: int, db: Session = Depends(get_db)) -> WorkHealthOut:
    """Check how complete a work is (missing/failed chapters vs. advertised) and record
    the verdict on the work."""
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    report = diagnose.completeness(db, work)
    diagnose.apply_health(db, work, report)
    return _health_out(work_id, report)


@router.post("/works/{work_id}/repair", response_model=WorkHealthOut)
def repair_work(work_id: int, db: Session = Depends(get_db)) -> WorkHealthOut:
    """Attempt to fix an incomplete work: retry failed chapters, fill missing-chapter
    gaps, re-seed a stalled crawl, and reopen a backfill job."""
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    report = diagnose.repair(db, work)
    return _health_out(work_id, report)


@router.delete("/works/{work_id}")
def delete_work(work_id: int, db: Session = Depends(get_db)) -> dict:
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    # Remove the work's crawl jobs too (no orphan "work missing" rows).
    for job in db.scalars(select(CrawlJob).where(CrawlJob.work_id == work_id)).all():
        db.delete(job)
    # Clear catalog/index "hooked" pointers so they don't dangle at a deleted work
    # (SQLite FK enforcement is off, so nothing nulls these automatically).
    db.execute(
        update(CatalogWork).where(CatalogWork.hooked_work_id == work_id)
        .values(hooked_work_id=None, health="unknown", health_detail=None)
    )
    db.execute(
        update(IndexedPage).where(IndexedPage.hooked_work_id == work_id)
        .values(hooked_work_id=None)
    )
    db.delete(work)
    db.commit()
    return {"deleted": work_id}
