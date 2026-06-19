"""Per-user Goodreads connection.

Goodreads is per-user — unlike the operator-wide library managers / metadata providers (admin-only
on the ``/integrations`` surface). Each user connects their own public want-to-read shelf and its
titles auto-hook into THAT user's library + their ``goodreads_target`` bookshelf. So this surface is
auth-gated (any logged-in user), and a user's connection is just an
``Integration(kind="goodreads", user_id=<caller>)`` — already understood by the sync pipeline.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..integrations import metadata_sync
from ..models import Integration, User
from ..schemas import GoodreadsIn, GoodreadsOut

router = APIRouter()


def _mine(db: Session, user_id: int) -> Integration | None:
    return db.scalar(
        select(Integration).where(
            Integration.kind == "goodreads", Integration.user_id == user_id
        ).order_by(Integration.id)
    )


def _out(integ: Integration | None) -> GoodreadsOut:
    if integ is None:
        return GoodreadsOut(connected=False)
    cfg = integ.config or {}
    return GoodreadsOut(
        connected=True, id=integ.id, enabled=integ.enabled,
        goodreads_user_id=str(cfg.get("user_id") or integ.base_url or ""),
        shelf=cfg.get("shelf") or "to-read",
        last_sync_at=integ.last_sync_at, last_error=integ.last_error,
    )


async def _import(db: Session, integ: Integration) -> None:
    """Run the wishlist import for this connection, stamping sync status (best-effort)."""
    try:
        await metadata_sync.import_goodreads(db, integ)
        integ.last_sync_at = datetime.now(UTC)
        integ.last_error = None
    except Exception as exc:  # noqa: BLE001 — surface the error, don't 500 the request
        integ.last_error = str(exc)
    db.commit()


@router.get("/me/goodreads", response_model=GoodreadsOut)
def get_my_goodreads(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> GoodreadsOut:
    return _out(_mine(db, user.id))


@router.put("/me/goodreads", response_model=GoodreadsOut)
async def connect_my_goodreads(
    payload: GoodreadsIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> GoodreadsOut:
    """Create or update the caller's Goodreads connection, then import the shelf once."""
    gid = (payload.goodreads_user_id or "").strip()
    if not gid:
        raise HTTPException(400, "A Goodreads user ID (or profile URL) is required.")
    shelf = (payload.shelf or "to-read").strip() or "to-read"
    integ = _mine(db, user.id)
    if integ is None:
        integ = Integration(kind="goodreads", name=f"Goodreads ({user.username})",
                            base_url=gid, api_key="", user_id=user.id, enabled=True)
        db.add(integ)
        # First connection: provision the default "Goodreads" destination shelf for imported titles.
        from ..library import ensure_named_shelf
        ensure_named_shelf(db, user.id, "Goodreads", goodreads_target=True)
    integ.base_url = gid
    integ.config = {"user_id": gid, "shelf": shelf}
    if payload.enabled is not None:
        integ.enabled = payload.enabled
    db.commit()
    db.refresh(integ)
    if integ.enabled:
        await _import(db, integ)
        db.refresh(integ)
    return _out(integ)


@router.delete("/me/goodreads")
def disconnect_my_goodreads(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    """Disconnect (delete the connection). Already-hooked titles stay in the library; pending
    Goodreads auto-hooks are left to resolve or expire on their own."""
    integ = _mine(db, user.id)
    if integ is None:
        raise HTTPException(404, "No Goodreads connection.")
    db.delete(integ)
    db.commit()
    return {"disconnected": True}


@router.post("/me/goodreads/sync", response_model=GoodreadsOut)
async def sync_my_goodreads(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> GoodreadsOut:
    integ = _mine(db, user.id)
    if integ is None:
        raise HTTPException(404, "No Goodreads connection.")
    await _import(db, integ)
    db.refresh(integ)
    return _out(integ)
