"""Mass rescan — queue many missing titles for a SEQUENTIAL, batched re-acquire.

Generalizes the per-title ``POST /missing/{id}/recheck`` (reset_sources + acquire force) to a whole
scope (all | author | series | ids). Queuing only stamps ``rescan_queued_at`` + resets the per-source
search rows; the actual searching is done by :func:`rescan_drain_tick`, which picks the oldest queued
rows a small batch at a time and re-acquires them ONE BY ONE (never in parallel) so per-source rate
limits are honored (acquire→source_state already gates over-quota sources). A ``rescan_run``
AppSetting tracks the run total so the UI can show a progress strip; it's cleared when the queue
empties.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config_store
from ..models import AppSetting, CatalogWork, ContentRequest

log = logging.getLogger("shelf.rescan")

# The AppSetting key holding the active run's {total, started_at}. Absent/empty = idle.
RESCAN_RUN_KEY = "rescan_run"
# Statuses that are SEARCHABLE — the only rows a rescan queues. planned (not released yet) and
# resolved (already in the library) are excluded.
_SEARCHABLE = ("unavailable", "open")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _queued_count(db: Session) -> int:
    return int(db.scalar(select(func.count(ContentRequest.id)).where(
        ContentRequest.rescan_queued_at.is_not(None))) or 0)


def queue_rescan(db: Session, *, scope: str, author: str | None = None,
                 series: str | None = None, ids: list[int] | None = None) -> int:
    """Stamp ``rescan_queued_at`` on every SEARCHABLE ledger row matching ``scope`` (and reset its
    per-source rows + attempts), then update the ``rescan_run`` total. Returns how many were queued.

    Scope:
      all     → every searchable row.
      author  → searchable rows whose ContentRequest.author matches (case-insensitive).
      series  → searchable rows whose joined CatalogWork.extra ``$.series`` matches.
      ids     → searchable rows among the given ContentRequest ids."""
    from . import source_state

    sel = select(ContentRequest).where(ContentRequest.status.in_(_SEARCHABLE))
    if scope == "author":
        sel = sel.where(func.lower(ContentRequest.author) == func.lower(author or ""))
    elif scope == "series":
        sel = sel.join(CatalogWork, CatalogWork.id == ContentRequest.catalog_work_id).where(
            func.json_extract(CatalogWork.extra, "$.series") == (series or ""))
    elif scope == "ids":
        sel = sel.where(ContentRequest.id.in_(ids or []))
    elif scope != "all":
        return 0

    rows = db.scalars(sel).all()
    was_empty = _queued_count(db) == 0
    now = _utcnow()
    n = 0
    for row in rows:
        if row.rescan_queued_at is not None:   # already queued this run → don't double-count
            continue
        row.rescan_queued_at = now
        row.attempts = 0
        n += 1
    db.commit()
    for row in rows:                            # reset sources AFTER the queue commit (its own commits)
        source_state.reset_sources(db, row)

    if n:
        run = db.get(AppSetting, RESCAN_RUN_KEY)
        prev = run.value if run and isinstance(run.value, dict) else None
        if was_empty or not prev:
            val = {"total": n, "started_at": now.isoformat()}
        else:
            val = {"total": int(prev.get("total", 0)) + n,
                   "started_at": prev.get("started_at", now.isoformat())}
        if run is None:
            db.add(AppSetting(key=RESCAN_RUN_KEY, value=val))
        else:
            run.value = val
        db.commit()
    return n


def rescan_status(db: Session) -> dict:
    """``{total, done, queued, active}`` for the progress strip. ``total`` is the run size (0 idle),
    ``queued`` the rows still holding the marker, ``done = max(0, total - queued)``, ``active`` iff
    anything is still queued."""
    queued = _queued_count(db)
    run = db.get(AppSetting, RESCAN_RUN_KEY)
    total = int(run.value.get("total", 0)) if run and isinstance(run.value, dict) else 0
    return {"total": total, "done": max(0, total - queued),
            "queued": queued, "active": queued > 0}


def _cw_for_request(db: Session, row: ContentRequest) -> CatalogWork | None:
    """The representative CatalogWork to re-acquire ``row`` from (its catalog_work_id, else the most-
    popular catalog row in its norm_key cluster) — mirrors the per-title recheck endpoint."""
    cw = db.get(CatalogWork, row.catalog_work_id) if row.catalog_work_id else None
    if cw is None and row.norm_key:
        cw = db.scalar(select(CatalogWork).where(CatalogWork.norm_key == row.norm_key)
                       .order_by(CatalogWork.popularity.desc()))
    return cw


async def rescan_drain_tick(db: Session) -> None:
    """Drain the mass-rescan queue: pick the oldest ``missing_recheck_batch``-capped rows holding
    ``rescan_queued_at`` and, for each SEQUENTIALLY, resolve its representative CatalogWork and
    ``acquire(force=True)``, then clear ``rescan_queued_at``. One-by-one with try/except so one bad
    title can't stall the queue; per-source rate limits are honored by acquire→source_state. When the
    queue empties, the ``rescan_run`` AppSetting is cleared (idle)."""
    from .acquire import acquire, user_priority

    batch = max(1, int(config_store.effective("missing_recheck_batch")))
    rows = db.scalars(
        select(ContentRequest)
        .where(ContentRequest.rescan_queued_at.is_not(None))
        .order_by(ContentRequest.rescan_queued_at)
        .limit(batch)
    ).all()
    for row in rows:
        cw = _cw_for_request(db, row)
        if cw is not None:
            try:
                await acquire(db, cw, user_id=None, priority=user_priority(db, None), force=True)
            except Exception:  # noqa: BLE001 — one bad title must not stall the queue
                db.rollback()
                log.exception("rescan_drain_tick: re-acquire failed for %r", row.title)
        # Clear the marker whether or not a catalog row existed, so the queue always drains.
        db.refresh(row)
        row.rescan_queued_at = None
        db.commit()

    # Queue empty → end the run (clear the progress AppSetting so status reports idle).
    if _queued_count(db) == 0:
        run = db.get(AppSetting, RESCAN_RUN_KEY)
        if run is not None:
            db.delete(run)
            db.commit()
