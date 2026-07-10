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
import json
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
from . import catalog, ledger
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
# Open-library (libgen) recovery runs on its own tick; bound how much it does per run so a single
# run can't sprawl across minutes on slow/blocked mirrors (which would get successive runs skipped).
STOCK_LIBGEN_PER_TICK = 3            # issue items attempted per libgen recovery tick
STOCK_LIBGEN_BUDGET_S = 90.0         # wall-clock budget per libgen recovery tick (stops between items)
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
    if isinstance(v, str) and v.strip():
        return v.strip()
    # No admin override → fall back to the env/config default (SHELF_STOCK_DIR), so the pool location
    # survives a DB reset and is the app's durable default rather than a per-instance runtime setting.
    from ..config import get_settings
    return (get_settings().stock_dir.strip() or None)


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


# ----------------------------------------------------------------- daily caps
# Operator-set daily throttle on stock activity. A "search" = one stock item searched on the usenet
# pipeline; a "download" = one release grabbed via SABnzbd. Both 0 = unlimited.
#
# Counting approach: a single durable per-day counter persisted in one AppSetting row
# (``stock_usage`` → ``{"date": "YYYY-MM-DD", "searches": N, "downloads": M}``). It's bumped at the
# moment a search/grab is actually performed in ``_process_pending`` and committed in the same
# transaction as the item's state change, so it never double-counts across a worker restart (it's an
# incrementing integer, not a recompute) and a counted action is always persisted alongside its
# effect. The counter auto-resets when the UTC date stored in the row no longer matches today — no
# cleanup job needed. A single worker writes it (stock_tick), so no write contention.
_STOCK_USAGE_KEY = "stock_usage"


def _today_utc() -> str:
    return _utcnow().strftime("%Y-%m-%d")


def _usage(db: Session) -> dict:
    """Today's stock usage counters {searches, downloads}; a stored counter from a previous UTC day
    reads as zero (the new day hasn't been written yet)."""
    row = db.get(AppSetting, _STOCK_USAGE_KEY)
    raw = row.value if row else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = None
    if not isinstance(raw, dict) or raw.get("date") != _today_utc():
        return {"searches": 0, "downloads": 0}
    return {"searches": int(raw.get("searches") or 0), "downloads": int(raw.get("downloads") or 0)}


def _bump_usage(db: Session, *, searches: int = 0, downloads: int = 0) -> None:
    """Increment today's durable per-day usage counters (resetting them if the stored day rolled).
    Does NOT commit — the caller commits it together with the item state change it accounts for."""
    cur = _usage(db)
    val = json.dumps({"date": _today_utc(),
                      "searches": cur["searches"] + searches,
                      "downloads": cur["downloads"] + downloads})
    row = db.get(AppSetting, _STOCK_USAGE_KEY)
    if row is None:
        db.add(AppSetting(key=_STOCK_USAGE_KEY, value=val))
    else:
        row.value = val


def _cap(field: str) -> int:
    """The configured daily cap for ``field`` (0/invalid → 0 = unlimited)."""
    from .. import config_store
    try:
        return max(0, int(config_store.effective(field)))
    except (TypeError, ValueError):
        return 0


def daily_caps(db: Session) -> dict:
    """Current daily caps + how much of each is used today (for the summary / UI gauge)."""
    used = _usage(db)
    return {
        "searches_per_day": _cap("stock_searches_per_day"),
        "downloads_per_day": _cap("stock_downloads_per_day"),
        "searches_used_today": used["searches"],
        "downloads_used_today": used["downloads"],
    }


def _remaining(cap: int, used: int) -> int | None:
    """Slots left under ``cap`` (None = unlimited). Never negative."""
    return None if cap <= 0 else max(0, cap - used)


# ----------------------------------------------------------------- selection
# Languages the usenet/verify pipeline can actually obtain+confirm (en-only indexers/verify). A
# catalog title in another language (ja/ko/zh — tens of thousands of them) can NEVER match an
# English usenet release and would only waste grabs, so auto-selection is gated to English.
# Auto-selection is gated to the instance's configured content languages (config_store.content_languages,
# default ["en"]). Each code is matched as a PREFIX so every spelling/variant is accepted — "en"/"eng"/
# "english"/"en-us", "no"/"nob"/"norwegian"/"norsk". A group with no language tag is treated as English
# (coalesce below). Empty config (blank/"*") = no language restriction.


def _select_groups(db: Session, *, media: str | None, dimension: str | None, value: str | None,
                   sort: str, limit: int, group_ids: list[int] | None,
                   exclude_web_index: bool = False) -> list[CatalogGroup]:
    """Resolve a stocking selection to catalog groups — mirrors the Index browse filters (media
    category / genre / theme / popularity). Already-available (hooked) groups AND titles already in
    the stock list (any status) are skipped, so a selection only ever picks genuinely-new titles and
    its ``limit`` isn't wasted on things we already have.

    Auto-selection (no explicit ``group_ids``) additionally requires a real popularity signal and a
    pipeline-obtainable language, so a "Top N" never degenerates into the zero-popularity / foreign
    long tail that the usenet pipeline can't match (the dominant cause of near-zero stock yield)."""
    sel = select(CatalogGroup).where(
        CatalogGroup.hooked_work_id.is_(None),
        # Skip anything already represented in the stock list (queued/in-flight/stocked/issue) — keyed
        # by norm_key, the same identity the queue dedupes on.
        CatalogGroup.norm_key.notin_(select(StockItem.norm_key)),
    )
    if group_ids:
        sel = sel.where(CatalogGroup.id.in_(group_ids))
    else:
        # An operator-curated explicit selection is trusted as-is; a broad auto-selection is gated so
        # it can't pull zero-popularity or out-of-language titles the pipeline can't obtain/verify.
        sel = sel.where(CatalogGroup.popularity_norm > 0)
        from .. import config_store
        langs = config_store.content_languages()
        if langs:
            from sqlalchemy import or_
            col = func.lower(func.coalesce(CatalogGroup.language, "en"))
            sel = sel.where(or_(*[col.like(f"{c}%") for c in langs]))
    if dimension in ("genre", "theme") and value:
        sel = sel.join(CatalogTag, CatalogTag.group_id == CatalogGroup.id).where(
            CatalogTag.kind == dimension, CatalogTag.slug == value)
    if exclude_web_index:
        # Keep only groups with ≥1 non-crawled member (Readarr/Kapowarr/…). A group whose members
        # are ONLY web_index (crawled web-novel/scanlation sources) is dropped — the operator wants
        # to stock from real obtainable sources, not crawled-only titles.
        sel = sel.where(
            select(CatalogWork.id).where(
                CatalogWork.group_id == CatalogGroup.id,
                CatalogWork.provider != "web_index",
            ).exists()
        )
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
                    group_ids: list[int] | None = None, variant: str = "ebook",
                    entire_catalog: bool = False, exclude_web_index: bool = False) -> dict:
    """Create a named :class:`StockJob` and ``pending`` StockItems for the selected catalog groups
    (deduped by norm_key — a title already queued in any batch is skipped). Bounded by ``limit``
    (and a hard ``MAX_PER_REQUEST`` cap). The worker fetches them in the background. Returns the new
    job id/name plus queued/skipped/selected counts.

    ``entire_catalog``: stock the WHOLE catalog (ignore the media/dimension/value filter), capped at
    ``limit`` (defaulting to the ``MAX_PER_REQUEST`` safety cap). ``exclude_web_index``: drop groups
    whose only members are crawled web sources."""
    limit = max(1, min(MAX_PER_REQUEST, int(limit or 0) or MAX_PER_REQUEST))
    if entire_catalog:                       # whole-catalog stock → clear the narrowing filter
        media = dimension = value = None
    groups = _select_groups(db, media=media, dimension=dimension, value=value, sort=sort,
                            limit=limit, group_ids=group_ids, exclude_web_index=exclude_web_index)
    job = StockJob(
        name=(name or "").strip()[:255] or _default_job_name(media, dimension, value, sort),
        media_category=media, dimension=dimension, value=value, sort=sort, requested=len(groups),
        variant=(variant if variant in ("ebook", "audiobook", "both") else "ebook"),
    )
    db.add(job)
    db.flush()  # assign job.id before attaching items
    queued = skipped = 0
    from ..db import insert_or_reuse
    for g in groups:
        nk = g.norm_key or f"id:{g.id}"
        if db.scalar(select(StockItem.id).where(StockItem.norm_key == nk)) is not None:
            skipped += 1
            continue
        # insert_or_reuse: a CONCURRENT queue request can insert the same norm_key between our
        # check and this insert — the unique index turns that into a savepoint-contained
        # collision (counted as skipped) instead of a duplicate item / aborted batch.
        _item, created = insert_or_reuse(db, StockItem(
            stock_job_id=job.id,
            norm_key=nk, catalog_work_id=g.id, title=g.title, author=g.author,
            media_label=g.media_label, media_category=catalog.media_category(g.media_label),
            popularity_norm=g.popularity_norm or 0.0, status="pending"),
            select(StockItem).where(StockItem.norm_key == nk))
        if created:
            queued += 1
        else:
            skipped += 1
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
    # IDEMPOTENT: three uncoordinated paths can mark the same item stocked (the poll import hook,
    # reconcile_stock, and the inline pending path), interleaving at awaits. If it's already stocked
    # for THIS work, do nothing — re-running _migrate_work_links for the same old→new id would
    # double-move/double-delete LibraryItem/Chapter rows.
    if si.status == "stocked" and si.work_id == work_id:
        return
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
    # Title is now stocked → clear any missing-content gate (no-op if never recorded). Stock fetches
    # that imported via the usenet/libgen paths already resolved there; this also covers the
    # already-available and re-fetch paths that bypass an import (Stage 1).
    cw = _stock_cw(db, si)
    if cw is not None:
        ledger.mark_resolved(db, cw)


def _stock_cw(db: Session, si: StockItem) -> CatalogWork | None:
    """The representative catalog row for a stock item (by its catalog_work_id, else its norm_key) —
    the handle the missing-content ledger keys off."""
    cw = db.get(CatalogWork, si.catalog_work_id) if si.catalog_work_id else None
    if cw is None and si.norm_key and not si.norm_key.startswith("id:"):
        cw = db.scalar(select(CatalogWork).where(CatalogWork.norm_key == si.norm_key)
                       .order_by(CatalogWork.popularity.desc()))
    return cw


def on_stock_imported(db: Session, job: DownloadJob, *, hook_group: bool = True) -> None:
    """Hook from the download import path: a stock job's file landed → flip its StockItem to stocked
    and mark the catalog GROUP hooked immediately (so the Index reflects it before the next regroup).
    ``hook_group=False`` for an AUDIOBOOK import: the catalog group represents the EBOOK, so an audio
    Work must not become its hooked work. Best-effort; never raises into the importer."""
    try:
        if (job.grab_kind or "") != STOCK_KIND or not job.work_id:
            return
        si = db.scalar(select(StockItem).where(StockItem.download_job_id == job.id))
        # norm_key fallback is EBOOK-only: a 'both' batch's StockItem tracks the EBOOK job, so an audio
        # job must NOT resolve to it by norm_key (that would flip the ebook item to the audio Work and,
        # when the ebook then imports, _migrate_work_links would DELETE the audiobook). An audio job
        # only ever flips a StockItem that explicitly points at it (audiobook-only batch).
        if si is None and (job.fmt or "") != "audio" and job.catalog_work_id:
            cw = db.get(CatalogWork, job.catalog_work_id)
            if cw is not None:
                si = db.scalar(select(StockItem).where(StockItem.norm_key == (cw.norm_key or "")))
        if si is not None:
            _mark_stocked(db, si, job.work_id)
        if hook_group and job.catalog_work_id:  # the rep id == its CatalogGroup id
            grp = db.get(CatalogGroup, job.catalog_work_id)
            work = db.get(Work, job.work_id)
            # Invariant backstop: hooked_work_id must never point at an audio Work (audio is
            # surfaced via audiobook pairing, not hooks) — even if a caller passes hook_group=True.
            if (grp is not None and grp.hooked_work_id is None
                    and work is not None and (work.media_kind or "") != "audio"):
                grp.hooked_work_id = job.work_id
        db.commit()
        from .. import cache
        cache.clear_catalog()   # newly stocked/hooked → refresh baked in_stock state (throttled)
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
            # The referenced job was pruned (e.g. a transient-retry libgen job that exhausted its
            # retries, then aged out via cleanup_jobs) — the item only ever reconciled on
            # imported/failed, so it would otherwise stay 'downloading' forever with no completion
            # path. Re-queue it for a fresh attempt rather than stranding it.
            si.status = "pending"
            si.error = None
            si.download_job_id = None
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


async def retry_failed_via_libgen(db: Session, *, limit: int = 20,
                                  budget_s: float = STOCK_LIBGEN_BUDGET_S) -> dict:
    """Retry stock items the usenet pipeline couldn't get (``failed`` / ``unavailable``) through the
    open-library fallback, stocking the ones it can verify. Bounded by ``limit`` AND a wall-clock
    ``budget_s`` (checked between items, since a single download on a slow mirror can take a while) —
    an item the fallback also can't get right now is put on a cooldown (``LIBGEN_RETRY_COOLDOWN``) so
    it isn't retried every run but DOES cycle back in later (unavailable today may be obtainable
    tomorrow)."""
    import time as _time
    from . import libgen
    from sqlalchemy import or_
    if not (libgen.configured(db) and get_stock_dir(db)):
        return {"skipped": "open-library fallback or stock dir not configured"}
    deadline = _time.monotonic() + budget_s
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
        if _time.monotonic() >= deadline:        # out of time → stop; the rest retry next tick
            break
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

    # What this batch stocks. An audiobook is a SEPARATE Work (audio categories → audiobook path), so
    # an audiobook batch ignores the ebook's hooked/ledger state.
    job_row = db.get(StockJob, si.stock_job_id) if si.stock_job_id else None
    batch_variant = getattr(job_row, "variant", None) or "ebook"
    audio_primary = batch_variant == "audiobook"
    primary = "audiobook" if audio_primary else "ebook"

    if cw.hooked_work_id and not audio_primary:  # ebook already available → already stocked
        _mark_stocked(db, si, cw.hooked_work_id)
        db.commit()
        return

    # GATE (ebook only): skip searching a title the missing-content ledger knows is unavailable until
    # its re-check is due. Audiobook availability isn't tracked by the ledger, so it isn't gated.
    if not audio_primary:
        gated, next_check = ledger.is_gated(db, cw)
        if gated:
            si.status = "unavailable"
            si.error = f"gated by missing-content ledger; next re-check {next_check:%Y-%m-%d %H:%M}"
            db.commit()
            return

    si.status = "searching"
    # Account the search against today's cap in the same commit that flips the row to 'searching'
    # (the durable, restart-safe per-day counter).
    _bump_usage(db, searches=1)
    db.commit()
    ranked = await rm.find_releases(db, cw, variant=primary)
    cands = rm.candidate_dicts(ranked, cap=downloads.CANDIDATE_CAP, include_speculative=True)
    if not cands:
        si.status = "unavailable"
        if audio_primary:
            si.error = "no audiobook release found"
        else:
            # Usenet has nothing → hand off to the open-library worker on its own schedule; record
            # unavailable so the title is gated + periodically re-checked (Stage 1).
            si.error = "no usenet release; queued for the open-library fallback worker"
            ledger.mark_unavailable(db, cw, reason="no_match", provider="pipeline")
        db.commit()
        return
    # Daily download cap: a release was found, but no grab budget left today → leave the item PENDING
    # (don't grab, don't fail) so it's retried tomorrow when the cap resets. The search above still
    # counted (it did happen).
    dl_cap = _cap("stock_downloads_per_day")
    if dl_cap > 0 and _usage(db)["downloads"] >= dl_cap:
        si.status = "pending"
        si.error = None
        db.commit()
        return
    try:
        job = await downloads.grab_release(db, cw, candidates=cands, user_id=None,
                                           kind=STOCK_KIND, variant=primary)
    except IntegrationError as exc:
        # SAB unreachable → mark as an issue; the open-library worker (not stock_tick) retries it.
        si.status, si.error = "failed", str(exc)
        db.commit()
        return
    _bump_usage(db, downloads=1)            # a release was grabbed → count it against today's cap
    si.download_job_id = job.id
    si.error = None
    if job.status == "imported" and job.work_id:
        _mark_stocked(db, si, job.work_id)
    elif job.status == "failed":
        si.status, si.error = "failed", job.error or "download failed"
    else:
        si.status = "downloading"
    db.commit()
    # 'both': also fetch the audiobook (operator-owned, untracked by this StockItem; the ebook above is
    # the one whose progress the StockItem reflects). It now rides the per-format missing-content ledger
    # just like the ebook: a miss is recorded so source_retry_tick re-checks the AUDIOBOOK periodically
    # (a found one is cleared on import via _import_audiobook), instead of giving up after one try.
    if batch_variant == "both":
        try:
            a_ranked = await rm.find_releases(db, cw, variant="audiobook")
            a_cands = rm.candidate_dicts(a_ranked, cap=downloads.CANDIDATE_CAP, include_speculative=True)
            if a_cands:
                await downloads.grab_release(db, cw, candidates=a_cands, user_id=None,
                                             kind=STOCK_KIND, variant="audiobook")
            else:
                ledger.mark_unavailable(db, cw, reason="no_match", provider="pipeline", variant="audiobook")
        except Exception:  # noqa: BLE001 — the audiobook is a bonus; never fail the ebook stock on it
            db.rollback()
            log.info("stock 'both': audiobook grab failed for %r", cw.title, exc_info=True)


async def stock_tick() -> dict:
    """Background worker: advance up to ``STOCK_PER_TICK`` pending stock items + reconcile in-flight
    ones. No-op unless the pipeline + stock directory are configured. Deliberately does NOT run the
    open-library (libgen) recovery — that downloads + verifies synchronously and can take minutes on
    slow/blocked mirrors, which would block this tick and stall reconcile/progress updates. The
    recovery runs on its own schedule in :func:`stock_libgen_tick`."""
    from ..db import SessionLocal
    db = SessionLocal()
    try:
        if not stock_configured(db):                          # prowlarr+sabnzbd + stock dir
            return {"skipped": "not configured"}
        reconcile_stock(db)
        # Backpressure: throttle new grabs by how many stock downloads are already in flight, so we
        # never dump thousands of NZBs into a shared SABnzbd at once (it's also used by other apps).
        inflight = db.scalar(
            select(func.count(StockItem.id)).where(StockItem.status.in_(_IN_FLIGHT))
        ) or 0
        slots = max(0, STOCK_MAX_INFLIGHT - int(inflight))
        if slots <= 0:
            return {"processed": 0, "inflight": int(inflight), "throttled": True}
        # Daily search cap: bound this batch by today's remaining search budget (0 = unlimited).
        # The per-item download cap is enforced inside _process_pending (a found release isn't grabbed
        # once the download budget is spent — the item stays pending for tomorrow).
        search_left = _remaining(_cap("stock_searches_per_day"), _usage(db)["searches"])
        if search_left is not None:
            slots = min(slots, search_left)
        if slots <= 0:
            return {"processed": 0, "inflight": int(inflight), "capped": True}
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
        return {"processed": len(pending)}
    finally:
        db.close()


async def stock_libgen_tick() -> dict:
    """Open-library (libgen) recovery on its OWN schedule, decoupled from :func:`stock_tick` because
    it downloads + content-verifies synchronously (minutes on slow/Cloudflare-blocked mirrors). Kept
    on a wall-clock budget so a single run can't sprawl and so successive runs aren't all skipped for
    'maximum running instances'. No-op unless the open-library fallback + stock dir are configured."""
    from ..db import SessionLocal
    from . import libgen
    db = SessionLocal()
    try:
        if not (get_stock_dir(db) and libgen.configured(db)):
            return {"skipped": "not configured"}
        reconcile_stock(db)
        return {"libgen_retry": await retry_failed_via_libgen(db, limit=STOCK_LIBGEN_PER_TICK)}
    finally:
        db.close()


def remove_stock(db: Session, stock_id: int, *, delete_file: bool = True) -> bool:
    """Remove a stock item: optionally delete its file from the stock directory, and drop the row.
    The shared Work is left in place (users who already acquired it keep it — imported ebooks read
    from their chapterized DB content), but when the FILE is deleted the Work's file fields are
    cleared (so edition-dedup/kindle/export don't chase a ghost file) and its catalog hooks are
    dropped (so the title reverts to "Acquire" instead of advertising dead stock)."""
    from ..library import unhook_work
    si = db.get(StockItem, stock_id)
    if si is None:
        return False
    if delete_file:
        _delete_stock_file(si, get_stock_dir(db))
        work = db.get(Work, si.work_id) if si.work_id else None
        if work is not None:
            work.local_path = None
            work.local_mtime = None
            work.local_size = None
            work.content_hash = None
            unhook_work(db, work.id)
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


def refetch_stock_item(db: Session, si: StockItem, sdir: str | None) -> None:
    """Remediate ONE bad stocked file: delete it, blacklist the release that produced it, un-hook
    the catalog, and reset the item to ``pending`` so the worker re-fetches a fresh copy. KEEPS the
    Work and every user's library entry/progress — the re-import migrates them onto the new copy
    (see _mark_stocked → _migrate_work_links). Commits."""
    from . import broken
    _delete_stock_file(si, sdir)
    # Mark the release that produced this bad file broken, so the re-fetch picks a different one.
    if si.download_job_id:
        job = db.get(DownloadJob, si.download_job_id)
        if job and job.release_key:
            broken.mark_broken(db, {"key": job.release_key, "title": si.title},
                               reason="corrupt/unimportable stocked file")
    # Un-hook the catalog so NEW acquisitions go through the re-fetch. By hooked_work_id, not
    # si.catalog_work_id: legacy items without a catalog ref would otherwise leave the GROUP
    # advertising the corrupt copy during the re-fetch.
    if si.work_id:
        for cwrow in db.scalars(select(CatalogWork).where(
                CatalogWork.hooked_work_id == si.work_id)).all():
            cwrow.hooked_work_id = None
        for grp in db.scalars(select(CatalogGroup).where(
                CatalogGroup.hooked_work_id == si.work_id)).all():
            grp.hooked_work_id = None
        # Clear the Work's FILE fields too (the file is gone) — leaving local_path set made
        # stock_link re-index the file-less Work as in-stock and RE-HOOK the group within 6h,
        # reversing this remediation (and _process_pending's hooked short-circuit then marked
        # the item 'stocked' again without downloading anything). Same shape as remove_stock.
        work = db.get(Work, si.work_id)
        if work is not None:
            work.local_path = None
            work.local_mtime = None
            work.local_size = None
            work.content_hash = None
    si.status, si.file_path, si.download_job_id, si.error = "pending", None, None, None
    si.stocked_at = None      # si.work_id kept → users migrate to the re-fetched copy
    db.commit()
    from .. import cache
    cache.clear_catalog()   # un-hooked + de-stocked → refresh the baked in_stock state (throttled)


def sweep_integrity(db: Session, *, limit: int = 500) -> dict:
    """Re-check every STOCKED file's structural integrity and re-fetch the bad ones. A file that's
    missing, corrupt, or no longer importable is removed; its (broken) release is recorded so the
    re-fetch won't grab the same one; the shared Work is dropped and its catalog rows un-hooked; and
    the stock item is reset to ``pending`` so the worker fetches a fresh copy (usenet first, then the
    open-library fallback). Returns {checked, corrupt, refetch_queued}."""
    import os

    from . import convert, verify

    sdir = get_stock_dir(db)
    items = db.scalars(
        select(StockItem).where(StockItem.status == "stocked").order_by(StockItem.id).limit(limit)
    ).all()
    checked = corrupt = 0
    for si in items:
        # Guard the Work fetch: a stocked item can outlive its Work (deleted/purged), so db.get may
        # return None — reading .local_path off that would AttributeError and abort the whole sweep.
        work = db.get(Work, si.work_id) if si.work_id else None
        path = si.file_path or (work.local_path if work else None)
        checked += 1
        # Audio-aware check: a FOLDER audiobook is a directory — the old isfile()+check_integrity
        # shape read every one as "missing" and would have re-fetched the whole audio stock.
        mk = (work.media_kind if work else None) or \
            ("audio" if (path or "").lower().endswith(verify._AUDIO_EXTS) else "text")
        ok = bool(path) and verify.check_media_file(path, mk)[0]
        # A stocked Kindle file we can now convert isn't "corrupt" — leave it (separate concern).
        if ok or (path and convert.can_convert(path)):
            continue
        corrupt += 1
        refetch_stock_item(db, si, sdir)
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


def stock_feeding_lists(db: Session) -> list[dict]:
    """The active list subscriptions currently routed to operator STOCK (``to_stock=True``) — surfaced
    so the UI can show which monitored external reading lists feed the stock pool. ``sync_list``
    already queues their new titles to stock; this is read-only reporting, no new pipeline."""
    from ..models import ListSubscription
    rows = db.scalars(
        select(ListSubscription).where(
            ListSubscription.to_stock.is_(True), ListSubscription.active.is_(True)
        ).order_by(ListSubscription.id)
    ).all()
    return [{
        "id": s.id, "provider": s.provider, "list_name": s.list_name,
        "display_name": s.display_name, "variant": s.variant, "to_stock": True,
        "auto_added": s.auto_added, "last_checked_at": s.last_checked_at,
    } for s in rows]


def summary(db: Session) -> dict:
    """Counts by status across ALL stock items (config-card dashboard), plus today's daily-cap usage
    and the list subscriptions feeding stock."""
    rows = db.execute(
        select(StockItem.status, func.count(StockItem.id)).group_by(StockItem.status)
    ).all()
    counts = {s: int(c) for s, c in rows}
    return {"counts": counts, "total": sum(counts.values()),
            "daily_caps": daily_caps(db), "feeding_lists": stock_feeding_lists(db)}


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
                     "sort": None, "variant": "ebook", "requested": base["total"], "created_at": None})
    else:
        base.update({"id": job.id, "name": job.name, "media_category": job.media_category,
                     "dimension": job.dimension, "value": job.value, "sort": job.sort,
                     "variant": getattr(job, "variant", None) or "ebook",
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


def job_detail(db: Session, job_id: int | None, *, item_cap: int = 200,
               problem_cap: int = 100) -> dict | None:
    """A single batch with its stats + a CAPPED sample of items. ``job_id`` None/0 → the legacy
    ungrouped bucket. Totals/progress come from grouped counts (accurate), but the item rows are
    capped — the ungrouped bucket alone can hold thousands, and shipping them all on every 4s poll
    bloated the payload and the UI listing. Issue items are surfaced first so triage stays useful."""
    legacy = job_id in (None, 0)
    job = None if legacy else db.get(StockJob, job_id)
    if not legacy and job is None:
        return None
    cond = StockItem.stock_job_id.is_(None) if legacy else StockItem.stock_job_id == job_id
    # Accurate stats from grouped counts over ALL items (not just the capped sample).
    count_rows = db.execute(
        select(StockItem.status, func.count(StockItem.id)).where(cond).group_by(StockItem.status)
    ).all()
    counts = {s: int(c) for s, c in count_rows}
    if legacy and not counts:
        return None
    size = int(db.scalar(
        select(func.coalesce(func.sum(StockItem.size), 0))
        .where(cond, StockItem.status == "stocked")
    ) or 0)
    # Issue items first (the ones needing attention), then in-flight/pending, then stocked — each
    # most-popular first — and only the first ``item_cap`` for display.
    issues = list(db.scalars(
        select(StockItem).where(cond, StockItem.status.in_(_ISSUE))
        .order_by(StockItem.popularity_norm.desc(), StockItem.id.desc()).limit(problem_cap)
    ).all())
    items = list(db.scalars(
        select(StockItem).where(cond)
        .order_by(StockItem.status, StockItem.popularity_norm.desc(), StockItem.id.desc())
        .limit(item_cap)
    ).all())
    info = _job_dict(job, counts, size)
    info["items"] = items
    info["items_shown"] = len(items)
    info["problem_items"] = issues
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
