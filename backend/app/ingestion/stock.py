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
from datetime import timedelta

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
# Backpressure: don't let stock pile unbounded downloads into SABnzbd (a SHARED downloader — other
# apps use it too). When this many stock downloads are already in flight, hold off grabbing more
# pending items this tick; the worker resumes as completions drain. Bounds outstanding stock work
# regardless of how deep the operator queued (or whether SAB is paused/slow).
STOCK_MAX_INFLIGHT = 50
# How long to wait before re-trying a stock item the open-library fallback already couldn't get.
# A cooldown (not a permanent skip): an item unavailable today may be obtainable later (new mirror
# upload / transient block lifted), so issue items cycle back in instead of being excluded forever.
LIBGEN_RETRY_COOLDOWN = timedelta(hours=12)
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
def _migrate_work_links(db: Session, old_id: int, new_id: int) -> None:
    """Move every user's library membership + shelf placement from a replaced stock Work to its
    re-fetched copy, then drop the old Work — so re-fetching a corrupt book never silently removes it
    from users' shelves. Reading position is reset (the chapters are freshly re-imported)."""
    from ..models import BookshelfItem, Chapter, LibraryItem, ReadingState
    if old_id == new_id:
        return
    for li in db.scalars(select(LibraryItem).where(LibraryItem.work_id == old_id)).all():
        dup = db.scalar(select(LibraryItem.id).where(
            LibraryItem.work_id == new_id, LibraryItem.user_id == li.user_id))
        db.delete(li) if dup else setattr(li, "work_id", new_id)
    for bi in db.scalars(select(BookshelfItem).where(BookshelfItem.work_id == old_id)).all():
        dup = db.scalar(select(BookshelfItem.id).where(
            BookshelfItem.work_id == new_id, BookshelfItem.shelf_id == bi.shelf_id))
        db.delete(bi) if dup else setattr(bi, "work_id", new_id)
    db.execute(delete(ReadingState).where(ReadingState.work_id == old_id))
    db.execute(delete(Chapter).where(Chapter.work_id == old_id))
    old = db.get(Work, old_id)
    if old is not None:
        db.delete(old)


def _mark_stocked(db: Session, si: StockItem, work_id: int) -> None:
    # If this item carried a prior Work (a re-fetch after an integrity sweep), carry the users who had
    # the old (corrupt) copy over to the fresh one instead of leaving them with a broken book.
    if si.work_id and si.work_id != work_id:
        _migrate_work_links(db, si.work_id, work_id)
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
    """Sync in-flight StockItems from their DownloadJob's outcome (safety net for the import hook).

    Also rescues items stranded WITHOUT a job: ``_process_pending`` flips a row to ``searching`` and
    commits before the (network) release search, so a worker restart mid-search leaves it ``searching``
    with no ``download_job_id`` — invisible to both this reconcile and the pending sweep. Reset those
    back to ``pending`` so the worker picks them up again."""
    items = db.scalars(select(StockItem).where(StockItem.status.in_(_IN_FLIGHT))).all()
    for si in items:
        if si.download_job_id is None:
            # No job ever attached (e.g. crashed mid-search) → requeue for a fresh attempt.
            si.status = "pending"
            si.error = None
            continue
        job = db.get(DownloadJob, si.download_job_id)
        if job is None:
            continue
        if job.status == "imported" and job.work_id:
            _mark_stocked(db, si, job.work_id)
        elif job.status == "failed":
            si.status = "failed"
            si.error = job.error or "download failed"
    db.commit()


async def _try_libgen(db: Session, si: StockItem, cw: CatalogWork) -> bool:
    """Open-library FALLBACK: search + download + content-verify the work into the stock dir. On
    success marks the item ``stocked`` and returns True; on failure returns False without changing
    the terminal status (the caller sets it). Used when the usenet pipeline can't get a stock item."""
    from . import libgen
    sdir = get_stock_dir(db)
    if not (libgen.configured(db) and sdir):
        return False
    job = await libgen.fetch_for_stock(db, cw, sdir)
    if job is not None:
        si.download_job_id = job.id
    if job is not None and job.status == "imported" and job.work_id:
        _mark_stocked(db, si, job.work_id)
        db.commit()
        log.info("stock: recovered %r via open-library fallback", si.title)
        return True
    db.commit()
    return False


async def retry_failed_via_libgen(db: Session, *, limit: int = 20) -> dict:
    """Retry stock items the usenet pipeline couldn't get (``failed`` / ``unavailable``) through the
    open-library fallback, stocking the ones it can verify. Bounded; an item the fallback also can't
    get right now is put on a cooldown (``LIBGEN_RETRY_COOLDOWN``) so it isn't retried every run but
    DOES cycle back in later — an item unavailable today may be obtainable tomorrow."""
    from . import libgen
    from sqlalchemy import or_
    if not (libgen.configured(db) and get_stock_dir(db)):
        return {"skipped": "open-library fallback or stock dir not configured"}
    cutoff = _utcnow() - LIBGEN_RETRY_COOLDOWN
    items = db.scalars(
        select(StockItem).where(
            StockItem.status.in_(_ISSUE),
            or_(
                # Not yet attempted via the fallback (usenet-failed, no ``open-library:`` tag) →
                # eligible right away.
                StockItem.error.is_(None),
                ~StockItem.error.like("open-library:%"),
                # Already attempted by the fallback → eligible again only once the cooldown elapses,
                # so it cycles back in later instead of being skipped forever.
                StockItem.updated_at < cutoff,
            ),
        ).order_by(StockItem.popularity_norm.desc(), StockItem.id).limit(limit)
    ).all()
    tried = stocked = 0
    for si in items:
        cw = db.get(CatalogWork, si.catalog_work_id) if si.catalog_work_id else None
        if cw is None and si.norm_key and not si.norm_key.startswith("id:"):
            cw = db.scalar(select(CatalogWork).where(CatalogWork.norm_key == si.norm_key)
                           .order_by(CatalogWork.popularity.desc()))
        if cw is None:
            continue
        tried += 1
        if await _try_libgen(db, si, cw):
            stocked += 1
        else:
            # Couldn't get/verify it right now → start the cooldown so it isn't retried every run,
            # but stays eligible for a later attempt. Bump updated_at explicitly: if the error text
            # is unchanged from a prior pass the row wouldn't otherwise be dirty, and the cooldown
            # (which keys off updated_at) would never reset.
            si.status = si.status if si.status in _ISSUE else "unavailable"
            base = si.error or "no verifiable file found"
            si.error = base[:500] if base.startswith("open-library:") else ("open-library: " + base)[:500]
            si.updated_at = _utcnow()
            db.commit()
    log.info("stock libgen-retry: tried=%s stocked=%s (of %s issue items)", tried, stocked, len(items))
    return {"tried": tried, "stocked": stocked}


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
        # Usenet has nothing → try the open-library fallback before giving up.
        if await _try_libgen(db, si, cw):
            return
        si.status, si.error = "unavailable", "no usenet release; open-library fallback found no verifiable file"
        db.commit()
        return
    try:
        job = await downloads.grab_release(db, cw, candidates=cands, user_id=None, kind=STOCK_KIND)
    except IntegrationError as exc:
        if await _try_libgen(db, si, cw):
            return
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
    from . import libgen
    db = SessionLocal()
    try:
        sdir = get_stock_dir(db)
        usenet_ok = stock_configured(db)                      # prowlarr+sabnzbd + stock dir
        libgen_ok = bool(sdir) and libgen.configured(db)      # open-library fallback + stock dir
        if not (usenet_ok or libgen_ok):
            return {"skipped": "not configured"}
        reconcile_stock(db)
        # Backpressure: throttle new grabs by how many stock downloads are already in flight, so we
        # never dump thousands of NZBs into a shared SABnzbd at once (it's also used by other apps).
        inflight = db.scalar(
            select(func.count(StockItem.id)).where(StockItem.status.in_(_IN_FLIGHT))
        ) or 0
        slots = max(0, STOCK_MAX_INFLIGHT - int(inflight))
        if slots <= 0:
            retried = await retry_failed_via_libgen(db, limit=2) if libgen_ok else {}
            return {"processed": 0, "inflight": int(inflight), "throttled": True,
                    "libgen_retry": retried}
        pending = db.scalars(
            select(StockItem).where(StockItem.status == "pending")
            .order_by(StockItem.popularity_norm.desc(), StockItem.id)
            .limit(min(STOCK_PER_TICK, slots))
        ).all()
        for si in pending:
            try:
                await _process_pending(db, si)
            except Exception:  # noqa: BLE001 — one bad item must not stall the queue
                db.rollback()
                si.status, si.error = "failed", "stock processing error"
                db.commit()
                log.exception("stock processing failed for item %s", si.id)
        # Recover a few items the usenet pipeline couldn't get, via the open-library fallback.
        retried = await retry_failed_via_libgen(db, limit=2) if libgen_ok else {}
        return {"processed": len(pending), "libgen_retry": retried}
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
    # Path-boundary check (not a substring prefix): only delete files genuinely inside the stock dir.
    inside = os.path.commonpath([os.path.abspath(si.file_path), os.path.abspath(stock_dir)]) \
        == os.path.abspath(stock_dir)
    try:
        if os.path.isfile(si.file_path) and inside:
            os.remove(si.file_path)
    except OSError:
        log.warning("could not delete stock file %s", si.file_path)


def sweep_integrity(db: Session, *, limit: int = 500) -> dict:
    """Re-check every STOCKED file's structural integrity and re-fetch the bad ones. A file that's
    missing, corrupt, or no longer importable is removed; its (broken) release is recorded so the
    re-fetch won't grab the same one; the shared Work is dropped and its catalog rows un-hooked; and
    the stock item is reset to ``pending`` so the worker fetches a fresh copy (usenet first, then the
    open-library fallback). Returns {checked, corrupt, refetch_queued}."""
    import os

    from . import broken, convert, verify

    sdir = get_stock_dir(db)
    items = db.scalars(
        select(StockItem).where(StockItem.status == "stocked").order_by(StockItem.id).limit(limit)
    ).all()
    checked = corrupt = 0
    for si in items:
        path = si.file_path or (db.get(Work, si.work_id).local_path if si.work_id else None)
        checked += 1
        missing = not (path and os.path.isfile(path))
        ok = (not missing) and verify.check_integrity(path)[0]
        # A stocked Kindle file we can now convert isn't "corrupt" — leave it (separate concern).
        if ok or (path and convert.can_convert(path)):
            continue
        corrupt += 1
        _delete_stock_file(si, sdir)
        # Mark the release that produced this bad file broken, so the re-fetch picks a different one.
        if si.download_job_id:
            job = db.get(DownloadJob, si.download_job_id)
            if job and job.release_key:
                broken.mark_broken(db, {"key": job.release_key, "title": si.title},
                                   reason="corrupt/unimportable stocked file")
        # Un-hook the catalog so NEW acquisitions go through the re-fetch — but KEEP the Work and every
        # user's library entry / progress: the re-fetch migrates them onto the fresh copy (see
        # _mark_stocked → _migrate_work_links). Resetting to pending keeps si.work_id as the rebind
        # target so existing readers aren't silently dropped.
        if si.work_id:
            for cwrow in db.scalars(select(CatalogWork).where(CatalogWork.hooked_work_id == si.work_id)).all():
                cwrow.hooked_work_id = None
            grp = db.get(CatalogGroup, si.catalog_work_id) if si.catalog_work_id else None
            if grp is not None and grp.hooked_work_id == si.work_id:
                grp.hooked_work_id = None
        si.status, si.file_path, si.download_job_id, si.error = "pending", None, None, None
        si.stocked_at = None      # si.work_id kept → users migrate to the re-fetched copy
        db.commit()
    # Orphan pass: corrupt/leftover files in the stock dir that no Work points at (e.g. promoted by
    # an old failed import). Remove the corrupt ones so the pool only holds valid books.
    orphans = 0
    if sdir and os.path.isdir(sdir):
        from .media import is_supported
        kept = {w.local_path for w in db.scalars(
            select(Work).where(Work.local_path.is_not(None))).all() if w.local_path}
        for dp, _dirs, files in os.walk(sdir):
            for f in files:
                fp = os.path.join(dp, f)
                if fp in kept or not is_supported(f):
                    continue
                if not verify.check_integrity(fp)[0] and not convert.can_convert(fp):
                    try:
                        os.remove(fp)
                        orphans += 1
                        if os.path.isdir(dp) and not os.listdir(dp):
                            os.rmdir(dp)
                    except OSError:
                        pass
    log.info("stock integrity sweep: checked=%s corrupt=%s orphans_removed=%s", checked, corrupt, orphans)
    return {"checked": checked, "corrupt": corrupt, "refetch_queued": corrupt, "orphans_removed": orphans}


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
