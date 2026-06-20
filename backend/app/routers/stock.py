"""Library stocking API (admin-only).

Configure the stock directory, queue catalog works to pre-fetch through Prowlarr/SABnzbd, and view
the stock pool. Stocked works become shared hooked Works, so a user acquiring one gets it instantly.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..ingestion import stock as stock_mod
from ..ingestion.acquire import pipeline_configured
from ..schemas import (
    StockConfigIn,
    StockJobDetailOut,
    StockJobOut,
    StockQueueIn,
    StockSummaryOut,
)

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
        db, name=payload.name, media=payload.media, dimension=payload.dimension,
        value=payload.value, sort=payload.sort, limit=payload.limit, group_ids=payload.group_ids,
        variant=payload.variant,
    )
    return res


@router.get("/stock/jobs", response_model=list[StockJobOut])
def list_stock_jobs(db: Session = Depends(get_db)) -> list[dict]:
    """Every named stocking batch with rolled-up progress + issue stats (newest first)."""
    return stock_mod.list_jobs(db)


@router.get("/stock/jobs/{job_id}", response_model=StockJobDetailOut)
def stock_job_detail(job_id: int, db: Session = Depends(get_db)) -> dict:
    """One batch: its titles, progress, stats, and the items that need attention. ``job_id`` 0 → the
    legacy ungrouped pool."""
    detail = stock_mod.job_detail(db, job_id)
    if detail is None:
        raise HTTPException(404, "Stock job not found")
    return detail


@router.post("/stock/jobs/{job_id}/retry", response_model=dict)
def retry_stock_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    """Requeue this batch's failed/unavailable items so the worker tries them again."""
    return {"requeued": stock_mod.retry_job_issues(db, job_id)}


@router.delete("/stock/jobs/{job_id}", response_model=dict)
def delete_stock_job(
    job_id: int,
    delete_files: bool = Query(False, description="also delete stocked files from disk"),
    db: Session = Depends(get_db),
) -> dict:
    """Delete a whole batch (and its items). Shared Works stay; pass delete_files to also remove the
    stocked files."""
    if not stock_mod.remove_job(db, job_id, delete_files=delete_files):
        raise HTTPException(404, "Stock job not found")
    return {"deleted": job_id}


@router.delete("/stock/{stock_id}", response_model=dict)
def delete_stock(
    stock_id: int,
    delete_file: bool = Query(True, description="also delete the stocked file from disk"),
    db: Session = Depends(get_db),
) -> dict:
    if not stock_mod.remove_stock(db, stock_id, delete_file=delete_file):
        raise HTTPException(404, "Stock item not found")
    return {"deleted": stock_id}

