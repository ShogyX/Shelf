"""Library stocking — operator pre-fetch of catalog works through the Prowlarr/SABnzbd pipeline.

The admin selects catalog works (by media category / genre / theme / popularity) to "stock". Each
selection becomes a :class:`StockItem` (``pending``); a background worker (:func:`stock_tick`) walks
the pending rows at a bounded rate, searches usenet via Prowlarr — for EVERY item, regardless of how
it was originally indexed (web crawl included) — and grabs the best release through SABnzbd as an
operator-owned download (``user_id=None``, ``grab_kind='stock'``). The download imports into a
dedicated STOCK DIRECTORY as a shared, hooked Work, flipping the row to ``stocked``.

Once stocked, the work is already in the global library (its catalog row carries ``hooked_work_id``),
so when any user acquires it ``acquire()`` returns it instantly — stock is checked first, no second
download. Only the SABnzbd pipeline is used; nothing here touches the web-hook / library-manager
routes.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..integrations import IntegrationError
from ..models import (
    AppSetting,
    CatalogGroup,
    CatalogTag,
    CatalogWork,
    DownloadJob,
    StockItem,
    StockJob,
    Work,
)
from . import catalog
from .acquire import pipeline_configured

log = logging.getLogger("shelf.stock")

_STOCK_DIR_KEY = "stock_dir"          # AppSetting: the dedicated directory stocked files land in
STOCK_KIND = "stock"                  # DownloadJob.grab_kind for operator stock fetches
STOCK_PER_TICK = 4                    # pending items searched+grabbed per worker tick (rate cap)
MAX_PER_REQUEST = 5000               # safety cap on a single batch (run several batches for more)
# Statuses still in flight (their DownloadJob drives the final outcome).
_IN_FLIGHT = ("searching", "downloading")
_PENDING = ("pending",)
_ISSUE = ("failed", "unavailable")    # items the operator may need to resolve
_DONE = ("stocked",)


def _utcnow():
    from datetime import UTC, datetime
    return datetime.now(UTC)


# ----------------------------------------------------------------- stock dir
def get_stock_dir(db: Session) -> str | None:
    row = db.get(AppSetting, _STOCK_DIR_KEY)
    v = row.value if row else None
    return (v.strip() or None) if isinstance(v, str) else None


def set_stock_dir(db: Session, path: str | None) -> str | None:
    row = db.get(AppSetting, _STOCK_DIR_KEY)
    val = (path or "").strip() or None
    if val is None:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(AppSetting(key=_STOCK_DIR_KEY, value=val))
    else:
        row.value = val
    db.commit()
    return val


def stock_configured(db: Session) -> bool:
    """Stocking needs the usenet pipeline AND a stock directory set."""
    return pipeline_configured(db) and bool(get_stock_dir(db))


# ----------------------------------------------------------------- selection
def _select_groups(db: Session, *, media: str | None, dimension: str | None, value: str | None,
                   sort: str, limit: int, group_ids: list[int] | None) -> list[CatalogGroup]:
    """Resolve a stocking selection to catalog groups — mirrors the Index browse filters (media
    category / genre / theme / popularity). Already-available (hooked) groups are skipped: they're
    instant already, so there's nothing to stock."""
    sel = select(CatalogGroup).where(CatalogGroup.hooked_work_id.is_(None))
    if group_ids:
        sel = sel.where(CatalogGroup.id.in_(group_ids))
    if dimension in ("genre", "theme") and value:
        sel = sel.join(CatalogTag, CatalogTag.group_id == CatalogGroup.id).where(
            CatalogTag.kind == dimension, CatalogTag.slug == value)
    if media in catalog.MEDIA_CATEGORIES:
        sel = sel.where(CatalogGroup.media_label.in_(catalog.category_labels(media)))
    if sort == "title":
        sel = sel.order_by(CatalogGroup.title.asc())
    elif sort == "new":
        sel = sel.order_by(CatalogGroup.id.desc())
    else:  # popularity (default) — stock the most-wanted first
        sel = sel.order_by(CatalogGroup.popularity_norm.desc(), CatalogGroup.id.desc())
    return list(db.scalars(sel.limit(limit)).all())


def _default_job_name(media: str | None, dimension: str | None, value: str | None,
                      sort: str) -> str:
    """A readable fallback name from the selection (used when the operator didn't name the batch)."""
    parts: list[str] = []
    parts.append(media or "All media")
    if dimension and value:
        parts.append(f"{dimension}: {value}")
    sort_label = {"popularity": "popular", "new": "newest", "title": "A–Z"}.get(sort, sort)
    parts.append(sort_label)
    return " · ".join(parts)


def queue_selection(db: Session, *, name: str | None = None, media: str | None = None,
                    dimension: str | None = None, value: str | None = None,
                    sort: str = "popularity", limit: int = 200,
                    group_ids: list[int] | None = None) -> dict:
    """Create a named :class:`StockJob` and ``pending`` StockItems for the selected catalog groups
    (deduped by norm_key — a title already queued in any batch is skipped). Bounded by ``limit``
    (and a hard ``MAX_PER_REQUEST`` cap). The worker fetches them in the background. Returns the new
    job id/name plus queued/skipped/selected counts."""
    limit = max(1, min(MAX_PER_REQUEST, int(limit or 0) or MAX_PER_REQUEST))
    groups = _select_groups(db, media=media, dimension=dimension, value=value, sort=sort,
                            limit=limit, group_ids=group_ids)
    job = StockJob(
        name=(name or "").strip()[:255] or _default_job_name(media, dimension, value, sort),
        media_category=media, dimension=dimension, value=value, sort=sort, requested=len(groups),
    )
    db.add(job)
    db.flush()  # assign job.id before attaching items
    queued = skipped = 0
    for g in groups:
        nk = g.norm_key or f"id:{g.id}"
        if db.scalar(select(StockItem.id).where(StockItem.norm_key == nk)) is not None:
            skipped += 1
            continue
        db.add(StockItem(
            stock_job_id=job.id,
            norm_key=nk, catalog_work_id=g.id, title=g.title, author=g.author,
            media_label=g.media_label, media_category=catalog.media_category(g.media_label),
            popularity_norm=g.popularity_norm or 0.0, status="pending"))
        queued += 1
    if queued == 0:  # nothing new to fetch → don't leave an empty batch lying around
        db.delete(job)
        db.commit()
        log.info("stock queue: nothing new (skipped=%s of %s selected)", skipped, len(groups))
        return {"job_id": None, "name": job.name, "queued": 0, "skipped": skipped,
                "selected": len(groups)}
    db.commit()
    log.info("stock job %s %r queued=%s skipped=%s (of %s selected)",
             job.id, job.name, queued, skipped, len(groups))
    return {"job_id": job.id, "name": job.name, "queued": queued, "skipped": skipped,
            "selected": len(groups)}


# ----------------------------------------------------------------- worker
def _mark_stocked(db: Session, si: StockItem, work_id: int) -> None:
    si.work_id = work_id
    si.status = "stocked"
    si.error = None
    si.stocked_at = _utcnow()
    w = db.get(Work, work_id)
    if w is not None:
        si.file_path = w.local_path
        si.size = int(w.local_size) if w.local_size else si.size


def on_stock_imported(db: Session, job: DownloadJob) -> None:
    """Hook from the download import path: a stock job's file landed → flip its StockItem to stocked
    and mark the catalog GROUP hooked immediately (so the Index reflects it before the next regroup).
    Best-effort; never raises into the importer."""
    try:
        if (job.grab_kind or "") != STOCK_KIND or not job.work_id:
            return
        si = db.scalar(select(StockItem).where(StockItem.download_job_id == job.id))
        if si is None and job.catalog_work_id:  # fallback: match by the rep's norm_key
            cw = db.get(CatalogWork, job.catalog_work_id)
            if cw is not None:
                si = db.scalar(select(StockItem).where(StockItem.norm_key == (cw.norm_key or "")))
        if si is not None:
            _mark_stocked(db, si, job.work_id)
        if job.catalog_work_id:  # the rep id == its CatalogGroup id
            grp = db.get(CatalogGroup, job.catalog_work_id)
            if grp is not None and grp.hooked_work_id is None:
                grp.hooked_work_id = job.work_id
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("on_stock_imported failed for job %s", job.id)


def reconcile_stock(db: Session) -> None:
    """Sync in-flight StockItems from their DownloadJob's outcome (safety net for the import hook)."""
    items = db.scalars(select(StockItem).where(
        StockItem.status.in_(_IN_FLIGHT), StockItem.download_job_id.isnot(None))).all()
    for si in items:
        job = db.get(DownloadJob, si.download_job_id)
        if job is None:
            continue
        if job.status == "imported" and job.work_id:
            _mark_stocked(db, si, job.work_id)
        elif job.status == "failed":
            si.status = "failed"
            si.error = job.error or "download failed"
    db.commit()


async def _process_pending(db: Session, si: StockItem) -> None:
    """Search usenet for one pending stock item and grab it (operator-owned). Sets the row's status."""
    from . import downloads, release_matcher as rm

    cw = db.get(CatalogWork, si.catalog_work_id) if si.catalog_work_id else None
    if cw is None and si.norm_key and not si.norm_key.startswith("id:"):
        cw = db.scalar(select(CatalogWork).where(CatalogWork.norm_key == si.norm_key)
                       .order_by(CatalogWork.popularity.desc()))
    if cw is None:
        si.status, si.error = "failed", "catalog entry no longer exists"
        db.commit()
        return
    if cw.hooked_work_id:  # became available since queueing → already stocked, no download needed
        _mark_stocked(db, si, cw.hooked_work_id)
        db.commit()
        return

    si.status = "searching"
    db.commit()
    ranked = await rm.find_releases(db, cw)
    cands = rm.candidate_dicts(ranked, cap=downloads.CANDIDATE_CAP, include_speculative=True)
    if not cands:
        si.status, si.error = "unavailable", "no matching usenet release found"
        db.commit()
        return
    try:
        job = await downloads.grab_release(db, cw, candidates=cands, user_id=None, kind=STOCK_KIND)
    except IntegrationError as exc:
        si.status, si.error = "failed", str(exc)
        db.commit()
        return
    si.download_job_id = job.id
    si.error = None
    if job.status == "imported" and job.work_id:
        _mark_stocked(db, si, job.work_id)
    elif job.status == "failed":
        si.status, si.error = "failed", job.error or "download failed"
    else:
        si.status = "downloading"
    db.commit()


async def stock_tick() -> dict:
    """Background worker: advance up to ``STOCK_PER_TICK`` pending stock items + reconcile in-flight
    ones. No-op unless the pipeline + stock directory are configured."""
    from ..db import SessionLocal
    db = SessionLocal()
    try:
        if not stock_configured(db):
            return {"skipped": "not configured"}
        reconcile_stock(db)
        pending = db.scalars(
            select(StockItem).where(StockItem.status == "pending")
            .order_by(StockItem.popularity_norm.desc(), StockItem.id)
            .limit(STOCK_PER_TICK)
        ).all()
        for si in pending:
            try:
                await _process_pending(db, si)
            except Exception:  # noqa: BLE001 — one bad item must not stall the queue
                db.rollback()
                si.status, si.error = "failed", "stock processing error"
                db.commit()
                log.exception("stock processing failed for item %s", si.id)
        return {"processed": len(pending)}
    finally:
        db.close()


def remove_stock(db: Session, stock_id: int, *, delete_file: bool = True) -> bool:
    """Remove a stock item: optionally delete its file from the stock directory, and drop the row.
    The shared Work is left in place (users who already acquired it keep it)."""
    si = db.get(StockItem, stock_id)
    if si is None:
        return False
    if delete_file:
        _delete_stock_file(si, get_stock_dir(db))
    db.delete(si)
    db.commit()
    return True


def _delete_stock_file(si: StockItem, stock_dir: str | None) -> None:
    """Delete a stocked file from disk — only when it's inside the configured stock dir (never a
    user/library file). Best-effort; never raises."""
    import os
    if not (si.file_path and stock_dir):
        return
    try:
        if os.path.isfile(si.file_path) and si.file_path.startswith(stock_dir):
            os.remove(si.file_path)
    except OSError:
        log.warning("could not delete stock file %s", si.file_path)


def summary(db: Session) -> dict:
    """Counts by status across ALL stock items (config-card dashboard)."""
    rows = db.execute(
        select(StockItem.status, func.count(StockItem.id)).group_by(StockItem.status)
    ).all()
    counts = {s: int(c) for s, c in rows}
    return {"counts": counts, "total": sum(counts.values())}


# ----------------------------------------------------------------- named jobs (batches)
def _derive_stats(counts: dict[str, int]) -> dict:
    """Roll per-status counts into the numbers the UI shows: total, progress, in-flight, issues."""
    total = sum(counts.values())
    stocked = sum(counts.get(s, 0) for s in _DONE)
    in_flight = sum(counts.get(s, 0) for s in (_PENDING + _IN_FLIGHT))
    issues = sum(counts.get(s, 0) for s in _ISSUE)
    if total and stocked == total:
        overall = "complete"
    elif issues and in_flight == 0:
        overall = "needs attention"   # nothing left running, but some couldn't be stocked
    elif in_flight:
        overall = "working"
    elif issues:
        overall = "needs attention"
    else:
        overall = "empty"
    return {
        "total": total, "stocked": stocked, "in_flight": in_flight, "issues": issues,
        "pending": counts.get("pending", 0),
        "progress": round(stocked / total, 4) if total else 0.0,
        "overall": overall, "counts": counts,
    }


def _counts_by_job(db: Session) -> dict[int | None, dict[str, int]]:
    """{stock_job_id (or None for legacy ungrouped): {status: count}} in one grouped query."""
    rows = db.execute(
        select(StockItem.stock_job_id, StockItem.status, func.count(StockItem.id))
        .group_by(StockItem.stock_job_id, StockItem.status)
    ).all()
    out: dict[int | None, dict[str, int]] = {}
    for job_id, status, c in rows:
        out.setdefault(job_id, {})[status] = int(c)
    return out


def _stocked_size_by_job(db: Session) -> dict[int | None, int]:
    rows = db.execute(
        select(StockItem.stock_job_id, func.coalesce(func.sum(StockItem.size), 0))
        .where(StockItem.status == "stocked").group_by(StockItem.stock_job_id)
    ).all()
    return {job_id: int(sz or 0) for job_id, sz in rows}


def _job_dict(job: StockJob | None, counts: dict[str, int], size: int) -> dict:
    """Assemble one job's listing row (job may be None for the legacy 'ungrouped' bucket)."""
    base = _derive_stats(counts)
    base["stocked_size"] = size
    if job is None:
        base.update({"id": None, "name": "Ungrouped (queued before batches)",
                     "media_category": None, "dimension": None, "value": None,
                     "sort": None, "requested": base["total"], "created_at": None})
    else:
        base.update({"id": job.id, "name": job.name, "media_category": job.media_category,
                     "dimension": job.dimension, "value": job.value, "sort": job.sort,
                     "requested": job.requested, "created_at": job.created_at})
    return base


def list_jobs(db: Session) -> list[dict]:
    """Every stocking batch with rolled-up progress/issue stats, newest first. Includes a synthetic
    'Ungrouped' bucket for items queued before named jobs existed (if any)."""
    counts = _counts_by_job(db)
    sizes = _stocked_size_by_job(db)
    jobs = db.scalars(select(StockJob).order_by(StockJob.created_at.desc(), StockJob.id.desc())).all()
    out = [_job_dict(j, counts.get(j.id, {}), sizes.get(j.id, 0)) for j in jobs]
    if None in counts:  # legacy ungrouped items
        out.append(_job_dict(None, counts[None], sizes.get(None, 0)))
    return out


def job_detail(db: Session, job_id: int | None) -> dict | None:
    """A single batch with its items + stats. ``job_id`` None/0 → the legacy ungrouped bucket."""
    legacy = job_id in (None, 0)
    job = None if legacy else db.get(StockJob, job_id)
    if not legacy and job is None:
        return None
    sel = select(StockItem).where(
        StockItem.stock_job_id.is_(None) if legacy else StockItem.stock_job_id == job_id
    ).order_by(StockItem.status, StockItem.popularity_norm.desc(), StockItem.id.desc())
    items = list(db.scalars(sel).all())
    if legacy and not items:
        return None
    counts: dict[str, int] = {}
    size = 0
    for it in items:
        counts[it.status] = counts.get(it.status, 0) + 1
        if it.status == "stocked" and it.size:
            size += int(it.size)
    info = _job_dict(job, counts, size)
    info["items"] = items
    # Surface the issues explicitly so the operator can resolve them.
    info["problem_items"] = [it for it in items if it.status in _ISSUE]
    return info


def remove_job(db: Session, job_id: int | None, *, delete_files: bool = False) -> bool:
    """Delete a batch and all its items (optionally deleting stocked files). ``job_id`` None/0 →
    the legacy ungrouped bucket. The shared Works stay (users who acquired them keep them)."""
    legacy = job_id in (None, 0)
    job = None if legacy else db.get(StockJob, job_id)
    if not legacy and job is None:
        return False
    cond = StockItem.stock_job_id.is_(None) if legacy else StockItem.stock_job_id == job_id
    items = db.scalars(select(StockItem).where(cond)).all()
    if legacy and not items:
        return False
    if delete_files:
        sdir = get_stock_dir(db)
        for si in items:
            _delete_stock_file(si, sdir)
    # One transaction: drop all the rows (and the job) atomically rather than a commit per item.
    db.execute(delete(StockItem).where(cond))
    if job is not None:
        db.delete(job)
    db.commit()
    return True


def retry_job_issues(db: Session, job_id: int | None) -> int:
    """Reset a batch's failed/unavailable items back to ``pending`` so the worker retries them —
    the operator's 'resolve the issues' action. Returns how many were requeued."""
    legacy = job_id in (None, 0)
    sel = select(StockItem).where(
        StockItem.status.in_(_ISSUE),
        StockItem.stock_job_id.is_(None) if legacy else StockItem.stock_job_id == job_id,
    )
    rows = db.scalars(sel).all()
    for si in rows:
        si.status = "pending"
        si.error = None
        si.download_job_id = None
    db.commit()
    return len(rows)
