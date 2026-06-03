"""URL Index API — submit sites to auto-crawl, browse pages, full-text search, hook to library."""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from .. import cache
from .. import db as dbmod
from ..auth import current_user, require_admin
from ..db import get_db, index_fts_delete
from ..library import add_to_library
from ..ingestion import blocklist, catalog
from ..ingestion.base import RawChapter, registry
from ..ingestion.engine import ComplianceError, ensure_source, store_chapter_content
from ..ingestion.indexer import start_index
from ..models import CatalogWork, Chapter, IndexBlock, IndexedPage, IndexSite, User, Work
from ..config import get_settings
from ..schemas import (
    CatalogGroupOut,
    CrawlTuningIn,
    CrawlTuningOut,
    GrabOut,
    IndexBlockIn,
    IndexBlockOut,
    IndexConfigIn,
    IndexConfigOut,
    IndexedPageDetailOut,
    IndexedPageOut,
    IndexSearchOut,
    IndexSiteIn,
    IndexSiteOut,
    IndexSiteUpdate,
    IndexStatsOut,
    WorkOut,
)

router = APIRouter()


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


def _build_site_out(
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
    return _build_site_out(site, rows, words, titles, last_activity, _utcnow())


# ---------------------------------------------------------------------- sites
@router.get("/index/sites", response_model=list[IndexSiteOut])
def list_sites(db: Session = Depends(get_db)) -> list[IndexSiteOut]:
    cached = cache.get("index-sites")
    if cached is not None:
        return cached
    sites = db.scalars(select(IndexSite).order_by(IndexSite.created_at.desc())).all()
    if not sites:
        return []
    ids = [s.id for s in sites]
    # Batch every per-site aggregate into one grouped query each (instead of 4–5 per site).
    status_by_site: dict[int, dict[str, int]] = {}
    for sid, status, cnt in db.execute(
        select(IndexedPage.site_id, IndexedPage.status, func.count(IndexedPage.id))
        .where(IndexedPage.site_id.in_(ids))
        .group_by(IndexedPage.site_id, IndexedPage.status)
    ).all():
        status_by_site.setdefault(sid, {})[status] = cnt
    words_by_site = dict(
        db.execute(
            select(IndexedPage.site_id, func.sum(IndexedPage.word_count))
            .where(IndexedPage.site_id.in_(ids)).group_by(IndexedPage.site_id)
        ).all()
    )
    last_by_site = dict(
        db.execute(
            select(IndexedPage.site_id, func.max(IndexedPage.fetched_at))
            .where(IndexedPage.site_id.in_(ids)).group_by(IndexedPage.site_id)
        ).all()
    )
    titles_by_site = dict(
        db.execute(
            select(CatalogWork.site_id, func.count(CatalogWork.id))
            .where(CatalogWork.site_id.in_(ids)).group_by(CatalogWork.site_id)
        ).all()
    )
    now = _utcnow()
    out = [
        _build_site_out(
            s, status_by_site.get(s.id, {}), words_by_site.get(s.id) or 0,
            titles_by_site.get(s.id) or 0, last_by_site.get(s.id), now,
        )
        for s in sites
    ]
    # Short TTL: these per-site aggregates are heavy over a large DB and the UI polls them
    # every ~2.5s. Cache just under the poll cadence so each poll burst is one computation.
    cache.put("index-sites", out, ttl=2.0)
    return out


@router.get("/index/stats", response_model=IndexStatsOut)
def index_stats(db: Session = Depends(get_db)) -> IndexStatsOut:
    """Aggregate crawl observability: sites by status, pages, titles, requests, time."""
    cached = cache.get("index-stats")
    if cached is not None:
        return cached
    site_status = dict(
        db.execute(
            select(IndexSite.status, func.count(IndexSite.id)).group_by(IndexSite.status)
        ).all()
    )
    page_status = dict(
        db.execute(
            select(IndexedPage.status, func.count(IndexedPage.id)).group_by(IndexedPage.status)
        ).all()
    )
    titles = db.scalar(
        select(func.count(CatalogWork.id)).where(CatalogWork.site_id.is_not(None))
    ) or 0
    words = db.scalar(select(func.sum(IndexedPage.word_count))) or 0
    fetched = page_status.get("fetched", 0)
    failed = page_status.get("failed", 0)
    # Total crawl time = sum of each site's elapsed span (created → last fetch, or now if
    # still active), so concurrent crawls each contribute their own duration. One grouped
    # query for last-activity (avoids an N+1 over sites).
    last_by_site = dict(
        db.execute(
            select(IndexedPage.site_id, func.max(IndexedPage.fetched_at))
            .group_by(IndexedPage.site_id)
        ).all()
    )
    now = _utcnow()
    spent = 0.0
    for site in db.scalars(select(IndexSite)).all():
        created = _aware(site.created_at)
        if created is None:
            continue
        end = now if site.status == "active" else (_aware(last_by_site.get(site.id)) or created)
        spent += max(0.0, (end - created).total_seconds())
    out = IndexStatsOut(
        sites_total=sum(site_status.values()),
        sites_active=site_status.get("active", 0),
        sites_paused=site_status.get("paused", 0),
        sites_done=site_status.get("done", 0),
        sites_failed=site_status.get("failed", 0),
        pages_total=sum(page_status.values()),
        pages_fetched=fetched,
        pages_pending=page_status.get("pending", 0),
        pages_failed=failed,
        titles_found=int(titles),
        requests_made=fetched + failed,
        words_indexed=int(words),
        time_spent_seconds=spent,
    )
    cache.put("index-stats", out)
    return out


@router.get("/index/config", response_model=IndexConfigOut)
def get_index_config(db: Session = Depends(get_db)) -> IndexConfigOut:
    """Global indexing defaults: the idle-page stop threshold and the (unlimited) page cap."""
    from ..ingestion import indexer
    return IndexConfigOut(
        stop_after_idle_pages=indexer.global_idle_default(db),
        max_pages=get_settings().index_max_pages,
    )


@router.put("/index/config", response_model=IndexConfigOut,
             dependencies=[Depends(require_admin)])
def put_index_config(payload: IndexConfigIn, db: Session = Depends(get_db)) -> IndexConfigOut:
    """Set the global idle-page stop threshold applied to NEW crawls."""
    from ..ingestion import indexer
    n = indexer.set_global_idle_default(db, payload.stop_after_idle_pages)
    cache.clear("index")  # config change affects index-sites/stats
    return IndexConfigOut(stop_after_idle_pages=n, max_pages=get_settings().index_max_pages)


@router.get("/index/crawl-tuning", response_model=CrawlTuningOut)
def get_crawl_tuning(db: Session = Depends(get_db)) -> CrawlTuningOut:
    """Current live crawl speed (applies to running + future jobs)."""
    from ..ingestion import crawl_tuning
    return CrawlTuningOut(**crawl_tuning.get_tuning(db))


@router.put("/index/crawl-tuning", response_model=CrawlTuningOut,
            dependencies=[Depends(require_admin)])
def put_crawl_tuning(payload: CrawlTuningIn, db: Session = Depends(get_db)) -> CrawlTuningOut:
    """Set crawl speed. Takes effect immediately: resizes the fetch concurrency and reschedules
    the crawl/index ticks, so running jobs speed up without a restart."""
    from ..ingestion import crawl_tuning
    updated = crawl_tuning.set_tuning(db, payload.model_dump(exclude_none=True))
    return CrawlTuningOut(**updated)


@router.patch("/index/sites/{site_id}", response_model=IndexSiteOut,
               dependencies=[Depends(require_admin)])
def update_site(
    site_id: int, payload: IndexSiteUpdate, db: Session = Depends(get_db)
) -> IndexSiteOut:
    """Edit a single crawl's bounds — its idle-page timeout, page cap (0 = unlimited), depth."""
    site = db.get(IndexSite, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    data = payload.model_dump(exclude_unset=True)
    if "stop_after_idle_pages" in data and data["stop_after_idle_pages"] is not None:
        site.stop_after_idle_pages = data["stop_after_idle_pages"]
    if "max_pages" in data and data["max_pages"] is not None:
        site.max_pages = data["max_pages"]
    if "max_depth" in data and data["max_depth"] is not None:
        site.max_depth = data["max_depth"]
    db.commit()
    cache.clear("index")
    return _site_out(db, site)


@router.post("/index/sites", response_model=IndexSiteOut,
             dependencies=[Depends(require_admin)])
def add_site(payload: IndexSiteIn, db: Session = Depends(get_db)) -> IndexSiteOut:
    try:
        site = start_index(
            db, payload.url,
            max_pages=payload.max_pages, max_depth=payload.max_depth,
            same_host_only=payload.same_host_only,
            update_indexed=payload.update_indexed,
        )
    except ComplianceError as exc:
        raise HTTPException(403, str(exc)) from exc
    cache.clear("index")
    return _site_out(db, site)


@router.post("/index/sites/{site_id}/pause", response_model=IndexSiteOut,
             dependencies=[Depends(require_admin)])
def pause_site(site_id: int, db: Session = Depends(get_db)) -> IndexSiteOut:
    site = db.get(IndexSite, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    site.status = "paused"
    db.commit()
    cache.clear("index-sites")
    return _site_out(db, site)


@router.post("/index/sites/{site_id}/resume", response_model=IndexSiteOut,
             dependencies=[Depends(require_admin)])
def resume_site(site_id: int, db: Session = Depends(get_db)) -> IndexSiteOut:
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
    cache.clear("index-sites")
    return _site_out(db, site)


@router.delete("/index/sites/{site_id}", dependencies=[Depends(require_admin)])
def delete_site(
    site_id: int,
    purge: bool = Query(
        False, description="also permanently delete the indexed pages + catalog entries"
    ),
    db: Session = Depends(get_db),
) -> dict:
    """Remove an indexed source. By default this is a soft removal: crawling stops but every
    indexed page, catalog entry and full-text search record is KEPT, so re-adding the same URL
    later resumes without re-crawling. Permanent deletion of the indexed material is a separate,
    explicit action (``purge=true``) per the user's request."""
    site = db.get(IndexSite, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    if not purge:
        # Soft remove: stop the crawl, preserve all indexed material. The site stays in the list
        # (status "removed") so it can be restored or permanently deleted later.
        site.status = "removed"
        db.commit()
        cache.clear("index")  # site status changed; kept content still serves search/catalog
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


# ---------------------------------------------------------------------- pages
@router.get("/index/pages", response_model=list[IndexedPageOut])
def list_pages(
    site_id: int | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[IndexedPageOut]:
    # Select only the list columns + a SHORT prefix of text — never the big html/text blobs
    # (loading those for every row made this list endpoint take seconds on a large DB).
    cols = (
        IndexedPage.id, IndexedPage.site_id, IndexedPage.url, IndexedPage.title,
        IndexedPage.description, IndexedPage.author, IndexedPage.cover_url,
        IndexedPage.site_name, IndexedPage.page_type, IndexedPage.word_count,
        IndexedPage.depth, IndexedPage.status, IndexedPage.hooked_work_id,
        IndexedPage.fetched_at, IndexedPage.last_error, IndexedPage.attempts,
        IndexedPage.next_attempt_at, func.substr(IndexedPage.text, 1, 240).label("snip"),
    )
    q = select(*cols)
    if site_id is not None:
        q = q.where(IndexedPage.site_id == site_id)
    if status:
        q = q.where(IndexedPage.status == status)
    q = q.order_by(IndexedPage.depth, IndexedPage.id).limit(limit).offset(offset)
    out: list[IndexedPageOut] = []
    for r in db.execute(q).all():
        snippet = (r.snip or "").strip()
        out.append(
            IndexedPageOut(
                id=r.id, site_id=r.site_id, url=r.url, title=r.title, description=r.description,
                author=r.author, cover_url=r.cover_url, site_name=r.site_name,
                page_type=r.page_type, word_count=r.word_count, depth=r.depth, status=r.status,
                hooked_work_id=r.hooked_work_id, fetched_at=r.fetched_at,
                snippet=snippet or None,
                last_error=r.last_error, attempts=r.attempts or 0,
                next_attempt_at=r.next_attempt_at,
            )
        )
    return out


@router.get("/index/pages/{page_id}", response_model=IndexedPageDetailOut)
def get_page(page_id: int, db: Session = Depends(get_db)) -> IndexedPageDetailOut:
    p = db.get(IndexedPage, page_id)
    if p is None:
        raise HTTPException(404, "Page not found")
    return IndexedPageDetailOut(
        id=p.id, site_id=p.site_id, url=p.url, title=p.title, description=p.description,
        author=p.author, cover_url=p.cover_url, site_name=p.site_name, page_type=p.page_type,
        word_count=p.word_count, depth=p.depth, status=p.status,
        hooked_work_id=p.hooked_work_id, fetched_at=p.fetched_at,
        html=p.html, domain=urlparse(p.url).netloc,
    )


# --------------------------------------------------------------------- search
def _fts_query(raw: str) -> str:
    """Turn free user input into a safe FTS5 prefix-AND query."""
    tokens = [t for t in raw.replace('"', " ").split() if t]
    return " ".join(f'"{t}"*' for t in tokens)


def _snippet(text_body: str, q: str, *, width: int = 220) -> str:
    """Build a short, HTML-escaped snippet around the first matched query token, with the
    match wrapped in <mark>. Escaped because it's rendered via dangerouslySetInnerHTML."""
    import html as _html

    txt = text_body or ""
    low = txt.lower()
    idx, tok = -1, ""
    for t in q.lower().split():
        i = low.find(t)
        if i >= 0:
            idx, tok = i, t
            break
    start = max(0, idx - 60) if idx >= 0 else 0
    frag = txt[start:start + width].strip()
    out = _html.escape(frag)
    if tok:
        out = re.sub(re.escape(_html.escape(tok)), lambda m: f"<mark>{m.group(0)}</mark>",
                     out, count=1, flags=re.I)
    return ("…" if start > 0 else "") + out


@router.get("/index/search", response_model=list[IndexSearchOut])
def search(
    q: str = Query(..., min_length=1),
    site_id: int | None = None,
    limit: int = Query(30, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[IndexSearchOut]:
    # Select only display columns + a 4 KB prefix of text for the snippet — never the big
    # html/text blobs (loading those per row made search take seconds).
    cols = (
        IndexedPage.id, IndexedPage.site_id, IndexedPage.url, IndexedPage.title,
        IndexedPage.description, IndexedPage.author, IndexedPage.cover_url,
        func.substr(IndexedPage.text, 1, 4000).label("snip_src"),
    )
    scores: dict[int, float] = {}
    if dbmod.fts_enabled:
        match = _fts_query(q)
        if not match:
            return []
        # Rank + limit using ONLY the FTS index first (no join to the big indexed_pages rows,
        # no snippet over every match — both made this O(all matches) and slow). Then load
        # just the top-N rows. Over-fetch when site-filtering so the filter still has matches.
        fetch = limit if site_id is None else limit * 4
        try:
            ranked = db.execute(
                text(
                    "SELECT rowid, bm25(indexed_pages_fts) AS score FROM indexed_pages_fts "
                    "WHERE indexed_pages_fts MATCH :m ORDER BY score LIMIT :lim"
                ),
                {"m": match, "lim": fetch},
            ).all()
        except Exception:
            ranked = []
        if not ranked:
            return []
        order = {rid: i for i, (rid, _s) in enumerate(ranked)}
        scores = {rid: float(s) for rid, s in ranked}
        sel = select(*cols).where(IndexedPage.id.in_(list(order)))
        if site_id is not None:
            sel = sel.where(IndexedPage.site_id == site_id)
        rows = sorted(db.execute(sel).all(), key=lambda r: order.get(r.id, 1 << 30))[:limit]
    else:
        # Fallback: LIKE search (no FTS5 in this SQLite build).
        like = f"%{q}%"
        cond = (IndexedPage.title.like(like)) | (IndexedPage.text.like(like))
        sel = select(*cols).where(IndexedPage.status == "fetched", cond)
        if site_id is not None:
            sel = sel.where(IndexedPage.site_id == site_id)
        rows = db.execute(sel.limit(limit)).all()

    return [
        IndexSearchOut(
            page_id=r.id, site_id=r.site_id, url=r.url, title=r.title,
            snippet=_snippet(r.snip_src or "", q), score=scores.get(r.id, 0.0),
            description=r.description, author=r.author, cover_url=r.cover_url,
        )
        for r in rows
    ]


# -------------------------------------------------------------------- catalog
@router.get("/catalog", response_model=list[CatalogGroupOut])
async def list_catalog(
    q: str | None = Query(None, description="Search title / author / synopsis"),
    site_id: int | None = None,
    hooked: bool | None = None,
    media: str | None = Query(None, description="Filter by media label (Novel/Book/Manga/…)"),
    domain: str | None = Query(None, description="Filter to one source domain"),
    sort: str = Query("relevance", description="relevance | chapters | title"),
    live: bool = Query(False, description="Also live-search connected integrations"),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[CatalogGroupOut]:
    """The searchable catalog of discovered works, grouped + deduped across sources (web
    crawl + Readarr + Kapowarr) so the same title is one card with selectable sources.
    Media-type / source filters and sort are applied server-side over the full grouped set.
    With ``live=true`` and a query, also looks the term up live in connected integrations."""
    if live and q and q.strip():
        from ..integrations import sync as isync
        try:
            await isync.search_integrations(db, q.strip())
        except Exception:  # noqa: BLE001 — live lookup is best-effort
            pass
        cache.clear("catalog")  # integration results may have changed the catalog

    # Cache the FULL grouped+filtered+sorted result for a query/filter set (NOT per page), then
    # slice locally — so paging (infinite scroll) and repeat visits reuse one computation instead
    # of re-fetching + re-grouping 2000 rows for every offset.
    gkey = f"catalog-groups:{q}:{site_id}:{hooked}:{media}:{domain}:{sort}"
    groups: list[CatalogGroupOut] | None = None if live else cache.get(gkey)
    if groups is None:
        def _compute() -> list[CatalogGroupOut]:
            rows = catalog.find_rows(db, q=q, site_id=site_id, hooked=hooked, limit=2000)
            g = catalog.group_rows(rows, q=q)
            g = catalog.filter_and_sort_groups(g, media=media, domain=domain, sort=sort)
            return [CatalogGroupOut(**x) for x in g]

        # Grouping is CPU-bound + synchronous — run it off the event loop so it never blocks
        # concurrent API requests.
        groups = await asyncio.to_thread(_compute)
        if not live:
            cache.put(gkey, groups, ttl=15.0)
    return groups[offset:offset + limit]


@router.get("/catalog/facets")
def catalog_facets(db: Session = Depends(get_db)) -> dict:
    """Complete filter options (all media types + source domains) for the Index page."""
    cached = cache.get("catalog-facets")
    if cached is not None:
        return cached
    out = catalog.catalog_facets(db)
    cache.put("catalog-facets", out, ttl=15.0)
    return out


@router.get("/catalog/stats")
def catalog_stats(db: Session = Depends(get_db)) -> dict:
    cached = cache.get("catalog-stats")
    if cached is not None:
        return cached
    total = db.scalar(select(func.count(CatalogWork.id))) or 0
    hooked = db.scalar(
        select(func.count(CatalogWork.id)).where(CatalogWork.hooked_work_id.is_not(None))
    ) or 0
    sites = db.scalar(select(func.count(func.distinct(CatalogWork.site_id)))) or 0
    groups = db.scalar(select(func.count(func.distinct(CatalogWork.norm_key)))) or 0
    out = {"entries": total, "titles": groups, "hooked": hooked, "sites": sites}
    cache.put("catalog-stats", out)
    return out


@router.post("/catalog/{catalog_id}/grab", response_model=GrabOut)
async def grab_catalog(catalog_id: int, db: Session = Depends(get_db)) -> GrabOut:
    """Grab a discovered work via its integration (Readarr/Kapowarr): add it there +
    trigger a download. The file is imported once it lands in a watched folder."""
    entry = db.get(CatalogWork, catalog_id)
    if entry is None:
        raise HTTPException(404, "Catalog entry not found")
    if entry.provider == "web_index":
        raise HTTPException(400, "Online sources are added with Hook, not grabbed.")
    from ..integrations import IntegrationError
    from ..integrations import sync as isync
    try:
        info = await isync.grab_external(db, entry)
    except IntegrationError as exc:
        raise HTTPException(502, str(exc)) from exc
    name = info["integration"]
    return GrabOut(
        ok=True, integration=name,
        message=f"Queued in {name}. It will appear in your library once downloaded "
                "into a watched folder.",
    )


@router.post("/catalog/{catalog_id}/hook", response_model=WorkOut)
async def hook_catalog(
    catalog_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Work:
    """Add a discovered work to the caller's library. If it's already hooked (by anyone), just add
    membership and surface it — no re-crawl, no new jobs. Otherwise pull it via the adaptive web
    adapter and self-diagnose completeness; the Work + crawl are shared across users."""
    entry = db.get(CatalogWork, catalog_id)
    if entry is None:
        raise HTTPException(404, "Catalog entry not found")
    # Already in the global catalog as hooked → membership only.
    if entry.hooked_work_id is not None:
        work = db.get(Work, entry.hooked_work_id)
        if work is not None:
            add_to_library(db, user.id, work.id)
            cache.clear("catalog")
            return work
    try:
        work = await catalog.hook_entry(db, entry)
        add_to_library(db, user.id, work.id)
        cache.clear("catalog")  # hooked flags / stats changed
        return work
    except ComplianceError as exc:
        raise HTTPException(403, str(exc)) from exc


# --------------------------------------------------------------- remove + block
# Health verdicts that mark a catalog entry as "broken" content.
BROKEN_HEALTH = ("no_chapters", "incomplete", "unreachable")


def _delete_catalog_entry(db: Session, entry: CatalogWork) -> None:
    """Remove a catalog entry + the indexed landing page(s) for its work URL (and their FTS
    rows), so the removed content also leaves full-text search. Per design, the already-hooked
    library Work (if any) is intentionally left in place. Chapter pages attributed to this work
    but living at other URLs aren't reverse-mapped here; the blocklist stops them re-surfacing."""
    if entry.site_id is not None:
        conn = db.connection()
        landing = db.scalars(
            select(IndexedPage).where(
                IndexedPage.site_id == entry.site_id, IndexedPage.url == entry.work_url
            )
        ).all()
        for page in landing:
            index_fts_delete(conn, page.id)
            db.delete(page)
    db.delete(entry)


@router.delete("/catalog/{catalog_id}", dependencies=[Depends(require_admin)])
def remove_catalog(
    catalog_id: int,
    block: bool = Query(True, description="also bar this content from being re-added"),
    block_domain: bool = Query(False, description="block the whole domain, not just this URL"),
    db: Session = Depends(get_db),
) -> dict:
    """Remove broken/unwanted content from the index. By default it's also blocked so a later
    crawl won't re-discover it. The hooked library copy (if any) is left untouched."""
    entry = db.get(CatalogWork, catalog_id)
    if entry is None:
        raise HTTPException(404, "Catalog entry not found")
    blocked = None
    if block and entry.work_url:
        b = blocklist.add_block(
            db, scope=("domain" if block_domain else "url"), value=entry.work_url,
            reason="removed broken content", title=entry.title,
        )
        blocked = {"scope": b.scope, "value": b.value}
    _delete_catalog_entry(db, entry)
    db.commit()
    cache.clear("catalog")
    return {"deleted": catalog_id, "blocked": blocked}


@router.post("/catalog/purge-broken", dependencies=[Depends(require_admin)])
def purge_broken(
    block: bool = Query(True),
    db: Session = Depends(get_db),
) -> dict:
    """Bulk-remove every crawled (web_index) catalog entry whose diagnosed health is broken
    and that hasn't been hooked into the library. Each removed URL is blocked when block=True."""
    entries = db.scalars(
        select(CatalogWork).where(
            CatalogWork.provider == "web_index",
            CatalogWork.hooked_work_id.is_(None),
            CatalogWork.health.in_(BROKEN_HEALTH),
        )
    ).all()
    removed = 0
    for entry in entries:
        if block and entry.work_url:
            blocklist.add_block(db, scope="url", value=entry.work_url,
                                reason="bulk purge: broken content", title=entry.title)
        _delete_catalog_entry(db, entry)
        removed += 1
    db.commit()
    if removed:
        cache.clear("catalog")
    return {"removed": removed}


@router.get("/index/blocks", response_model=list[IndexBlockOut],
            dependencies=[Depends(require_admin)])
def list_blocks(db: Session = Depends(get_db)) -> list[IndexBlock]:
    return db.scalars(select(IndexBlock).order_by(IndexBlock.created_at.desc())).all()


@router.post("/index/blocks", response_model=IndexBlockOut,
             dependencies=[Depends(require_admin)])
def add_block(payload: IndexBlockIn, db: Session = Depends(get_db)) -> IndexBlock:
    return blocklist.add_block(
        db, scope=payload.scope, value=payload.value,
        reason=payload.reason or "manually blocked", title=payload.title,
    )


@router.delete("/index/blocks/{block_id}", dependencies=[Depends(require_admin)])
def remove_block(block_id: int, db: Session = Depends(get_db)) -> dict:
    b = db.get(IndexBlock, block_id)
    if b is None:
        raise HTTPException(404, "Block not found")
    db.delete(b)
    db.commit()
    return {"deleted": block_id}


# ----------------------------------------------------------------------- hook
@router.post("/index/pages/{page_id}/hook", response_model=WorkOut)
def hook_page(
    page_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Work:
    p = db.get(IndexedPage, page_id)
    if p is None:
        raise HTTPException(404, "Page not found")
    if p.status != "fetched" or not p.html:
        raise HTTPException(409, "Page has no fetched content yet.")
    if p.hooked_work_id is not None:  # already hooked → membership only, no re-store
        work = db.get(Work, p.hooked_work_id)
        if work is not None:
            add_to_library(db, user.id, work.id)
            return work
    src = ensure_source(db, registry.get("web_index"))

    ref = f"indexpage:{p.id}"
    work = db.scalar(select(Work).where(Work.source_id == src.id, Work.source_work_ref == ref))
    if work is None:
        work = Work(source_id=src.id, source_work_ref=ref)
        db.add(work)
    work.title = p.title or p.url
    work.author = p.author
    work.description = p.description or (p.text or "")[:2000] or None
    work.cover_url = p.cover_url
    work.language = "en"
    work.status = "complete"
    work.hooked = False
    work.media_kind = "text"
    work.total_chapters_known = 1
    db.commit()
    db.refresh(work)

    for ch in list(work.chapters):
        db.delete(ch)
    db.commit()
    ch = Chapter(work_id=work.id, source_chapter_ref=ref, index=1,
                 title=work.title, fetch_status="pending")
    db.add(ch)
    db.flush()
    store_chapter_content(db, ch, RawChapter(title=work.title, body=p.html, fmt="html"))

    p.hooked_work_id = work.id
    db.commit()
    db.refresh(work)
    add_to_library(db, user.id, work.id)
    return work


@router.post("/index/sites/{site_id}/hook", response_model=WorkOut)
def hook_site(
    site_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Work:
    """Add every fetched page of a site to the caller's library as one multi-chapter work."""
    site = db.get(IndexSite, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    pages = db.scalars(
        select(IndexedPage)
        .where(IndexedPage.site_id == site_id, IndexedPage.status == "fetched")
        .order_by(IndexedPage.depth, IndexedPage.id)
    ).all()
    pages = [p for p in pages if p.html]
    if not pages:
        raise HTTPException(409, "This site has no fetched pages yet.")
    src = ensure_source(db, registry.get("web_index"))

    ref = f"indexsite:{site.id}"
    work = db.scalar(select(Work).where(Work.source_id == src.id, Work.source_work_ref == ref))
    if work is None:
        work = Work(source_id=src.id, source_work_ref=ref)
        db.add(work)
    cover = next((p.cover_url for p in pages if p.cover_url), None)
    work.title = site.title or site.domain
    work.author = next((p.author for p in pages if p.author), None)
    work.description = next((p.description for p in pages if p.description), None)
    work.cover_url = cover
    work.language = "en"
    work.status = "complete"
    work.hooked = False
    work.media_kind = "text"
    work.total_chapters_known = len(pages)
    db.commit()
    db.refresh(work)

    for ch in list(work.chapters):
        db.delete(ch)
    db.commit()
    for i, p in enumerate(pages, start=1):
        ch = Chapter(work_id=work.id, source_chapter_ref=f"indexpage:{p.id}", index=i,
                     title=p.title or p.url, fetch_status="pending")
        db.add(ch)
        db.flush()
        store_chapter_content(db, ch, RawChapter(title=p.title or p.url, body=p.html, fmt="html"))
        p.hooked_work_id = work.id
    db.commit()
    db.refresh(work)
    add_to_library(db, user.id, work.id)
    return work
