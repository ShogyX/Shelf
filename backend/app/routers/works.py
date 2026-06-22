from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import current_user, require_admin
from ..db import get_db
from ..ingestion import diagnose, tracker
from ..ingestion.extract import norm_title
from ..library import assert_work_access, in_library, remove_from_library
from ..models import (
    Bookshelf,
    BookshelfItem,
    CatalogGroup,
    CatalogWork,
    Chapter,
    ContentRequest,
    CrawlJob,
    IndexedPage,
    LibraryItem,
    MetadataLink,
    QueuedHook,
    ReadingState,
    Source,
    User,
    UserSettings,
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
    DefaultShelfIn,
    MetaCandidateOut,
    SeriesOut,
    WorkDetailOut,
    WorkHealthOut,
    WorkMetaUpdate,
    WorkOut,
    WorkProvenanceOut,
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


def library_status(work: Work, pending: int) -> str:
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


def _match_audiobook(work: Work, audio_by_norm: dict[str, list[tuple[int, str | None]]]) -> int | None:
    """Pick the shared audiobook Work that pairs with this ebook ``work`` (same normalized title).
    Prefer an author-compatible match; fall back to title-only when there's a single candidate."""
    cands = audio_by_norm.get(norm_title(work.title or ""), [])
    if not cands:
        return None
    wa = (work.author or "").strip().lower()
    if wa:
        for aid, aauthor in cands:
            if (aauthor or "").strip().lower() == wa:
                return aid
    return cands[0][0] if len(cands) == 1 else None


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
        # NULL media_kind counts as text (matches catalog_groups), so don't let it drop a work.
        .where(LibraryItem.user_id == target,
               or_(Work.media_kind != "audio", Work.media_kind.is_(None)))
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
    # Audiobooks are shared stock (never library items): surface the one matching each title as its
    # "listen" format so the user sees ONE title and picks ebook-or-audiobook.
    audio_by_norm: dict[str, list[tuple[int, str | None]]] = {}
    for aid, atitle, aauthor in db.execute(
        select(Work.id, Work.title, Work.author)
        .where(Work.media_kind == "audio", Work.local_path.is_not(None))
    ).all():
        audio_by_norm.setdefault(norm_title(atitle or ""), []).append((aid, aauthor))
    out: list[WorkOut] = []
    for w in works:
        item = WorkOut.model_validate(w)
        item.chapters_fetched = fetched_by_work.get(w.id, 0)
        item.library_status = library_status(w, pending_by_work.get(w.id, 0))
        item.shelf_ids = shelves_by_work.get(w.id, [])
        item.audiobook_work_id = _match_audiobook(w, audio_by_norm)
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
    detail.library_status = library_status(work, _pending_count(db, work_id))
    if state:
        detail.chapters_read = state.chapters_read
        detail.last_chapter_id = state.last_chapter_id
        detail.scroll_fraction = state.scroll_fraction
    s = db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if s and s.work_default_shelves:
        detail.default_shelf_id = s.work_default_shelves.get(str(work_id))
    return detail


def _work_detail(db: Session, user: User, work: Work) -> WorkDetailOut:
    """Build the WorkDetailOut for a work + this user (chapter counts, status, reading state,
    default shelf). Shared by the metadata PATCH response."""
    detail = WorkDetailOut.model_validate(work)
    detail.chapters_total = _total_count(db, work.id)
    detail.chapters_fetched = _fetched_count(db, work.id)
    detail.library_status = library_status(work, _pending_count(db, work.id))
    state = db.scalar(select(ReadingState).where(
        ReadingState.work_id == work.id, ReadingState.user_id == user.id))
    if state:
        detail.chapters_read = state.chapters_read
        detail.last_chapter_id = state.last_chapter_id
        detail.scroll_fraction = state.scroll_fraction
    s = db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if s and s.work_default_shelves:
        detail.default_shelf_id = s.work_default_shelves.get(str(work.id))
    return detail


@router.patch("/works/{work_id}", response_model=WorkDetailOut)
def update_work_metadata(
    work_id: int, payload: WorkMetaUpdate,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> WorkDetailOut:
    """Manually correct a library work's metadata (fix a wrong auto-match): title / author / cover /
    series. Only the fields PRESENT in the request change. NB: works are SHARED, so editing the title
    updates it for everyone who has the work — intended (a correction is a correction). Requires the
    work be in the caller's library."""
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)
    data = payload.model_dump(exclude_unset=True)
    if "title" in data:
        t = (data["title"] or "").strip()
        if not t:
            raise HTTPException(400, "Title can't be empty.")
        work.title = t[:255]
    if "author" in data:
        work.author = ((data["author"] or "").strip()[:255]) or None
    if "cover_url" in data:
        work.cover_url = (data["cover_url"] or "").strip() or None
    if "series" in data:
        work.series = ((data["series"] or "").strip()[:255]) or None
    if "series_position" in data:
        work.series_position = data["series_position"]
    if "source_work_ref" in data:
        # Re-point the fetching source's reference (fix a wrong match's source). NULL is fine.
        work.source_work_ref = ((data["source_work_ref"] or "").strip()[:512]) or None
    try:
        db.commit()
    except IntegrityError:
        # (source_id, source_work_ref) is unique — another work already uses that reference.
        db.rollback()
        raise HTTPException(409, "Another title is already fetched from that source reference.")
    db.refresh(work)
    return _work_detail(db, user, work)


@router.get("/works/{work_id}/provenance", response_model=WorkProvenanceOut)
def work_provenance(
    work_id: int, user: User = Depends(current_user), db: Session = Depends(get_db),
) -> WorkProvenanceOut:
    """Where a library work came from — to diagnose a wrong match: the fetching source + on-disk
    filename, the catalog metadata used for the fetch, and the originally-requested title/author
    (from an import list / watchlist). Requires the work be in the caller's library."""
    import os

    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)
    out = WorkProvenanceOut(source_ref=work.source_work_ref)
    if work.source_id:
        src = db.get(Source, work.source_id)
        if src:
            out.source_key = src.key
            out.source_name = src.display_name
            ref = work.source_work_ref or ""
            if ref.startswith("http"):
                out.source_url = ref
            elif src.base_url and ref:
                out.source_url = src.base_url.rstrip("/") + "/" + ref.lstrip("/")
    if work.local_path:
        out.filename = os.path.basename(work.local_path)
        out.file_size = work.local_size
    # The catalog entry that hooked into this work = the metadata used for the fetch.
    cw = db.scalar(select(CatalogWork).where(CatalogWork.hooked_work_id == work_id))
    if cw:
        out.catalog_title = cw.title
        out.catalog_author = cw.author
        out.catalog_domain = cw.domain
        out.catalog_url = cw.work_url
    # The originally-requested title/author (import list / watchlist): prefer the request tied to that
    # catalog entry; else fall back to one matching the work's current normalized title.
    cr = None
    if cw:
        cr = db.scalar(select(ContentRequest).where(ContentRequest.catalog_work_id == cw.id))
    if cr is None:
        cr = db.scalar(select(ContentRequest).where(ContentRequest.norm_key == norm_title(work.title or "")))
    if cr:
        out.request_title = cr.title
        out.request_author = cr.author
        out.request_origin = cr.origin
        out.request_detail = cr.origin_detail
    return out


@router.get("/works/{work_id}/metadata-search", response_model=list[MetaCandidateOut])
async def search_work_metadata(
    work_id: int,
    q: str = Query(..., min_length=1, description="Title (or title+author) to search providers for"),
    author: str | None = Query(None),
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> list[MetaCandidateOut]:
    """Search the enabled metadata providers for candidate matches to re-point a library work at
    (powers 'Fix metadata'). Aggregates per-provider hits; the client applies a chosen one by PATCHing
    the work. Each provider is best-effort + time-boxed so one slow/erroring source can't sink it."""
    import asyncio

    from ..integrations import metadata as meta_mod
    from ..models import Integration

    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    assert_work_access(db, user, work_id)
    integs = db.scalars(select(Integration).where(Integration.enabled.is_(True))).all()
    providers = []
    for integ in integs:
        if not meta_mod.is_metadata_kind(integ.kind) or integ.kind == "goodreads":
            continue
        provider = meta_mod.provider_for(integ)
        if getattr(provider, "renders", False):
            continue  # skip slow headless-render providers on the interactive path
        providers.append((integ.kind, provider))

    async def _one(kind: str, provider) -> tuple[str, list]:
        # Each provider is independently time-boxed + swallowed so one slow/erroring source can't
        # sink the search; gather() runs them concurrently so total latency stays ~8s, not 8s×N.
        try:
            return kind, (await asyncio.wait_for(provider.search(q.strip(), author, limit=6), timeout=8)) or []
        except Exception:  # noqa: BLE001
            return kind, []

    results = await asyncio.gather(*[_one(k, p) for k, p in providers])
    out: list[MetaCandidateOut] = []
    seen: set[tuple[str, str]] = set()
    for kind, matches in results:
        for m in matches:
            key = (kind, m.ref)
            if key in seen:
                continue
            seen.add(key)
            out.append(MetaCandidateOut(
                provider=kind, ref=m.ref, title=m.title, author=m.author,
                year=m.year, cover_url=m.cover_url,
                synopsis=((m.synopsis or "")[:400] or None), media_kind=m.media_kind,
            ))
    return out[:30]


@router.put("/works/{work_id}/default-shelf", response_model=WorkDetailOut)
def set_work_default_shelf(
    work_id: int, payload: DefaultShelfIn,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> WorkDetailOut:
    """Set (or clear with null) THIS user's default shelf for THIS work. The shelf must belong to
    the caller. Stored per-user (UserSettings.work_default_shelves), not on the shared Work."""
    work = db.get(Work, work_id)
    if work is None or (user.role != "admin" and not in_library(db, user.id, work_id)):
        raise HTTPException(404, "Work not found")
    if payload.shelf_id is not None:
        owner = db.scalar(select(Bookshelf.user_id).where(Bookshelf.id == payload.shelf_id))
        if owner is None or owner != user.id:
            raise HTTPException(404, "Bookshelf not found")
    s = db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if s is None:
        s = UserSettings(user_id=user.id)
        db.add(s)
    # Reassign (not mutate-in-place) so SQLAlchemy detects the JSON change.
    m = dict(s.work_default_shelves or {})
    if payload.shelf_id is None:
        m.pop(str(work_id), None)
    else:
        m[str(work_id)] = payload.shelf_id
    s.work_default_shelves = m
    db.commit()
    return get_work(work_id, user, db)


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
    item.library_status = library_status(work, _pending_count(db, work_id))
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
    item.library_status = library_status(work, _pending_count(db, work_id))
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
    item.library_status = library_status(work, _pending_count(db, work_id))
    return item


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
    from .. import cache
    if not purge:
        target = _target_user_id(user, user_id)
        remove_from_library(db, target, work_id)
        orphaned = _prune_if_orphaned(db, work_id)
        # Drop cached catalog slices: they carry per-user in_library/in_stock flags (and, if the
        # orphan was purged, a now-deleted work id) that would otherwise keep showing the removed
        # title as in-library for up to the cache TTL — mirror the hook path's invalidation.
        cache.clear_catalog()
        if orphaned:
            return {"removed_from_library": work_id, "user_id": target, "purged_orphan": True}
        return {"removed_from_library": work_id, "user_id": target}
    if user.role != "admin":
        raise HTTPException(403, "Admins only may permanently delete a shared work")
    purge_work(db, work)
    cache.clear_catalog()
    return {"deleted": work_id}
