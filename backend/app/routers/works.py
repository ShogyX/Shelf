from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from ..auth import current_user, require_admin
from ..db import get_db
from ..ingestion import diagnose, tracker
from ..library import assert_work_access, in_library, remove_from_library
from ..models import (
    Bookshelf,
    BookshelfItem,
    CatalogGroup,
    CatalogWork,
    Chapter,
    CrawlJob,
    IndexedPage,
    LibraryItem,
    MetadataLink,
    QueuedHook,
    ReadingState,
    User,
    Work,
)


def _target_user_id(user: User, user_id: int | None) -> int:
    """Whose library to act on: the caller's own, or — for admins controlling all libraries —
    an explicit ?user_id. A non-admin may only ever touch their own."""
    if user_id is not None and user_id != user.id:
        if user.role != "admin":
            raise HTTPException(403, "Admins only may view or manage another user's library")
        return user_id
    return user.id
from ..schemas import (
    CheckAllUpdatesOut,
    CrawlPolicyIn,
    SeriesOut,
    WorkDetailOut,
    WorkHealthOut,
    WorkOut,
    WorkUpdateOut,
)

router = APIRouter()

_POLICY_ATTRS = (
    "crawl_interval_s", "crawl_window_start", "crawl_window_end",
)


def apply_crawl_policy(work: Work, data) -> None:
    """Replace the work's per-title crawl policy from an object carrying the same attrs
    (None = use the source default). Callers send the full policy each time."""
    for attr in _POLICY_ATTRS:
        setattr(work, attr, getattr(data, attr, None))


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


def _pending_count(db: Session, work_id: int) -> int:
    return db.scalar(
        select(func.count(Chapter.id)).where(
            Chapter.work_id == work_id, Chapter.fetch_status.in_(["pending", "failed"])
        )
    ) or 0


def library_status(work: Work, fetched: int, pending: int) -> str:
    """One plain-English state for a library card (see schemas.WorkOut.library_status):

      * paused     — automatic updates/gathering are turned off for this title
      * gathering  — chapters are being downloaded right now
      * ongoing    — caught up; the series is still releasing (more chapters will come)
      * complete   — the series has finished and everything is gathered
      * incomplete — chapters exist that we don't have, and we're not currently fetching them

    The series' own release state is ``work.status`` ('ongoing' | 'complete'); whether we're caught
    up / actively fetching comes from the outstanding chapters + crawl state."""
    if not work.hooked:
        return "complete"  # imported/local content is static and fully present
    if work.crawl_paused:
        return "paused"  # operator stopped continuous updates — clearly flagged + resumable
    if pending > 0:
        return "gathering"
    if work.health in ("incomplete", "no_chapters", "unreachable"):
        return "incomplete"
    if work.status == "complete":
        return "complete"
    return "ongoing"


@router.get("/works", response_model=list[WorkOut])
def list_works(
    q: str | None = Query(None, description="Filter by title / author / description"),
    user_id: int | None = Query(None, description="Admin only: view another user's library"),
    shelf_id: int | None = Query(None, description="Filter to one of the user's bookshelves"),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[WorkOut]:
    # The library is per-user: only the works in THIS user's library (membership). Admins may
    # pass ?user_id to view/manage any user's library.
    target = _target_user_id(user, user_id)
    stmt = (
        select(Work)
        .join(LibraryItem, LibraryItem.work_id == Work.id)
        .where(LibraryItem.user_id == target)
        .order_by(Work.created_at.desc())
    )
    if shelf_id is not None:
        # Only the target user's own shelves; 404 if it isn't theirs.
        owner = db.scalar(select(Bookshelf.user_id).where(Bookshelf.id == shelf_id))
        if owner != target:
            raise HTTPException(404, "Bookshelf not found")
        stmt = stmt.join(BookshelfItem, BookshelfItem.work_id == Work.id).where(
            BookshelfItem.shelf_id == shelf_id
        )
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(Work.title.ilike(like), Work.author.ilike(like), Work.description.ilike(like))
        )
    works = db.scalars(stmt.limit(limit).offset(offset)).all()
    ids = [w.id for w in works]
    # One grouped query for the fetched-chapter counts instead of a COUNT per work (N+1).
    fetched_by_work: dict[int, int] = {}
    pending_by_work: dict[int, int] = {}
    shelves_by_work: dict[int, list[int]] = {}
    if ids:
        fetched_by_work = dict(
            db.execute(
                select(Chapter.work_id, func.count(Chapter.id))
                .where(Chapter.work_id.in_(ids), Chapter.fetch_status == "fetched")
                .group_by(Chapter.work_id)
            ).all()
        )
        pending_by_work = dict(
            db.execute(
                select(Chapter.work_id, func.count(Chapter.id))
                .where(Chapter.work_id.in_(ids), Chapter.fetch_status.in_(["pending", "failed"]))
                .group_by(Chapter.work_id)
            ).all()
        )
        # Which of the target user's shelves each work is on (one query, not N+1).
        for w_id, s_id in db.execute(
            select(BookshelfItem.work_id, BookshelfItem.shelf_id)
            .join(Bookshelf, Bookshelf.id == BookshelfItem.shelf_id)
            .where(BookshelfItem.work_id.in_(ids), Bookshelf.user_id == target)
        ).all():
            shelves_by_work.setdefault(w_id, []).append(s_id)
    out: list[WorkOut] = []
    for w in works:
        item = WorkOut.model_validate(w)
        item.chapters_fetched = fetched_by_work.get(w.id, 0)
        item.library_status = library_status(w, item.chapters_fetched, pending_by_work.get(w.id, 0))
        item.shelf_ids = shelves_by_work.get(w.id, [])
        out.append(item)
    return out


@router.get("/works/{work_id}", response_model=WorkDetailOut)
def get_work(
    work_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> WorkDetailOut:
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    # Library isolation: a work is only readable if it's in the caller's library (admins may read
    # any). Surfaces as 404 so a non-member can't probe which works exist.
    if user.role != "admin" and not in_library(db, user.id, work_id):
        raise HTTPException(404, "Work not found")
    state = db.scalar(
        select(ReadingState).where(
            ReadingState.work_id == work_id, ReadingState.user_id == user.id
        )
    )
    detail = WorkDetailOut.model_validate(work)
    detail.chapters_total = _total_count(db, work_id)
    detail.chapters_fetched = _fetched_count(db, work_id)
    detail.library_status = library_status(work, detail.chapters_fetched, _pending_count(db, work_id))
    if state:
        detail.chapters_read = state.chapters_read
        detail.last_chapter_id = state.last_chapter_id
        detail.scroll_fraction = state.scroll_fraction
    return detail


@router.get("/works/{work_id}/series", response_model=SeriesOut)
async def work_series(
    work_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> SeriesOut:
    """The full series this library work belongs to — every volume, ordered, each flagged as in
    the user's library or MISSING (so the user sees what's still needed)."""
    from sqlalchemy import func

    from ..ingestion import series as series_mod
    from ..ingestion.extract import norm_title
    from ..library import library_work_ids
    work = db.get(Work, work_id)
    if work is None or (user.role != "admin" and not in_library(db, user.id, work_id)):
        raise HTTPException(404, "Work not found")
    # Seed series enumeration from a catalog row: prefer one tagged with this work's series, else
    # match by normalized title.
    cw = None
    if work.series:
        cw = db.scalar(select(CatalogWork).where(
            func.json_extract(CatalogWork.extra, "$.series") == work.series).limit(1))
    if cw is None:
        cw = db.scalar(select(CatalogWork).where(
            CatalogWork.norm_key == norm_title(work.title)).limit(1))
    if cw is None:
        return SeriesOut(series=work.series, books=[])
    detected = await series_mod.detect_series(db, cw)
    mine = library_work_ids(db, user.id)
    for b in detected["books"]:
        hw = b.get("hooked_work_id")
        b["in_library"] = bool(hw and hw in mine)
    return SeriesOut(series=detected["series"], books=detected["books"])


@router.patch("/works/{work_id}/crawl-policy", response_model=WorkOut,
              dependencies=[Depends(require_admin)])
def set_crawl_policy(
    work_id: int, payload: CrawlPolicyIn, db: Session = Depends(get_db)
) -> WorkOut:
    """Edit how fast / how much / when this title's background crawl may run. The crawl is SHARED
    across all users, so editing its rate/window is an operator (admin) action."""
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    apply_crawl_policy(work, payload)
    db.commit()
    db.refresh(work)
    item = WorkOut.model_validate(work)
    item.chapters_fetched = _fetched_count(db, work_id)
    item.library_status = library_status(work, item.chapters_fetched, _pending_count(db, work_id))
    return item


@router.post("/works/check-updates", response_model=CheckAllUpdatesOut,
             dependencies=[Depends(require_admin)])
async def check_all_updates(db: Session = Depends(get_db)) -> CheckAllUpdatesOut:
    """Re-check EVERY trackable hooked title (app-wide crawl work) — admin only."""
    summary = await tracker.check_all(db)
    return CheckAllUpdatesOut(**summary)


@router.post("/works/{work_id}/check-updates", response_model=WorkUpdateOut)
async def check_updates(
    work_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> WorkUpdateOut:
    """Re-check one hooked title now: refresh metadata and enqueue any new chapters."""
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)  # members of this work (or admin) may refresh it
    # An explicit manual check resumes a crawl the operator had paused (deleted/paused its job).
    if work.crawl_paused:
        work.crawl_paused = False
        db.commit()
    result = await tracker.check_work(db, work)
    return WorkUpdateOut(**result)


@router.post("/works/{work_id}/resume", response_model=WorkOut)
def resume_work(
    work_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> WorkOut:
    """Resume automatic updates/gathering for a paused title. Clearing the pause lets the reaper
    re-queue any outstanding chapters and the periodic refresh check it for new releases again."""
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)
    work.crawl_paused = False
    db.commit()
    db.refresh(work)
    item = WorkOut.model_validate(work)
    item.chapters_fetched = _fetched_count(db, work_id)
    item.library_status = library_status(work, item.chapters_fetched, _pending_count(db, work_id))
    return item


@router.post("/works/{work_id}/pause", response_model=WorkOut)
def pause_work(
    work_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> WorkOut:
    """Pause automatic updates/gathering for a title — it stays in the library but stops crawling
    (the shared crawl is paused for everyone who has it)."""
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)
    work.crawl_paused = True
    db.commit()
    db.refresh(work)
    item = WorkOut.model_validate(work)
    item.chapters_fetched = _fetched_count(db, work_id)
    item.library_status = library_status(work, item.chapters_fetched, _pending_count(db, work_id))
    return item


@router.get("/works/{work_id}/diagnose", response_model=WorkHealthOut)
def diagnose_work(
    work_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> WorkHealthOut:
    """Check how complete a work is (missing/failed chapters vs. advertised) and record
    the verdict on the work."""
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)
    report = diagnose.completeness(db, work)
    diagnose.apply_health(db, work, report)
    return _health_out(work_id, report)


@router.post("/works/{work_id}/repair", response_model=WorkHealthOut)
def repair_work(
    work_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> WorkHealthOut:
    """Attempt to fix an incomplete work: retry failed chapters, fill missing-chapter
    gaps, re-seed a stalled crawl, and reopen a backfill job."""
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)
    report = diagnose.repair(db, work)
    return _health_out(work_id, report)


def purge_work(db: Session, work: Work) -> None:
    """Permanently destroy a shared Work and every trace of it: memberships, shelf placements,
    crawl jobs, metadata links, and the catalog/index/queue "hooked" pointers that reference it
    (SQLite FK enforcement is off, so nothing nulls/cascades these automatically). The Work's own
    chapters, content and reading-states cascade via the ORM relationships on ``db.delete(work)``."""
    work_id = work.id
    db.execute(delete(LibraryItem).where(LibraryItem.work_id == work_id))
    db.execute(delete(BookshelfItem).where(BookshelfItem.work_id == work_id))
    db.execute(delete(MetadataLink).where(MetadataLink.work_id == work_id))
    for job in db.scalars(select(CrawlJob).where(CrawlJob.work_id == work_id)).all():
        db.delete(job)
    # Clear every "hooked at this work" back-pointer so none dangle at a deleted work.
    db.execute(
        update(CatalogWork).where(CatalogWork.hooked_work_id == work_id)
        .values(hooked_work_id=None, health="unknown", health_detail=None)
    )
    db.execute(
        update(CatalogGroup).where(CatalogGroup.hooked_work_id == work_id)
        .values(hooked_work_id=None)
    )
    db.execute(
        update(IndexedPage).where(IndexedPage.hooked_work_id == work_id)
        .values(hooked_work_id=None)
    )
    db.execute(
        update(QueuedHook).where(QueuedHook.related_work_id == work_id)
        .values(related_work_id=None)
    )
    db.execute(
        update(QueuedHook).where(QueuedHook.hooked_work_id == work_id)
        .values(hooked_work_id=None)
    )
    db.delete(work)
    db.commit()


def _prune_if_orphaned(db: Session, work_id: int) -> bool:
    """Purge a shared Work once its LAST library member leaves. The shared-Work-survives-removal
    rule exists so OTHER users keep their copy + crawl — with zero members that rationale is void,
    and an orphaned hooked work would otherwise keep getting crawled/gap-scanned forever. Returns
    True if the work was purged."""
    members = db.scalar(
        select(func.count(LibraryItem.id)).where(LibraryItem.work_id == work_id)
    ) or 0
    if members > 0:
        return False
    work = db.get(Work, work_id)
    if work is None:
        return False
    purge_work(db, work)
    return True


@router.delete("/works/{work_id}")
def delete_work(
    work_id: int,
    purge: bool = Query(False, description="Admin only: globally delete the shared work + crawl"),
    user_id: int | None = Query(None, description="Admin only: act on another user's library"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    # Default: drop the caller's library membership. The shared Work + its chapters/crawl stay for
    # OTHER users — but if that was the last member, the orphaned work is purged (no one is left to
    # serve, and a member-less hooked work would keep crawling forever). A global purge of a work
    # other users still hold is a separate admin-only action.
    if not purge:
        target = _target_user_id(user, user_id)
        remove_from_library(db, target, work_id)
        if _prune_if_orphaned(db, work_id):
            return {"removed_from_library": work_id, "user_id": target, "purged_orphan": True}
        return {"removed_from_library": work_id, "user_id": target}
    if user.role != "admin":
        raise HTTPException(403, "Admins only may permanently delete a shared work")
    purge_work(db, work)
    return {"deleted": work_id}
