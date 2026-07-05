"""Following API — a user's "follow" of an author or series (Wave E, R14-R16).

Each user sees + manages only their OWN subscriptions (per-user gated; PATCH/DELETE 403 on a
non-owner). Subscribing is idempotent on UNIQUE(user_id, kind, key): a repeat follow returns the
existing row. Subscribing SEEDS ``known_keys`` with the current roster (best-effort) so the day-1
backlog is NOT auto-requested — only titles that appear AFTER the follow fire in ``follow_tick``.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..ingestion.extract import _author_norm, norm_title
from ..models import CatalogWork, Subscription, User
from ..schemas import SubscriptionCreateIn, SubscriptionOut, SubscriptionPatchIn

log = logging.getLogger("shelf.subscriptions")

router = APIRouter()


def _out(s: Subscription) -> SubscriptionOut:
    return SubscriptionOut(
        id=s.id, kind=s.kind, key=s.key, display_name=s.display_name, active=s.active,
        auto_request=s.auto_request, auto_added=s.auto_added,
        last_checked_at=s.last_checked_at, created_at=s.created_at,
    )


async def _seed_keys(db: Session, kind: str, key: str, display_name: str,
                     cw: CatalogWork | None) -> list[str] | None:
    """Best-effort current roster as the diff baseline (so day-1 backlog isn't auto-fired). On any
    provider error → **None** (the "unseeded" sentinel): ``follow_tick`` then treats that sub's FIRST
    enumeration as baseline-only (acquire nothing, just record the roster) instead of seeing the whole
    backlog as new and firing it. A successful seed returns the sorted norm-keys (possibly empty)."""
    from ..ingestion import series
    try:
        if kind == "author":
            books = await series.enumerate_author(db, display_name)
        elif cw is not None:
            books = (await series.detect_series(db, cw)).get("books", [])
        else:
            books = []
    except Exception:  # noqa: BLE001 — seeding is best-effort; never block the follow
        log.exception("seeding known_keys for %s %r failed", kind, key)
        return None  # unseeded → first tick establishes the baseline without fetching the backlog
    return sorted({norm_title(b["title"]) for b in books if b.get("title")})


async def _grab_author_backlog_bg(user_id: int, author_name: str) -> None:
    """Follow-author ALSO grabs the author's existing back-catalog now (capped at SERIES_ACQUIRE_CAP,
    owned titles skipped), then ``follow_tick`` tracks FUTURE releases. Runs in its own DB session so
    it never blocks the follow response; best-effort (a grab failure must not undo the follow). The
    grabbed titles are tagged ``origin="following"`` so they surface as follow-driven in Wanted."""
    from ..db import SessionLocal
    from ..ingestion import series

    db = SessionLocal()
    try:
        await series.acquire_author(db, author_name, refs=None, want_all=True,
                                    user_id=user_id, origin="following", origin_detail=author_name)
    except Exception:  # noqa: BLE001 — background best-effort; the follow already succeeded
        log.exception("follow-author backlog grab failed for %r", author_name)
        db.rollback()
    finally:
        db.close()


# Hold a strong ref to in-flight background grabs: the event loop only weakly references tasks, so
# without this a task could be GC'd mid-flight. Discarded on completion.
_bg_tasks: set[asyncio.Task] = set()


def _schedule_author_backlog(user_id: int, author_name: str) -> None:
    """Fire the follow-author back-catalog grab as a background task. Extracted so tests can assert
    (and neutralize) the scheduling without running the async grab / hitting providers."""
    task = asyncio.create_task(_grab_author_backlog_bg(user_id, author_name))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@router.get("/subscriptions", response_model=list[SubscriptionOut])
def list_subscriptions(
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> list[SubscriptionOut]:
    """The caller's own follows (newest first)."""
    rows = db.scalars(
        select(Subscription).where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
    ).all()
    return [_out(s) for s in rows]


@router.post("/subscriptions", response_model=SubscriptionOut)
async def create_subscription(
    payload: SubscriptionCreateIn,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> SubscriptionOut:
    """Follow an author or series. Resolves (key, display_name) from the catalog row (or a series name),
    upserts idempotently on UNIQUE(user_id, kind, key), and seeds the diff baseline so existing titles
    aren't auto-requested. ``auto_request`` defaults True (R15)."""
    kind = payload.kind
    if kind not in ("author", "series"):
        raise HTTPException(400, "kind must be 'author' or 'series'")

    cw: CatalogWork | None = None
    if payload.catalog_id is not None:
        cw = db.get(CatalogWork, payload.catalog_id)
        if cw is None:
            raise HTTPException(404, "Catalog entry not found")

    if kind == "author":
        # From a catalog row (cw.author) or, for a library work that has no catalog row, an explicit
        # author name (the detail modal's 'Follow author' passes work.author).
        name = ((cw.author if cw else None) or payload.author_name or "").strip()
        if not name:
            raise HTTPException(400, "Follow an author from a catalog row or an author name")
        key = _author_norm(name)
        display_name = name
    else:  # series
        name = (payload.series_name or "").strip()
        if not name and cw is not None:
            name = ((cw.extra or {}).get("series") or "").strip() if isinstance(cw.extra, dict) else ""
        if not name:
            raise HTTPException(400, "Follow a series from a series name or a catalog row in a series")
        key = norm_title(name)
        display_name = name
    if not key:
        raise HTTPException(400, "Could not derive a follow key")

    existing = db.scalar(select(Subscription).where(
        Subscription.user_id == user.id, Subscription.kind == kind, Subscription.key == key))
    if existing is not None:
        return _out(existing)  # idempotent

    seed = await _seed_keys(db, kind, key, display_name, cw)
    sub = Subscription(user_id=user.id, kind=kind, key=key, display_name=display_name,
                       active=True, auto_request=True, known_keys=seed, auto_added=0)
    db.add(sub)
    try:
        db.commit()
    except IntegrityError:  # a concurrent follow won the unique race (the await above widens it)
        db.rollback()
        won = db.scalar(select(Subscription).where(
            Subscription.user_id == user.id, Subscription.kind == kind, Subscription.key == key))
        if won is not None:
            return _out(won)
        raise
    db.refresh(sub)
    out = _out(sub)
    if kind == "author":
        # Grab the existing back-catalog now (background, non-blocking) in addition to tracking future
        # releases — see _grab_author_backlog_bg. Series follows track future volumes only (the
        # SeriesModal's "Grab all" covers a series backlog explicitly).
        _schedule_author_backlog(user.id, display_name)
    return out


def _owned(db: Session, sub_id: int, user: User) -> Subscription:
    sub = db.get(Subscription, sub_id)
    if sub is None:
        raise HTTPException(404, "Subscription not found")
    if sub.user_id != user.id:
        raise HTTPException(403, "Not your subscription")
    return sub


@router.patch("/subscriptions/{sub_id}", response_model=SubscriptionOut)
def patch_subscription(
    sub_id: int, payload: SubscriptionPatchIn,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> SubscriptionOut:
    """Toggle ``auto_request`` (the off-switch) or ``active``. 403 on a non-owner."""
    sub = _owned(db, sub_id, user)
    if payload.auto_request is not None:
        sub.auto_request = payload.auto_request
    if payload.active is not None:
        sub.active = payload.active
    db.commit()
    db.refresh(sub)
    return _out(sub)


@router.delete("/subscriptions/{sub_id}")
def delete_subscription(
    sub_id: int, user: User = Depends(current_user), db: Session = Depends(get_db),
) -> dict:
    """Unfollow. 403 on a non-owner."""
    sub = _owned(db, sub_id, user)
    db.delete(sub)
    db.commit()
    return {"deleted": 1}
