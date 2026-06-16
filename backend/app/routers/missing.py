"""Missing-content ledger API — the per-title record of content requested but not found.

A regular user sees only the missing titles THEY requested (via the requester join); an admin sees
every row plus who wants each. The admin re-check endpoint force-runs the acquire pipeline for a
title immediately, bypassing the gate (the same thing the periodic ``missing_recheck_tick`` does on a
schedule). The model + lifecycle hooks live in :mod:`app.ingestion.ledger`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import current_user, require_admin
from ..db import get_db
from ..ingestion.ledger import REASONS
from ..models import (
    CatalogWork,
    ContentRequest,
    ContentRequestRequester,
    QueuedHook,
    User,
)
from ..schemas import MissingRequestOut, MissingStatsOut

router = APIRouter()

_LIST_CAP = 500
_STATUSES = ("open", "searching", "unavailable", "resolved")


def _row_out(row: ContentRequest, *, requested_at=None, requesters: list[str] | None = None,
             count: int | None = None) -> MissingRequestOut:
    return MissingRequestOut(
        id=row.id, title=row.title, author=row.author, status=row.status,
        failure_reason=row.failure_reason, last_provider=row.last_provider,
        attempts=row.attempts or 0, first_requested_at=row.first_requested_at,
        last_attempt_at=row.last_attempt_at, next_check_at=row.next_check_at,
        resolved_at=row.resolved_at, requested_at=requested_at,
        requester_count=count, requesters=requesters,
    )


@router.get("/missing", response_model=list[MissingRequestOut])
def list_missing(
    status: str | None = Query(None, description="Filter by status (open|searching|unavailable|resolved)"),
    reason: str | None = Query(None, description="Filter by failure_reason"),
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> list[MissingRequestOut]:
    """Missing titles. A regular user sees only the ones they requested; an admin sees ALL rows plus
    who wants each (requester count + usernames). Newest-first, capped at 500."""
    if status is not None and status not in _STATUSES:
        raise HTTPException(400, f"unknown status {status!r}")
    if reason is not None and reason not in REASONS:
        raise HTTPException(400, f"unknown reason {reason!r}")
    is_admin = user.role == "admin"

    sel = select(ContentRequest)
    if not is_admin:  # scope to rows this user is a requester of (via the join table)
        sel = sel.join(ContentRequestRequester,
                       ContentRequestRequester.request_id == ContentRequest.id) \
                 .where(ContentRequestRequester.user_id == user.id)
    if status is not None:
        sel = sel.where(ContentRequest.status == status)
    if reason is not None:
        sel = sel.where(ContentRequest.failure_reason == reason)
    rows = db.scalars(sel.order_by(ContentRequest.id.desc()).limit(_LIST_CAP)).all()

    out: list[MissingRequestOut] = []
    for row in rows:
        if is_admin:
            reqs = db.execute(
                select(ContentRequestRequester.user_id, User.username)
                .outerjoin(User, User.id == ContentRequestRequester.user_id)
                .where(ContentRequestRequester.request_id == row.id)
            ).all()
            names = [(uname or "system") if uid is not None else "system" for uid, uname in reqs]
            out.append(_row_out(row, requesters=names, count=len(reqs)))
        else:
            req = db.scalar(select(ContentRequestRequester.requested_at).where(
                ContentRequestRequester.request_id == row.id,
                ContentRequestRequester.user_id == user.id))
            out.append(_row_out(row, requested_at=req))

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
    a fresh re-check time. Resets the attempt counter so the manual retry reads as a fresh attempt."""
    from ..ingestion.acquire import acquire, user_priority

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
    await acquire(db, cw, user_id=None, priority=user_priority(db, None), force=True)
    db.refresh(row)
    reqs = db.execute(
        select(ContentRequestRequester.user_id, User.username)
        .outerjoin(User, User.id == ContentRequestRequester.user_id)
        .where(ContentRequestRequester.request_id == row.id)).all()
    names = [(uname or "system") if uid is not None else "system" for uid, uname in reqs]
    return _row_out(row, requesters=names, count=len(reqs))
