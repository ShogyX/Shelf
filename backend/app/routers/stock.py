"""Library stocking API (admin-only).

Configure the stock directory, queue catalog works to pre-fetch through Prowlarr/SABnzbd, and view
the stock pool. Stocked works become shared hooked Works, so a user acquiring one gets it instantly.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..ingestion import stock as stock_mod
from ..ingestion.acquire import pipeline_configured
from ..models import StockItem
from ..schemas import StockConfigIn, StockItemOut, StockQueueIn, StockSummaryOut

router = APIRouter()


def _summary(db: Session) -> StockSummaryOut:
    s = stock_mod.summary(db)
    return StockSummaryOut(
        configured=stock_mod.stock_configured(db),
        pipeline_configured=pipeline_configured(db),
        stock_dir=stock_mod.get_stock_dir(db),
        counts=s["counts"], total=s["total"],
    )


@router.get("/stock/summary", response_model=StockSummaryOut)
def stock_summary(db: Session = Depends(get_db)) -> StockSummaryOut:
    return _summary(db)


@router.put("/stock/config", response_model=StockSummaryOut)
def set_stock_config(payload: StockConfigIn, db: Session = Depends(get_db)) -> StockSummaryOut:
    """Set the dedicated stock directory (where stocked files are stored)."""
    stock_mod.set_stock_dir(db, payload.stock_dir)
    return _summary(db)


@router.get("/stock", response_model=list[StockItemOut])
def list_stock(
    status: str | None = Query(None, description="Filter by status"),
    media: str | None = Query(None, description="Filter by media category"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[StockItem]:
    sel = select(StockItem)
    if status:
        sel = sel.where(StockItem.status == status)
    if media:
        sel = sel.where(StockItem.media_category == media)
    sel = sel.order_by(StockItem.status, StockItem.popularity_norm.desc(), StockItem.id.desc())
    return list(db.scalars(sel.limit(limit).offset(offset)).all())


@router.post("/stock/queue", response_model=dict)
def queue_stock(payload: StockQueueIn, db: Session = Depends(get_db)) -> dict:
    """Queue catalog works to stock — a filtered selection (media/genre/theme/popularity, capped by
    ``limit``) or explicit ``group_ids``. They're fetched in the background through SABnzbd."""
    if not stock_mod.stock_configured(db):
        raise HTTPException(
            409,
            "Stocking needs the Prowlarr+SABnzbd pipeline AND a stock directory. "
            "Configure them under Settings → Integrations and on this page.",
        )
    res = stock_mod.queue_selection(
        db, media=payload.media, dimension=payload.dimension, value=payload.value,
        sort=payload.sort, limit=payload.limit, group_ids=payload.group_ids,
    )
    return res


@router.delete("/stock/{stock_id}", response_model=dict)
def delete_stock(
    stock_id: int,
    delete_file: bool = Query(True, description="also delete the stocked file from disk"),
    db: Session = Depends(get_db),
) -> dict:
    if not stock_mod.remove_stock(db, stock_id, delete_file=delete_file):
        raise HTTPException(404, "Stock item not found")
    return {"deleted": stock_id}


@router.post("/stock/clear", response_model=dict)
def clear_stock(
    status: str = Query(..., description="Remove all stock items in this status (e.g. unavailable)"),
    db: Session = Depends(get_db),
) -> dict:
    """Bulk-remove stock rows in a terminal status (unavailable / failed) — file-free housekeeping."""
    if status not in ("unavailable", "failed", "pending"):
        raise HTTPException(400, "Only 'unavailable', 'failed' or 'pending' rows can be bulk-cleared.")
    rows = db.scalars(select(StockItem).where(StockItem.status == status)).all()
    n = 0
    for si in rows:
        db.delete(si)
        n += 1
    db.commit()
    return {"deleted": n}
