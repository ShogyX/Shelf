"""Index-SITE management orchestration — the domain logic + cache invalidation behind the
admin mutation endpoints (add / edit / pause / resume / delete a crawled site, set the global
index config). The HTTP handlers in :mod:`app.routers.index` are thin wrappers over these.

Per-site stats assembly (``_build_site_out`` / ``_status_reason``) lives here too, since it is
index-site domain logic; the router's read endpoints (list/stats) import it back for serialization.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import cache
from .. import config_store
from ..db import index_fts_delete
from ..models import CatalogWork, IndexedPage, IndexSite
from ..schemas import IndexConfigOut, IndexSiteIn, IndexSiteOut
from .engine import ComplianceError
from .indexer import start_index


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC for arithmetic."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _status_reason(
    site: IndexSite, status_counts: dict[str, int], cooldown: datetime | None, now: datetime
) -> str:
    """A one-line, human explanation of the crawl's current state — why it stopped, paused,
    is cooling down, or is still going — so the operator isn't left guessing."""
    pending = status_counts.get("pending", 0)
    failed = status_counts.get("failed", 0)
    fetched = status_counts.get("fetched", 0)
    if cooldown and cooldown > now:
        mins = max(1, round((cooldown - now).total_seconds() / 60))
        why = site.last_error or "site pushed back"
        return f"Cooling down ~{mins} min after pushback — {why}"
    if site.status == "failed":
        return site.last_error or "Crawl failed."
    if site.status == "paused":
        return site.last_error or "Paused by operator."
    if site.status == "done":
        if fetched == 0 and failed > 0:
            return f"Stopped — every request failed ({failed}). See page errors below."
        idle = site.stop_after_idle_pages or 0
        if idle and (site.pages_since_new_title or 0) >= idle:
            return (f"Finished — no new titles for {site.pages_since_new_title} pages "
                    f"(idle-stop at {idle}).")
        return "Finished — crawl frontier exhausted."
    if site.status == "active":
        if pending:
            return f"Crawling — {pending} pages queued."
        return "Crawling…"
    return site.last_error or site.status


def build_site_out(
    site: IndexSite,
    status_counts: dict[str, int],
    words: int,
    titles: int,
    last_activity: datetime | None,
    now: datetime,
) -> IndexSiteOut:
    total = sum(status_counts.values())
    fetched, failed = status_counts.get("fetched", 0), status_counts.get("failed", 0)
    # Wall time from when the site was added to its last fetch — or to "now" while it's
    # still actively crawling (so the timer is live in the UI).
    created = _aware(site.created_at)
    end = now if site.status == "active" else (_aware(last_activity) or created)
    duration = max(0.0, (end - created).total_seconds()) if created else 0.0
    cooldown = _aware(site.cooldown_until)
    return IndexSiteOut(
        id=site.id, root_url=site.root_url, domain=site.domain, title=site.title,
        status=site.status, max_pages=site.max_pages, max_depth=site.max_depth,
        same_host_only=site.same_host_only,
        allowed_media_kinds=site.allowed_media_kinds,
        stop_after_idle_pages=site.stop_after_idle_pages or 0,
        pages_since_new_title=site.pages_since_new_title or 0,
        last_error=site.last_error,
        cooldown_until=cooldown,
        consecutive_errors=site.consecutive_errors or 0,
        status_reason=_status_reason(site, status_counts, cooldown, now),
        pages_total=total, pages_fetched=fetched,
        pages_pending=status_counts.get("pending", 0), pages_failed=failed,
        titles_found=int(titles), requests=fetched + failed,
        duration_seconds=duration,
        last_activity_at=_aware(last_activity),  # serialize with a UTC offset
        words=int(words), created_at=_aware(site.created_at),
    )


def _site_out(db: Session, site: IndexSite) -> IndexSiteOut:
    """Single-site stats (used by add/pause/resume). list_sites uses a batched path."""
    rows = dict(
        db.execute(
            select(IndexedPage.status, func.count(IndexedPage.id))
            .where(IndexedPage.site_id == site.id)
            .group_by(IndexedPage.status)
        ).all()
    )
    words = db.scalar(
        select(func.sum(IndexedPage.word_count)).where(IndexedPage.site_id == site.id)
    ) or 0
    titles = db.scalar(
        select(func.count(CatalogWork.id)).where(CatalogWork.site_id == site.id)
    ) or 0
    last_activity = db.scalar(
        select(func.max(IndexedPage.fetched_at)).where(IndexedPage.site_id == site.id)
    )
    return build_site_out(site, rows, words, titles, last_activity, _utcnow())


# --------------------------------------------------------------------- mutations
def set_index_config(db: Session, stop_after_idle_pages: int) -> IndexConfigOut:
    """Set the global idle-page stop threshold applied to NEW crawls (+ invalidate index caches)."""
    from . import indexer
    n = indexer.set_global_idle_default(db, stop_after_idle_pages)
    cache.clear_index()  # config change affects index-sites/stats
    return IndexConfigOut(
        stop_after_idle_pages=n, max_pages=config_store.effective("index_max_pages")
    )


def update_site(db: Session, site_id: int, data: dict) -> IndexSiteOut:
    """Edit a single crawl's bounds. ``data`` is the already-``model_dump(exclude_unset=True)``
    payload. Raises HTTPException(404) when the site is gone."""
    site = db.get(IndexSite, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    if "stop_after_idle_pages" in data and data["stop_after_idle_pages"] is not None:
        site.stop_after_idle_pages = data["stop_after_idle_pages"]
    if "max_pages" in data and data["max_pages"] is not None:
        site.max_pages = data["max_pages"]
    if "max_depth" in data and data["max_depth"] is not None:
        site.max_depth = data["max_depth"]
    if "allowed_media_kinds" in data:  # present (even null/[]) → set or CLEAR the restriction
        kinds = [k for k in (data["allowed_media_kinds"] or []) if k in ("text", "comic")]
        site.allowed_media_kinds = kinds or None  # [] / null → no restriction (serves all kinds)
    db.commit()
    cache.clear_index()
    return _site_out(db, site)


def add_site(db: Session, payload: IndexSiteIn) -> IndexSiteOut:
    """Start indexing a new site. Translates ComplianceError → HTTP 403."""
    try:
        site = start_index(
            db, payload.url,
            max_pages=payload.max_pages, max_depth=payload.max_depth,
            same_host_only=payload.same_host_only,
            update_indexed=payload.update_indexed,
        )
    except ComplianceError as exc:
        raise HTTPException(403, str(exc)) from exc
    cache.clear_index()
    return _site_out(db, site)


def pause_site(db: Session, site_id: int) -> IndexSiteOut:
    site = db.get(IndexSite, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    site.status = "paused"
    db.commit()
    cache.clear_index_sites()
    return _site_out(db, site)


def resume_site(db: Session, site_id: int) -> IndexSiteOut:
    site = db.get(IndexSite, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    site.status = "active"
    # Resuming is an explicit "try again": clear any backoff and re-queue pages that previously
    # gave up so they get another shot under the resilient retry path (a blip/temporary block no
    # longer strands them as permanently failed). Robots-skipped pages stay skipped.
    site.consecutive_errors = 0
    site.cooldown_until = None
    db.execute(
        update(IndexedPage)
        .where(IndexedPage.site_id == site_id, IndexedPage.status == "failed")
        .values(status="pending", attempts=0, next_attempt_at=None, last_error=None)
    )
    db.commit()
    cache.clear_index_sites()
    return _site_out(db, site)


def delete_site(db: Session, site_id: int, *, purge: bool) -> dict:
    """Remove an indexed source. Soft by default (status 'removed', content kept); ``purge=True``
    permanently drops the site, its indexed pages (+ FTS rows) and catalog entries."""
    site = db.get(IndexSite, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    if not purge:
        # Soft remove: stop the crawl, preserve all indexed material. The site stays in the list
        # (status "removed") so it can be restored or permanently deleted later.
        site.status = "removed"
        db.commit()
        cache.clear_index()  # site status changed; kept content still serves search/catalog
        return {"removed": site_id, "purged": False}
    # Permanent purge: drop the site, its indexed pages (+ their FTS rows) and catalog entries.
    page_ids = [
        pid for (pid,) in db.execute(
            select(IndexedPage.id).where(IndexedPage.site_id == site_id)
        ).all()
    ]
    conn = db.connection()
    for pid in page_ids:
        index_fts_delete(conn, pid)
    # Remove this site's catalog entries (no cascade on the plain relationship).
    for cw in db.scalars(select(CatalogWork).where(CatalogWork.site_id == site_id)).all():
        db.delete(cw)
    db.delete(site)
    db.commit()
    cache.clear()  # deletion removes catalog entries + site rows — drop all cached slices
    return {"deleted": site_id, "purged": True}
