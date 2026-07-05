"""Wanted — the requests + tracking dashboard (replaces the deprecated Watchlist page).

A regular user sees ONLY the titles THEY requested (each with its live acquisition state) plus the
series/authors THEY track. An admin can additionally view the whole instance (``scope=global``) with a
per-user breakdown, or drill into one user (``scope=global&user_id=``). Built fresh on the existing
ledger tables — ``ContentRequest`` + ``ContentRequestRequester`` for requests, ``Subscription`` for
tracking, ``DownloadJob`` for live progress — reusing only data, none of the old watchlist code.
"""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, nulls_last, select
from sqlalchemy.orm import Session

from ..auth import current_user, require_admin
from ..db import get_db
from ..models import (
    CatalogWork,
    ContentRequest,
    ContentRequestRequester,
    DownloadJob,
    Subscription,
    User,
)
from ..schemas import (
    RescanIn,
    RescanStatusOut,
    TrackedOut,
    WantedDashboardOut,
    WantedOverviewOut,
    WantedRecentWork,
    WantedRequestOut,
    WantedRequestsPage,
    WantedStateCounts,
    WantedTrackingCounts,
    WantedUserBreakdown,
)

router = APIRouter()

_ACTIVE_DL = ("queued", "downloading", "completed")   # a DownloadJob still working toward import
_LIVE_DL = ("queued", "downloading")                  # actively moving (counts as state "downloading")
# ContentRequest.status → the user-facing state shown on the page.
_STATUS_STATE = {"open": "requested", "searching": "searching", "unavailable": "unavailable",
                 "resolved": "available", "planned": "upcoming"}
_STATE_STATUS = {"requested": "open", "searching": "searching", "unavailable": "unavailable",
                 "available": "resolved", "upcoming": "planned"}
_STATES = ("requested", "searching", "downloading", "available", "unavailable", "upcoming")
_SORTS = ("newest", "title", "author")


def _target_uid(user: User, scope: str, user_id: int | None) -> int | None:
    """The user_id to scope queries to (None = whole instance). A regular user is ALWAYS pinned to
    their own id; only an admin may go global (None) or drill into another user's id."""
    if user.role != "admin":
        return user.id
    if scope == "global":
        return user_id            # None = all users; an id = that user's slice
    return user.id                # scope=me


def _state_counts(db: Session, uid: int | None) -> WantedStateCounts:
    q = select(ContentRequest.status, func.count(func.distinct(ContentRequest.id)))
    if uid is not None:
        q = q.join(ContentRequestRequester, ContentRequestRequester.request_id == ContentRequest.id) \
             .where(ContentRequestRequester.user_id == uid)
    by = {s: int(c) for s, c in db.execute(q.group_by(ContentRequest.status)).all()}
    dl = select(func.count(DownloadJob.id)).where(DownloadJob.status.in_(_LIVE_DL))
    if uid is not None:
        dl = dl.where(DownloadJob.user_id == uid)
    return WantedStateCounts(
        requested=by.get("open", 0), searching=by.get("searching", 0),
        available=by.get("resolved", 0), unavailable=by.get("unavailable", 0),
        upcoming=by.get("planned", 0), downloading=int(db.scalar(dl) or 0),
        total=sum(by.values()))


def _tracking_counts(db: Session, uid: int | None) -> WantedTrackingCounts:
    q = select(Subscription.active, Subscription.auto_added)
    if uid is not None:
        q = q.where(Subscription.user_id == uid)
    rows = db.execute(q).all()
    active = sum(1 for a, _ in rows if a)
    return WantedTrackingCounts(total=len(rows), active=active, paused=len(rows) - active,
                                auto_added_total=sum((au or 0) for _, au in rows))


def _per_user(db: Session) -> list[WantedUserBreakdown]:
    """Per-user request + tracking counts for the admin global view — three grouped queries, not N+1."""
    users = {u.id: u.username for u in db.scalars(select(User)).all()}
    req: dict[int, dict[str, int]] = defaultdict(dict)
    for uid, st, c in db.execute(
        select(ContentRequestRequester.user_id, ContentRequest.status,
               func.count(func.distinct(ContentRequest.id)))
        .join(ContentRequest, ContentRequest.id == ContentRequestRequester.request_id)
        .where(ContentRequestRequester.user_id.is_not(None))
        .group_by(ContentRequestRequester.user_id, ContentRequest.status)).all():
        req[uid][st] = int(c)
    dl = {uid: int(c) for uid, c in db.execute(
        select(DownloadJob.user_id, func.count()).where(
            DownloadJob.status.in_(_LIVE_DL), DownloadJob.user_id.is_not(None))
        .group_by(DownloadJob.user_id)).all()}
    subs: dict[int, dict[str, int]] = defaultdict(lambda: {"total": 0, "active": 0, "auto": 0})
    for uid, active, auto in db.execute(
            select(Subscription.user_id, Subscription.active, Subscription.auto_added)).all():
        subs[uid]["total"] += 1
        subs[uid]["active"] += 1 if active else 0
        subs[uid]["auto"] += auto or 0
    out = []
    for uid in sorted(set(req) | set(subs) | set(dl), key=lambda i: (users.get(i) or "").lower()):
        by, s = req.get(uid, {}), subs.get(uid, {"total": 0, "active": 0, "auto": 0})
        out.append(WantedUserBreakdown(
            user_id=uid, username=users.get(uid) or "system",
            requests=WantedStateCounts(
                requested=by.get("open", 0), searching=by.get("searching", 0),
                available=by.get("resolved", 0), unavailable=by.get("unavailable", 0),
                upcoming=by.get("planned", 0), downloading=dl.get(uid, 0), total=sum(by.values())),
            tracking=WantedTrackingCounts(
                total=s["total"], active=s["active"], paused=s["total"] - s["active"],
                auto_added_total=s["auto"])))
    return out


@router.get("/wanted/overview", response_model=WantedOverviewOut)
def overview(scope: str = Query("me"), user: User = Depends(current_user),
             db: Session = Depends(get_db)) -> WantedOverviewOut:
    """Summary counts for the caller's view. Regular user → their own; admin can pass ``scope=global``
    for the whole instance plus a per-user breakdown."""
    is_admin = user.role == "admin"
    scope = scope if (is_admin and scope == "global") else "me"
    uid = _target_uid(user, scope, None)
    out = WantedOverviewOut(scope=scope, is_admin=is_admin,
                            requests=_state_counts(db, uid), tracking=_tracking_counts(db, uid))
    if is_admin and scope == "global":
        out.per_user = _per_user(db)
    return out


def _serialize_request_rows(db: Session, rows, *, is_admin: bool, uid: int | None) -> list[WantedRequestOut]:
    """Build WantedRequestOut for a set of (ContentRequest, CatalogWork) rows, batching the per-row
    overlays (latest active download, resolved Work id, requesters/own-requested-at, language) so a
    page is a handful of queries, not N+1. Shared by the requests list and the admin dashboard rails."""
    cwids = [r.catalog_work_id for r, _ in rows if r.catalog_work_id]
    rids = [r.id for r, _ in rows]
    dljobs: dict[int, DownloadJob] = {}
    if cwids:
        for j in db.scalars(select(DownloadJob).where(
                DownloadJob.catalog_work_id.in_(cwids), DownloadJob.status.in_(_ACTIVE_DL))
                .order_by(DownloadJob.id.desc())).all():
            dljobs.setdefault(j.catalog_work_id, j)   # latest active per catalog work
    hooked = dict(db.execute(select(CatalogWork.id, CatalogWork.hooked_work_id).where(
        CatalogWork.id.in_(cwids), CatalogWork.hooked_work_id.is_not(None))).all()) if cwids else {}
    reqmap: dict[int, list[str]] = defaultdict(list)
    reqat: dict[int, object] = {}
    if is_admin and rids:
        for rid, u_id, uname in db.execute(
                select(ContentRequestRequester.request_id, ContentRequestRequester.user_id, User.username)
                .outerjoin(User, User.id == ContentRequestRequester.user_id)
                .where(ContentRequestRequester.request_id.in_(rids))).all():
            reqmap[rid].append((uname or "system") if u_id is not None else "system")
    elif uid is not None and rids:
        reqat = dict(db.execute(select(ContentRequestRequester.request_id, ContentRequestRequester.requested_at)
                                .where(ContentRequestRequester.request_id.in_(rids),
                                       ContentRequestRequester.user_id == uid)).all())

    items = []
    for r, cw in rows:
        dl = dljobs.get(r.catalog_work_id)
        live = dl is not None and dl.status in _LIVE_DL
        extra = cw.extra if (cw and isinstance(cw.extra, dict)) else {}
        pos = extra.get("series_position")
        items.append(WantedRequestOut(
            id=r.id, title=r.title, author=r.author, variant=getattr(r, "variant", "ebook"),
            language=(cw.language if cw else None),
            state=("downloading" if live and r.status != "resolved" else _STATUS_STATE.get(r.status, "requested")),
            status=r.status, cover_url=(cw.cover_url if cw else None),
            catalog_work_id=(cw.id if cw else None), work_id=hooked.get(r.catalog_work_id),
            series=extra.get("series"), series_position=pos if isinstance(pos, int) else None,
            origin=r.origin or "request", origin_detail=r.origin_detail,
            failure_reason=r.failure_reason, first_requested_at=r.first_requested_at,
            requested_at=reqat.get(r.id), last_attempt_at=r.last_attempt_at,
            resolved_at=r.resolved_at, release_date=r.release_date,
            download_status=(dl.status if dl else None),
            download_mb_left=(dl.progress_mb_left if dl else None),
            requester_count=(len(reqmap[r.id]) if is_admin else None),
            requesters=(reqmap[r.id] if is_admin else None)))
    return items


@router.get("/wanted/requests", response_model=WantedRequestsPage)
def list_requests(
    state: str | None = Query(None), scope: str = Query("me"), user_id: int | None = Query(None),
    sort: str = Query("newest"), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> WantedRequestsPage:
    """The caller's requested titles (paginated), each with its live acquisition state. Admins may set
    ``scope=global`` (all users) or ``scope=global&user_id=`` (one user)."""
    if state is not None and state not in _STATES:
        raise HTTPException(400, f"unknown state {state!r}")
    if sort not in _SORTS:
        raise HTTPException(400, f"unknown sort {sort!r}")
    is_admin = user.role == "admin"
    scope = scope if (is_admin and scope == "global") else "me"
    uid = _target_uid(user, scope, user_id)

    sel = select(ContentRequest, CatalogWork).outerjoin(
        CatalogWork, CatalogWork.id == ContentRequest.catalog_work_id)
    if uid is not None:
        sel = sel.join(ContentRequestRequester, ContentRequestRequester.request_id == ContentRequest.id) \
                 .where(ContentRequestRequester.user_id == uid)
    if state == "downloading":
        sel = sel.where(ContentRequest.catalog_work_id.in_(
            select(DownloadJob.catalog_work_id).where(DownloadJob.status.in_(_LIVE_DL))))
    elif state:
        sel = sel.where(ContentRequest.status == _STATE_STATUS[state])

    total = int(db.scalar(select(func.count()).select_from(sel.subquery())) or 0)
    order = {"newest": (ContentRequest.id.desc(),),
             "title": (func.lower(ContentRequest.title),),
             "author": (nulls_last(func.lower(ContentRequest.author)), ContentRequest.title)}[sort]
    rows = db.execute(sel.order_by(*order).limit(limit).offset(offset)).all()
    items = _serialize_request_rows(db, rows, is_admin=is_admin, uid=uid)
    return WantedRequestsPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/wanted/dashboard", response_model=WantedDashboardOut)
def dashboard(scope: str = Query("me"), user: User = Depends(current_user),
              db: Session = Depends(get_db)) -> WantedDashboardOut:
    """Overseerr-style dashboard rails. ``scope=me`` (default, any user) scopes the requests, tracking
    and imported lists to the CALLER; an admin may pass ``scope=global`` for the whole instance plus
    the per-user breakdown. "Recently added" is the shared library, so it's the same either way.
    Requests carry their requester(s), status and language."""
    from ..models import ListSubscription, Work
    from .list_imports import _out as _list_out

    is_admin = user.role == "admin"
    scope = scope if (is_admin and scope == "global") else "me"
    uid = None if (is_admin and scope == "global") else user.id  # me → own; global(admin) → all

    def _requests(where=None, order=None, limit=24):
        sel = (select(ContentRequest, CatalogWork)
               .outerjoin(CatalogWork, CatalogWork.id == ContentRequest.catalog_work_id))
        if uid is not None:
            sel = sel.join(ContentRequestRequester,
                           ContentRequestRequester.request_id == ContentRequest.id) \
                     .where(ContentRequestRequester.user_id == uid)
        if where is not None:
            sel = sel.where(where)
        rows = db.execute(sel.order_by(*(order or (ContentRequest.id.desc(),))).limit(limit)).all()
        return _serialize_request_rows(db, rows, is_admin=is_admin, uid=uid)

    def _recent_works(kinds, audio: bool):
        # "Added" = a hooked ebook/comic, or a downloaded audiobook file — the SHARED library, so the
        # same rail for everyone regardless of scope. Newest first.
        cond = (Work.local_path.is_not(None)) if audio else (Work.hooked.is_(True))
        rows = db.scalars(
            select(Work).where(Work.media_kind.in_(kinds), cond)
            .order_by(Work.created_at.desc()).limit(12)).all()
        return [WantedRecentWork(work_id=w.id, title=w.title, author=w.author, cover_url=w.cover_url,
                                 language=w.language, media_kind=w.media_kind, added_at=w.created_at)
                for w in rows]

    tracked_q = select(ListSubscription).order_by(ListSubscription.created_at.desc())
    if uid is not None:
        tracked_q = tracked_q.where(ListSubscription.user_id == uid)
    tracked = db.scalars(tracked_q.limit(50)).all()

    # Followed series/authors (active first, newest first) — scoped to the caller unless admin-global.
    track_q = select(Subscription, User.username).outerjoin(User, User.id == Subscription.user_id)
    if uid is not None:
        track_q = track_q.where(Subscription.user_id == uid)
    track_rows = db.execute(
        track_q.order_by(Subscription.active.desc(), Subscription.created_at.desc()).limit(24)).all()
    tracking = [TrackedOut(
        id=s.id, kind=s.kind, display_name=s.display_name, active=s.active,
        auto_request=s.auto_request, auto_added=s.auto_added or 0,
        last_checked_at=s.last_checked_at, created_at=s.created_at,
        state=("paused" if not s.active else ("up_to_date" if s.last_checked_at else "gathering")),
        user_id=s.user_id, username=(uname if is_admin else None)) for s, uname in track_rows]

    return WantedDashboardOut(
        recent_requests=_requests(),
        recent_ebooks=_recent_works(("text", "comic"), audio=False),
        recent_audiobooks=_recent_works(("audio",), audio=True),
        tracked_lists=[_list_out(db, s) for s in tracked],
        tracking=tracking,
        user_requests=(_per_user(db) if (is_admin and scope == "global") else []),
        upcoming=_requests(
            where=(ContentRequest.status == "planned"),
            order=(nulls_last(ContentRequest.release_date.asc()), ContentRequest.id.desc())),
    )


@router.get("/wanted/tracking", response_model=list[TrackedOut])
def list_tracking(
    scope: str = Query("me"), user_id: int | None = Query(None),
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> list[TrackedOut]:
    """The series/authors the caller tracks (Subscriptions), each with a follow state. Admins may set
    ``scope=global`` for everyone, or drill into one user."""
    is_admin = user.role == "admin"
    scope = scope if (is_admin and scope == "global") else "me"
    uid = _target_uid(user, scope, user_id)
    q = select(Subscription, User.username).outerjoin(User, User.id == Subscription.user_id)
    if uid is not None:
        q = q.where(Subscription.user_id == uid)
    q = q.order_by(Subscription.active.desc(), func.lower(Subscription.display_name)).limit(2000)
    out = []
    for s, uname in db.execute(q).all():
        state = "paused" if not s.active else ("up_to_date" if s.last_checked_at else "gathering")
        out.append(TrackedOut(
            id=s.id, kind=s.kind, display_name=s.display_name, active=s.active,
            auto_request=s.auto_request, auto_added=s.auto_added or 0,
            last_checked_at=s.last_checked_at, created_at=s.created_at, state=state,
            user_id=s.user_id, username=(uname if is_admin else None)))
    return out


@router.post("/wanted/requests/{request_id}/recheck", response_model=WantedRequestOut,
             dependencies=[Depends(require_admin)])
async def recheck(request_id: int, db: Session = Depends(get_db)) -> WantedRequestOut:
    """Admin: force an immediate re-acquire of a requested title (bypasses the gate, resets per-source
    search state + attempt counter) — resolves it if now obtainable, else re-marks unavailable."""
    from ..ingestion.acquire import acquire, user_priority
    from ..ingestion import source_state
    r = db.get(ContentRequest, request_id)
    if r is None:
        raise HTTPException(404, "request not found")
    cw = db.get(CatalogWork, r.catalog_work_id) if r.catalog_work_id else None
    if cw is None and r.norm_key:
        cw = db.scalar(select(CatalogWork).where(CatalogWork.norm_key == r.norm_key)
                       .order_by(CatalogWork.popularity.desc()))
    if cw is None:
        raise HTTPException(409, "no catalog entry to re-acquire this title from")
    r.attempts = 0
    db.commit()
    source_state.reset_sources(db, r)
    await acquire(db, cw, user_id=None, priority=user_priority(db, None), force=True)
    db.refresh(r)
    reqs = db.execute(select(ContentRequestRequester.user_id, User.username)
                      .outerjoin(User, User.id == ContentRequestRequester.user_id)
                      .where(ContentRequestRequester.request_id == r.id)).all()
    extra = cw.extra if isinstance(cw.extra, dict) else {}
    pos = extra.get("series_position")
    return WantedRequestOut(
        id=r.id, title=r.title, author=r.author, variant=getattr(r, "variant", "ebook"),
        state=_STATUS_STATE.get(r.status, "requested"), status=r.status, cover_url=cw.cover_url,
        catalog_work_id=cw.id, work_id=cw.hooked_work_id, series=extra.get("series"),
        series_position=pos if isinstance(pos, int) else None, origin=r.origin or "request",
        origin_detail=r.origin_detail, failure_reason=r.failure_reason,
        first_requested_at=r.first_requested_at, last_attempt_at=r.last_attempt_at,
        resolved_at=r.resolved_at, release_date=r.release_date,
        requester_count=len(reqs),
        requesters=[(un or "system") if uid is not None else "system" for uid, un in reqs])


@router.post("/wanted/rescan", dependencies=[Depends(require_admin)])
def rescan(body: RescanIn, db: Session = Depends(get_db)) -> dict:
    """Admin: mass re-acquire every searchable (unavailable/open) request in scope — exactly one of
    ``{all} | {author} | {series} | {ids}``. The rescan drain tick processes the queue sequentially."""
    from ..ingestion.rescan import queue_rescan
    scopes = [k for k in ("all", "author", "series", "ids") if getattr(body, k) not in (None, False)]
    if len(scopes) != 1:
        raise HTTPException(400, "provide exactly one of: all | author | series | ids")
    return {"queued": queue_rescan(db, scope=scopes[0], author=body.author,
                                   series=body.series, ids=body.ids)}


@router.get("/wanted/rescan/status", response_model=RescanStatusOut,
            dependencies=[Depends(require_admin)])
def rescan_status_endpoint(db: Session = Depends(get_db)) -> RescanStatusOut:
    """Admin: mass-rescan progress for the frontend's progress strip."""
    from ..ingestion.rescan import rescan_status
    return RescanStatusOut(**rescan_status(db))
