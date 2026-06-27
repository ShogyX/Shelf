"""Missing-content ledger API — the per-title record of content requested but not found.

A regular user sees only the missing titles THEY requested (via the requester join); an admin sees
every row plus who wants each. The admin re-check endpoint force-runs the acquire pipeline for a
title immediately, bypassing the gate (the same thing the periodic ``missing_recheck_tick`` does on a
schedule). The model + lifecycle hooks live in :mod:`app.ingestion.ledger`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, nulls_last, select
from sqlalchemy.orm import Session

from ..auth import current_user, require_admin
from ..db import get_db
from ..ingestion.ledger import REASONS
from ..ingestion.rescan import RESCAN_RUN_KEY, queue_rescan, rescan_status
from ..models import (
    CatalogWork,
    ContentRequest,
    ContentRequestRequester,
    QueuedHook,
    User,
    WorkSourceSearch,
)
from ..schemas import (
    MissingRequestOut,
    MissingStatsOut,
    RescanIn,
    RescanStatusOut,
    SourceSearchOut,
)

router = APIRouter()

_LIST_CAP = 500
_STATUSES = ("open", "searching", "unavailable", "resolved", "planned")
_SORTS = ("newest", "author", "series", "title")


def _source_states(db: Session, request_id: int) -> list[SourceSearchOut]:
    """The per-durable-source search rows for a missing title (Wave B info-icon popover)."""
    rows = db.scalars(select(WorkSourceSearch).where(
        WorkSourceSearch.content_request_id == request_id).order_by(WorkSourceSearch.source)).all()
    return [SourceSearchOut(
        source=s.source, status=s.status, reason=s.reason,
        last_attempt_at=s.last_attempt_at, next_retry_at=s.next_retry_at,
        attempts=s.attempts or 0,
    ) for s in rows]


def _series_fields(cw: CatalogWork | None) -> tuple[int | None, str | None, int | None]:
    """(catalog_work_id, series, series_position) from the joined catalog row's ``extra`` JSON — the
    chip's data, surfaced WITHOUT running detect_series (which is reserved for the lazy modal/auto-hook)."""
    if cw is None:
        return (None, None, None)
    extra = cw.extra if isinstance(cw.extra, dict) else {}
    pos = extra.get("series_position")
    return (cw.id, extra.get("series"), pos if isinstance(pos, int) else None)


def _row_out(row: ContentRequest, *, requested_at=None, requesters: list[str] | None = None,
             count: int | None = None, sources: list[SourceSearchOut] | None = None,
             cw: CatalogWork | None = None) -> MissingRequestOut:
    cwid, series, series_pos = _series_fields(cw)
    return MissingRequestOut(
        id=row.id, title=row.title, author=row.author, variant=getattr(row, "variant", "ebook"),
        status=row.status,
        failure_reason=row.failure_reason, last_provider=row.last_provider,
        attempts=row.attempts or 0, first_requested_at=row.first_requested_at,
        last_attempt_at=row.last_attempt_at, next_check_at=row.next_check_at,
        release_date=row.release_date,
        resolved_at=row.resolved_at, requested_at=requested_at,
        requester_count=count, requesters=requesters, sources=sources,
        origin=row.origin or "request", origin_detail=row.origin_detail,
        catalog_work_id=cwid, series=series, series_position=series_pos,
        cover_url=(cw.cover_url if cw else None),  # gallery thumbnail (Watchlist)
    )


@router.get("/missing", response_model=list[MissingRequestOut])
def list_missing(
    status: str | None = Query(None, description="Filter by status (open|searching|unavailable|resolved)"),
    reason: str | None = Query(None, description="Filter by failure_reason"),
    sort: str = Query("newest", description="Order: newest|author|series|title"),
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> list[MissingRequestOut]:
    """Missing titles. A regular user sees only the ones they requested; an admin sees ALL rows plus
    who wants each (requester count + usernames). Capped at 500. ``sort`` defaults to newest-first."""
    if status is not None and status not in _STATUSES:
        raise HTTPException(400, f"unknown status {status!r}")
    if reason is not None and reason not in REASONS:
        raise HTTPException(400, f"unknown reason {reason!r}")
    if sort not in _SORTS:
        raise HTTPException(400, f"unknown sort {sort!r}")
    is_admin = user.role == "admin"

    # Outerjoin the representative catalog row so the same query both surfaces the chip's series
    # fields (from CatalogWork.extra — NO detect_series) and powers the series sort.
    sel = select(ContentRequest, CatalogWork).outerjoin(
        CatalogWork, CatalogWork.id == ContentRequest.catalog_work_id)
    if not is_admin:  # scope to rows this user is a requester of (via the join table)
        sel = sel.join(ContentRequestRequester,
                       ContentRequestRequester.request_id == ContentRequest.id) \
                 .where(ContentRequestRequester.user_id == user.id)
    if status is not None:
        sel = sel.where(ContentRequest.status == status)
    if reason is not None:
        sel = sel.where(ContentRequest.failure_reason == reason)

    # Series sort buckets ungrouped titles ("no series") LAST, then orders by name → position → title.
    series_name = func.json_extract(CatalogWork.extra, "$.series")
    series_pos = func.json_extract(CatalogWork.extra, "$.series_position")
    order = {
        "newest": (ContentRequest.id.desc(),),
        "author": (nulls_last(func.lower(ContentRequest.author)), ContentRequest.title),
        "title": (func.lower(ContentRequest.title),),
        "series": (nulls_last(series_name), series_pos, ContentRequest.title),
    }[sort]
    rows = db.execute(sel.order_by(*order).limit(_LIST_CAP)).all()

    out: list[MissingRequestOut] = []
    for row, cw in rows:
        srcs = _source_states(db, row.id)   # per-source search state (info-icon popover)
        if is_admin:
            reqs = db.execute(
                select(ContentRequestRequester.user_id, User.username)
                .outerjoin(User, User.id == ContentRequestRequester.user_id)
                .where(ContentRequestRequester.request_id == row.id)
            ).all()
            names = [(uname or "system") if uid is not None else "system" for uid, uname in reqs]
            out.append(_row_out(row, requesters=names, count=len(reqs), sources=srcs, cw=cw))
        else:
            req = db.scalar(select(ContentRequestRequester.requested_at).where(
                ContentRequestRequester.request_id == row.id,
                ContentRequestRequester.user_id == user.id))
            out.append(_row_out(row, requested_at=req, sources=srcs, cw=cw))

    # Goodreads "waiting on hook" titles surfaced as virtual Missing rows (read-time union, no schema
    # change): QueuedHook(reason=goodreads, status=pending) queued from a user's shelf, auto-hooked
    # once they appear in the index. Tagged origin="goodreads"; they carry no failure_reason, so they
    # only join when no reason filter is set and the status filter is unset or "open".
    if reason is None and status in (None, "open"):
        qsel = select(QueuedHook).where(
            QueuedHook.reason == "goodreads", QueuedHook.status == "pending")
        if not is_admin:  # a regular user only sees hooks queued into their own library
            qsel = qsel.where(QueuedHook.user_id == user.id)
        for qh in db.scalars(qsel.order_by(QueuedHook.id.desc()).limit(_LIST_CAP)).all():
            out.append(MissingRequestOut(
                id=qh.id, title=qh.title, author=qh.author, status="open",
                attempts=qh.attempts or 0, first_requested_at=qh.created_at,
                last_provider=qh.source, origin="goodreads",
            ))
    return out


@router.get("/missing/stats", response_model=MissingStatsOut,
            dependencies=[Depends(require_admin)])
def missing_stats(db: Session = Depends(get_db)) -> MissingStatsOut:
    """Admin dashboard counts: rows by status + by failure_reason, the total unavailable, and the
    soonest pending re-check time."""
    by_status = {
        s: int(c) for s, c in db.execute(
            select(ContentRequest.status, func.count(ContentRequest.id))
            .group_by(ContentRequest.status)).all()
    }
    by_reason = {
        r: int(c) for r, c in db.execute(
            select(ContentRequest.failure_reason, func.count(ContentRequest.id))
            .where(ContentRequest.failure_reason.is_not(None))
            .group_by(ContentRequest.failure_reason)).all()
    }
    next_due = db.scalar(
        select(func.min(ContentRequest.next_check_at)).where(
            ContentRequest.status == "unavailable",
            ContentRequest.next_check_at.is_not(None)))
    return MissingStatsOut(
        total=int(sum(by_status.values())),
        total_unavailable=int(by_status.get("unavailable", 0)),
        by_status=by_status, by_reason=by_reason, next_due_at=next_due,
    )


@router.post("/missing/{request_id}/recheck", response_model=MissingRequestOut,
             dependencies=[Depends(require_admin)])
async def recheck_missing(request_id: int, db: Session = Depends(get_db)) -> MissingRequestOut:
    """Force an immediate re-acquire of a missing title, bypassing the gate (``force=True``). On a
    title that's now obtainable this resolves the row; otherwise acquire re-marks it unavailable with
    a fresh re-check time. Resets the attempt counter so the manual retry reads as a fresh attempt.

    Decision #4a: the admin "try everything fresh" override also RESETS the per-source search rows
    (no_match/exhausted/unavailable/skipped → pending, leases cleared), so the forced re-acquire
    re-searches every durable source — not just the non-terminal ones."""
    from ..ingestion.acquire import acquire, user_priority
    from ..ingestion import source_state

    row = db.get(ContentRequest, request_id)
    if row is None:
        raise HTTPException(404, "missing-content row not found")
    cw = db.get(CatalogWork, row.catalog_work_id) if row.catalog_work_id else None
    if cw is None and row.norm_key:
        cw = db.scalar(select(CatalogWork).where(CatalogWork.norm_key == row.norm_key)
                       .order_by(CatalogWork.popularity.desc()))
    if cw is None:
        raise HTTPException(409, "no catalog entry to re-acquire this title from")

    row.attempts = 0
    db.commit()
    source_state.reset_sources(db, row)
    await acquire(db, cw, user_id=None, priority=user_priority(db, None), force=True)
    db.refresh(row)
    reqs = db.execute(
        select(ContentRequestRequester.user_id, User.username)
        .outerjoin(User, User.id == ContentRequestRequester.user_id)
        .where(ContentRequestRequester.request_id == row.id)).all()
    names = [(uname or "system") if uid is not None else "system" for uid, uname in reqs]
    return _row_out(row, requesters=names, count=len(reqs), sources=_source_states(db, row.id), cw=cw)


@router.post("/missing/rescan", dependencies=[Depends(require_admin)])
def rescan(body: RescanIn, db: Session = Depends(get_db)) -> dict:
    """Mass-rescan: queue every SEARCHABLE missing title in ``scope`` for a sequential re-acquire.

    Scope is exactly one of ``{"all": true}`` | ``{"author": "..."}`` | ``{"series": "..."}`` |
    ``{"ids": [...]}``. Only rows in (unavailable, open) are queued — planned + resolved are excluded
    (a planned title isn't searchable yet; a resolved one is already in the library). Each matched row
    gets ``rescan_queued_at=now``, its per-source rows reset, and ``attempts=0``; the rescan_drain_tick
    then processes the queue one-by-one (never in parallel), honoring per-source rate limits. The
    progress total is tracked in the ``rescan_run`` AppSetting. Returns ``{"queued": N}``."""
    scopes = [k for k in ("all", "author", "series", "ids") if getattr(body, k) not in (None, False)]
    if len(scopes) != 1:
        raise HTTPException(400, "provide exactly one of: all | author | series | ids")
    queued = queue_rescan(db, scope=scopes[0],
                          author=body.author, series=body.series, ids=body.ids)
    return {"queued": queued}


@router.get("/missing/rescan/status", response_model=RescanStatusOut,
            dependencies=[Depends(require_admin)])
def rescan_status_endpoint(db: Session = Depends(get_db)) -> RescanStatusOut:
    """Mass-rescan progress for the frontend's progress strip: ``{total, done, queued, active}`` where
    ``queued`` counts rows still holding rescan_queued_at, ``total`` is this run's size (0 when idle),
    ``done = max(0, total - queued)``, and ``active = queued > 0``."""
    return RescanStatusOut(**rescan_status(db))
