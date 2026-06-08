"""URL Index API — submit sites to auto-crawl, browse pages, full-text search, hook to library."""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import exists, func, select, text, update
from sqlalchemy.orm import Session

from .. import cache
from .. import db as dbmod
from ..auth import current_user, require_admin
from ..db import get_db, index_fts_delete
from ..library import add_to_library
from ..ingestion import acquire, blocklist, catalog
from ..ingestion.base import RawChapter, registry
from ..ingestion.engine import ComplianceError, ensure_source, store_chapter_content
from ..ingestion.indexer import start_index
from ..models import (
    CatalogWork,
    Chapter,
    DownloadJob,
    IndexBlock,
    IndexedPage,
    IndexSite,
    User,
    Work,
)
from ..config import get_settings
from ..schemas import (
    BookCatalogConfigIn,
    CatalogGroupOut,
    CatalogRowOut,
    CrawlTuningIn,
    DownloadJobOut,
    FetchPriorityIn,
    ReleaseCandidateOut,
    SeriesAcquireIn,
    SeriesOut,
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
    OperatorIdentityIn,
    OperatorIdentityOut,
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


@router.get("/operator/identity", response_model=OperatorIdentityOut)
def get_operator_identity(db: Session = Depends(get_db)) -> OperatorIdentityOut:
    """The crawler's public identity (User-Agent + contact) sent to every source it fetches."""
    from ..ingestion import operator_identity
    return OperatorIdentityOut(**operator_identity.get_identity(db))


@router.put("/operator/identity", response_model=OperatorIdentityOut,
            dependencies=[Depends(require_admin)])
def put_operator_identity(
    payload: OperatorIdentityIn, db: Session = Depends(get_db)
) -> OperatorIdentityOut:
    """Set the crawl identity. Applies live: the next request of every running + future crawl
    carries the new User-Agent + From contact — no restart. A blank field resets to the default."""
    from ..ingestion import operator_identity
    updated = operator_identity.set_identity(db, payload.model_dump(exclude_none=True))
    return OperatorIdentityOut(**updated)


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
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[CatalogGroupOut]:
    """The searchable catalog of discovered works, grouped + deduped across sources (web
    crawl + Readarr + Kapowarr) so the same title is one card with selectable sources.
    Media-type / source filters and sort are applied server-side over the full grouped set,
    restricted to the categories the user may view.
    With ``live=true`` and a query, also looks the term up live in connected integrations."""
    # Cache the BASE grouped set (the expensive find_rows + cross-source grouping) keyed ONLY by
    # the inputs that affect it — q / site / hooked. Media-type/source filtering and sort are cheap
    # and applied per request on the cached base, so changing the sort or a filter never re-groups
    # (that was the "sorting is slow" symptom) — it just re-orders an in-memory list.
    bkey = f"catalog-base:{q}:{site_id}:{hooked}"
    cached: list[dict] | None = None if live else cache.get(bkey)

    # Hybrid book catalog: only on a cache miss, if the local catalog has no sufficiently-close
    # match for the query, resolve it live against the book APIs (Google Books + Open Library) and
    # persist the results so this and future searches are served locally. Closeness-gated +
    # per-query guarded, so common/warm searches never probe or hit the APIs. Best-effort.
    # Books from googlebooks/openlibrary/hardcover are only acquirable via the Prowlarr+SABnzbd
    # pipeline. With no pipeline, don't live-resolve them (it would surface + persist unhookable
    # items) and filter any already-seeded book-only groups out of the results below.
    hide_books = _hide_pipeline_books(db)
    resolved = False
    if cached is None and q and q.strip() and not hide_books:
        from ..ingestion import book_catalog
        try:
            resolved = await book_catalog.resolve_if_sparse(db, q.strip())
        except Exception:  # noqa: BLE001
            pass

    if live and q and q.strip():
        from ..integrations import sync as isync
        try:
            await isync.search_integrations(db, q.strip())
        except Exception:  # noqa: BLE001 — live lookup is best-effort
            pass
        cache.clear("catalog")  # integration results may have changed the catalog

    base: list[dict] | None = None if (live or resolved) else cached
    if base is None:
        def _group() -> list[dict]:
            rows = catalog.find_rows(db, q=q, site_id=site_id, hooked=hooked, limit=2000)
            return catalog.group_rows(rows, q=q)

        # Grouping is CPU-bound + synchronous — run it off the event loop so it never blocks
        # concurrent API requests.
        base = await asyncio.to_thread(_group)
        if not live:
            cache.put(bkey, base, ttl=15.0)
    groups = catalog.filter_and_sort_groups(base, media=media, domain=domain, sort=sort)
    # Enforce the user's category cap (admins → all) regardless of the requested media filter.
    allowed = set(catalog.effective_categories(db, user))
    groups = [g for g in groups if g.get("media_label") in allowed]
    if hide_books:
        # Drop groups whose ONLY sources are pipeline-only book providers (keep mixed groups, e.g.
        # a title also available on Project Gutenberg).
        groups = [g for g in groups
                  if any(s.get("provider") not in BOOK_PROVIDERS for s in (g.get("sources") or []))]
    return [CatalogGroupOut(**g) for g in groups[offset:offset + limit]]


@router.get("/catalog/book-config", dependencies=[Depends(require_admin)])
def get_book_catalog_config(db: Session = Depends(get_db)) -> dict:
    """Hybrid book-catalog settings + seeding status (admin)."""
    from ..ingestion import book_catalog
    return book_catalog.status(db)


@router.put("/catalog/book-config", dependencies=[Depends(require_admin)])
def put_book_catalog_config(payload: BookCatalogConfigIn, db: Session = Depends(get_db)) -> dict:
    from ..ingestion import book_catalog
    book_catalog.set_config(db, payload.model_dump(exclude_none=True))
    return book_catalog.status(db)


@router.post("/catalog/book-sync", dependencies=[Depends(require_admin)])
async def book_catalog_sync_now(db: Session = Depends(get_db)) -> dict:
    """Manually advance the book-catalog hot-set seed (admin)."""
    from ..ingestion import book_catalog
    return await book_catalog.sync_hot_set(db, max_requests=8)


@router.get("/catalog/facets")
def catalog_facets(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Complete filter options (all media types + source domains) for the Index page. The media
    types are capped to the categories the user may view."""
    hide_books = _hide_pipeline_books(db)
    ckey = f"catalog-facets:{'direct' if hide_books else 'all'}"
    cached = cache.get(ckey)
    if cached is None:
        cached = catalog.catalog_facets(db, hide_books=hide_books)
        cache.put(ckey, cached, ttl=15.0)
    allowed = set(catalog.effective_categories(db, user))
    # Only offer categories the user may view, and only sources that carry at least one of those
    # categories (a Manga-only user shouldn't see a novel/book site as a source filter).
    domain_media = cached.get("domain_media", {})
    domains = [d for d in cached.get("domains", [])
               if any(lbl in allowed for lbl in domain_media.get(d, []))]
    return {"media": [m for m in cached.get("media", []) if m in allowed], "domains": domains}


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


# --------------------------------------------------------------- discovery rows
_BUCKETS = ("comic", "text")
MEDIA_CATEGORIES = catalog.MEDIA_CATEGORIES  # Manga/Manhua/Webtoon/Comic/Novel/Book (display order)
_GENRE_ROWS = 8         # marquee genre lanes per media section
_THEME_ROWS = 6         # theme lanes per media section
_MIN_CATEGORY = 8       # a tag needs at least this many titles to become a row/browse target
_ROW_ITEMS = 20         # titles shown in a (horizontally-scrolled) row


def _serialize_groups(db: Session, groups: list) -> list[dict]:
    """CatalogGroup rows → CatalogGroupOut dicts, with each group's selectable sources resolved
    from its member catalog rows in ONE batched query (no N+1)."""
    from collections import defaultdict
    if not groups:
        return []
    ids = [g.id for g in groups]
    members = db.scalars(
        select(CatalogWork).where(CatalogWork.group_id.in_(ids))
    ).all()
    by_group: dict[int, list[CatalogWork]] = defaultdict(list)
    for m in members:
        by_group[m.group_id].append(m)
    out: list[dict] = []
    for g in groups:
        mem = sorted(by_group.get(g.id, []), key=lambda e: catalog._score(e, None), reverse=True)
        sources = [catalog._source_dict(e) for e in catalog.dedupe_sources(mem)]
        # Fall back to a member's cover when the group has none — surfaces a backfilled cover
        # immediately, before the next regroup tick copies it onto the group.
        cover = g.cover_url or next((m.cover_url for m in mem if (m.cover_url or "").strip()), None)
        out.append({
            "id": g.id, "norm_key": g.norm_key, "title": g.title, "author": g.author,
            "cover_url": cover, "synopsis": g.synopsis, "language": g.language,
            "media_kind": g.media_bucket, "media_label": g.media_label, "chapters": g.chapters,
            "hooked_work_id": g.hooked_work_id, "sources": sources,
        })
    return out


# Catalog items from these providers can ONLY be acquired via the Prowlarr+SABnzbd pipeline.
from ..ingestion.book_catalog import BOOK_PROVIDERS  # noqa: E402


def _has_direct_source():
    """SQL EXISTS: the group has at least one member from a directly-hookable source (anything but a
    pipeline-only book provider). Used to hide googlebooks/openlibrary/hardcover-ONLY groups when
    the acquisition pipeline isn't configured, while keeping mixed groups (e.g. a title also on
    Project Gutenberg) visible."""
    from ..models import CatalogGroup, CatalogWork
    return exists().where(
        CatalogWork.group_id == CatalogGroup.id,
        CatalogWork.provider.notin_(BOOK_PROVIDERS),
    )


def _hide_pipeline_books(db: Session) -> bool:
    """Whether to hide pipeline-only book items from discovery (no Prowlarr+SABnzbd configured)."""
    return not acquire.pipeline_configured(db)


def _sorted_groups_query(*, dimension: str | None, value: str | None,
                         media_label: str | None, sort: str, direct_only: bool = False):
    """Build the base SELECT for browsing groups by an optional (kind, slug) tag + media category
    (Manga/Manhua/Webtoon/Comic/Novel/Book), sorted. With ``direct_only`` it excludes groups whose
    only sources are pipeline-only book providers (hidden when no acquisition pipeline)."""
    from ..models import CatalogGroup, CatalogTag
    sel = select(CatalogGroup)
    if dimension in ("genre", "theme") and value:
        sel = sel.join(CatalogTag, CatalogTag.group_id == CatalogGroup.id).where(
            CatalogTag.kind == dimension, CatalogTag.slug == value
        )
    if media_label in MEDIA_CATEGORIES:
        sel = sel.where(CatalogGroup.media_label == media_label)
    if direct_only:
        sel = sel.where(_has_direct_source())
    if sort == "chapters":
        sel = sel.order_by(CatalogGroup.chapters.is_(None), CatalogGroup.chapters.desc())
    elif sort == "title":
        sel = sel.order_by(CatalogGroup.title.asc())
    elif sort == "new":
        sel = sel.order_by(CatalogGroup.id.desc())
    else:  # popularity (default)
        sel = sel.order_by(CatalogGroup.popularity_norm.desc(), CatalogGroup.id.desc())
    return sel


def _diversity_cap(groups: list, limit: int, frac: float = 0.6) -> list:
    """Trim a popularity-ranked list so no single source dominates the row (keeps the
    cross-genre 'Most Popular' lane varied rather than 20 titles from one site)."""
    cap = max(1, int(limit * frac))
    from collections import defaultdict
    per: dict[str, int] = defaultdict(int)
    out = []
    for g in groups:
        d = g.source_domain or ""
        if per[d] >= cap:
            continue
        per[d] += 1
        out.append(g)
        if len(out) >= limit:
            break
    return out


@router.get("/catalog/rows", response_model=list[CatalogRowOut])
def catalog_rows(
    media: str | None = Query(None, description="Limit to one media category (Manga/Webtoon/…)"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """The Index page's discovery rows: a 'Most Popular' lane plus the marquee genre + theme lanes
    per media section, each a handful of the most-popular titles. Reads the precomputed grouping
    (cheap indexed LIMIT queries); heavily cached + invalidated by the regroup tick. Sections are
    capped to the categories the (admin-controlled) user may view; the full set is cached once and
    the per-user allow-list is applied after."""
    from ..models import CatalogCategory, CatalogGroup
    allowed = set(catalog.effective_categories(db, user))
    direct = _hide_pipeline_books(db)  # pipeline-only book items hidden when no Prowlarr+SABnzbd
    ckey = f"catalog-rows:{media or 'all'}:{'direct' if direct else 'all'}"
    cached = cache.get(ckey)
    if cached is not None:
        return [r for r in cached if r["media_label"] in allowed]
    # One section per media category (Manga/Manhua/Webtoon/Comic/Novel/Book). The frontend then
    # shows only the categories the user has enabled — the server returns them all (cached once).
    labels = [media] if media in MEDIA_CATEGORIES else list(MEDIA_CATEGORIES)
    rows: list[dict] = []
    for label in labels:
        # Most Popular lane (source-diversity-capped) — works even before any genre enrichment.
        pop = db.scalars(
            _sorted_groups_query(dimension=None, value=None, media_label=label, sort="popularity",
                                 direct_only=direct)
            .limit(_ROW_ITEMS * 4)
        ).all()
        pop = _diversity_cap(pop, _ROW_ITEMS)
        if pop:
            count_sel = select(func.count(CatalogGroup.id)).where(CatalogGroup.media_label == label)
            if direct:
                count_sel = count_sel.where(_has_direct_source())
            total = db.scalar(count_sel) or 0
            rows.append({"kind": "popular", "slug": "", "label": "Most Popular",
                         "media_label": label, "count": int(total),
                         "items": _serialize_groups(db, pop)})
        # Genre then theme lanes — the most populous categories in this media category.
        for kind, cap in (("genre", _GENRE_ROWS), ("theme", _THEME_ROWS)):
            cats = db.execute(
                select(CatalogCategory.slug, CatalogCategory.label, CatalogCategory.group_count)
                .where(CatalogCategory.kind == kind, CatalogCategory.media_label == label,
                       CatalogCategory.group_count >= _MIN_CATEGORY)
                .order_by(CatalogCategory.group_count.desc()).limit(cap)
            ).all()
            for slug, clabel, count in cats:
                items = db.scalars(
                    _sorted_groups_query(dimension=kind, value=slug, media_label=label,
                                         sort="popularity", direct_only=direct).limit(_ROW_ITEMS)
                ).all()
                if items:
                    rows.append({"kind": kind, "slug": slug, "label": clabel,
                                 "media_label": label, "count": int(count),
                                 "items": _serialize_groups(db, items)})
    cache.put(ckey, rows, ttl=120.0)
    return [r for r in rows if r["media_label"] in allowed]


@router.get("/catalog/categories")
def catalog_categories(
    media: str | None = Query(None, description="Limit to one media category (Manga/Webtoon/…)"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """All browsable genre/theme categories (with title counts) for the browse nav — restricted to
    the media categories this user may view."""
    from ..models import CatalogCategory
    # Genre/theme categories under the "Book" label come from the book providers; with no
    # acquisition pipeline those items are hidden, so drop their browse categories too. The full
    # set is cached once; the per-user category cap is applied after.
    hide_books = _hide_pipeline_books(db)
    ckey = f"catalog-cat:{media or 'all'}:{'direct' if hide_books else 'all'}"
    cached = cache.get(ckey)
    if cached is None:
        sel = select(CatalogCategory.kind, CatalogCategory.slug, CatalogCategory.label,
                     CatalogCategory.media_label, CatalogCategory.group_count).where(
            CatalogCategory.group_count >= _MIN_CATEGORY)
        if media in MEDIA_CATEGORIES:
            sel = sel.where(CatalogCategory.media_label == media)
        if hide_books:
            sel = sel.where(CatalogCategory.media_label != "Book")
        sel = sel.order_by(CatalogCategory.group_count.desc())
        cached = [{"kind": k, "slug": s, "label": lab, "media_label": ml, "count": int(c)}
                  for (k, s, lab, ml, c) in db.execute(sel).all()]
        cache.put(ckey, cached, ttl=120.0)
    allowed = set(catalog.effective_categories(db, user))
    return {"categories": [c for c in cached if c["media_label"] in allowed]}


@router.get("/catalog/browse", response_model=list[CatalogGroupOut])
def catalog_browse(
    dimension: str = Query("popular", description="popular | genre | theme"),
    value: str | None = Query(None, description="category slug (for genre/theme)"),
    media: str | None = Query(None, description="Limit to one media category (Manga/Webtoon/…)"),
    sort: str = Query("popularity", description="popularity | chapters | title | new"),
    limit: int = Query(60, ge=1, le=120),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """The Browse grid for one category: sorted, paginated titles from the precomputed grouping —
    restricted to the categories the user may view."""
    from ..models import CatalogGroup
    allowed = catalog.effective_categories(db, user)
    if media and media not in allowed:
        return []  # browsing a category this user isn't permitted to see
    dim = dimension if dimension in ("genre", "theme") else None
    sel = _sorted_groups_query(dimension=dim, value=value, media_label=media, sort=sort,
                               direct_only=_hide_pipeline_books(db))
    if not media:  # the "all" browse must still honour the user's category cap
        sel = sel.where(CatalogGroup.media_label.in_(allowed))
    groups = db.scalars(sel.limit(limit).offset(offset)).all()
    return _serialize_groups(db, groups)


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


def _candidate_out(s) -> ReleaseCandidateOut:
    r = s.release
    return ReleaseCandidateOut(
        title=getattr(r, "title", ""), indexer=getattr(r, "indexer", None),
        guid=getattr(r, "guid", None), size=int(getattr(r, "size", 0) or 0),
        size_mb=getattr(r, "size_mb", 0.0), fmt=s.info.fmt, is_audiobook=s.info.is_audiobook,
        language=s.info.language, confidence=round(s.confidence, 3), score=s.score,
        accepted=s.accepted, auto_ok=s.auto_ok, reason=s.reason,
    )


def _job_out(j: DownloadJob) -> DownloadJobOut:
    return DownloadJobOut(
        id=j.id, catalog_work_id=j.catalog_work_id, title=j.title, release_title=j.release_title,
        indexer=j.indexer, size=j.size, fmt=j.fmt, status=j.status, grab_kind=j.grab_kind,
        work_id=j.work_id, error=j.error, not_before=j.not_before, created_at=j.created_at,
        updated_at=j.updated_at, completed_at=j.completed_at,
    )


@router.get("/catalog/{catalog_id}/releases", response_model=list[ReleaseCandidateOut])
async def catalog_releases(catalog_id: int, db: Session = Depends(get_db)) -> list[ReleaseCandidateOut]:
    """Preview ranked Prowlarr release candidates for a catalog book (usenet pipeline)."""
    cw = db.get(CatalogWork, catalog_id)
    if cw is None:
        raise HTTPException(404, "Catalog entry not found")
    from ..ingestion import release_matcher as rm
    ranked = await rm.find_releases(db, cw)
    return [_candidate_out(s) for s in ranked]


@router.post("/catalog/{catalog_id}/grab-pipeline", response_model=DownloadJobOut)
async def grab_pipeline(
    catalog_id: int, guid: str | None = Query(None, description="Grab this specific release"),
    fuzz: bool = Query(False, description="Book-fuzz: try every match (low-confidence too) and "
                                         "content-verify each"),
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> DownloadJobOut:
    """Grab a catalog book through the usenet pipeline (Prowlarr → SABnzbd). With ``guid`` grabs
    that specific release; otherwise grabs the best release that clears the strict auto-grab gate.
    With ``fuzz`` it casts a wide net — every release that even loosely matches the title is
    downloaded and content-verified in turn, and only a real match is kept; if NONE match the job
    fails clearly, telling the user no acquisition method has the title. The imported book lands in
    the caller's library."""
    from ..integrations import IntegrationError
    from ..ingestion import downloads, release_matcher as rm

    cw = db.get(CatalogWork, catalog_id)
    if cw is None:
        raise HTTPException(404, "Catalog entry not found")
    if cw.hooked_work_id:
        raise HTTPException(400, "This title is already in the library")

    ranked = await rm.find_releases(db, cw, fuzz=fuzz)
    # Build a candidate cascade: the download path tries each in turn, content-verifies it, and
    # advances past any that fail/are the wrong book — so availability is high without false imports.
    cap = 25 if fuzz else 20
    candidates = rm.candidate_dicts(ranked, cap=cap, include_speculative=True)
    if guid:
        if not any(c.get("guid") == guid for c in candidates):
            raise HTTPException(404, "Release not found (or no longer available)")
        candidates.sort(key=lambda c: 0 if c.get("guid") == guid else 1)  # picked release first
        kind = "manual"
    else:
        if not candidates:
            detail = ("No release even loosely matched this title on your indexers."
                      if fuzz else "No matching release found for this title")
            raise HTTPException(409, detail)
        kind = "fuzz" if fuzz else ("auto" if candidates[0]["auto_ok"] else "manual")
    # Fuzz tries the whole wide net; a normal grab keeps the tighter cap.
    candidates = candidates[: (cap if fuzz else downloads.CANDIDATE_CAP)]
    try:
        job = await downloads.grab_release(db, cw, candidates=candidates, user_id=user.id, kind=kind)
    except IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _job_out(job)


@router.get("/catalog/{catalog_id}/series", response_model=SeriesOut)
async def catalog_series(catalog_id: int, db: Session = Depends(get_db)) -> SeriesOut:
    """Detect this book's series and list its volumes (ordered) — for 'fetch the whole series'."""
    cw = db.get(CatalogWork, catalog_id)
    if cw is None:
        raise HTTPException(404, "Catalog entry not found")
    from ..ingestion import series
    return SeriesOut(**await series.detect_series(db, cw))


@router.post("/catalog/{catalog_id}/series/acquire")
async def acquire_series_ep(
    catalog_id: int, payload: SeriesAcquireIn,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> dict:
    """Acquire the whole series (all=true) or a custom selection (refs) via the caller's route
    priority. Each volume lands in the caller's library."""
    cw = db.get(CatalogWork, catalog_id)
    if cw is None:
        raise HTTPException(404, "Catalog entry not found")
    if not payload.all and not payload.refs:
        raise HTTPException(400, "Select at least one book, or set all=true")
    from ..ingestion import series
    results = await series.acquire_series(
        db, cw, refs=payload.refs or None, want_all=payload.all, user_id=user.id,
    )
    cache.clear("catalog")
    return {"results": results}


@router.get("/downloads", response_model=list[DownloadJobOut])
def list_downloads(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[DownloadJobOut]:
    """Acquisition jobs — the caller's own (admins see all), newest first."""
    sel = select(DownloadJob).order_by(DownloadJob.created_at.desc()).limit(200)
    if user.role != "admin":
        sel = sel.where(DownloadJob.user_id == user.id)
    return [_job_out(j) for j in db.scalars(sel).all()]


@router.delete("/downloads/{job_id}")
def delete_download(job_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    job = db.get(DownloadJob, job_id)
    if job is None:
        raise HTTPException(404, "Download not found")
    if user.role != "admin" and job.user_id != user.id:
        raise HTTPException(403, "Not your download")
    db.delete(job)
    db.commit()
    return {"deleted": job_id}


@router.get("/fetch-priority")
def get_fetch_priority(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """The acquisition route priority: the available routes, the global default, and the caller's
    effective order (their override if set, else the global default)."""
    return {
        "routes": list(acquire.ROUTES),
        "global": acquire.global_priority(db),
        "effective": acquire.user_priority(db, user),
    }


@router.put("/fetch-priority")
def set_fetch_priority(payload: FetchPriorityIn, user: User = Depends(current_user),
                       db: Session = Depends(get_db)) -> dict:
    """Set the caller's personal route-priority override."""
    eff = acquire.set_user_priority(db, user.id, payload.order)
    return {"effective": eff}


@router.put("/fetch-priority/global", dependencies=[Depends(require_admin)])
def set_global_fetch_priority(payload: FetchPriorityIn, db: Session = Depends(get_db)) -> dict:
    """Set the operator-wide default route priority (admin)."""
    return {"global": acquire.set_global_priority(db, payload.order)}


@router.get("/catalog/{catalog_id}/routes")
def catalog_routes(catalog_id: int, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> dict:
    """Which acquisition routes can fulfill this work, plus the caller's priority order."""
    cw = db.get(CatalogWork, catalog_id)
    if cw is None:
        raise HTTPException(404, "Catalog entry not found")
    return {
        "available": acquire.available_routes(db, cw),
        "priority": acquire.user_priority(db, user),
        "hooked_work_id": cw.hooked_work_id,
    }


@router.post("/catalog/{catalog_id}/acquire")
async def acquire_catalog(
    catalog_id: int, route: str | None = Query(None, description="Force a specific route"),
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> dict:
    """Acquire a catalog work via the caller's route priority (or a forced ``route``): hook a web
    source, grab via a connected manager, or download through the usenet pipeline — whichever the
    priority resolves to first. The result lands in the caller's library."""
    cw = db.get(CatalogWork, catalog_id)
    if cw is None:
        raise HTTPException(404, "Catalog entry not found")
    if route is not None and route not in acquire.ROUTES:
        raise HTTPException(400, f"unknown route {route!r}")
    result = await acquire.acquire(
        db, cw, user_id=user.id, priority=acquire.user_priority(db, user), route=route,
    )
    cache.clear("catalog")
    if result.get("status") == "none":
        raise HTTPException(409, result.get("detail") or "no available route could fulfill this title")
    return result


@router.post("/catalog/{catalog_id}/hook", response_model=WorkOut)
async def hook_catalog(
    catalog_id: int,
    start_chapter: int = Query(1, ge=1, description="Hook from this chapter (skip earlier ones)"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Work:
    """Add a discovered work to the caller's library. If it's already hooked (by anyone), just add
    membership and surface it — no re-crawl, no new jobs. Otherwise pull it via the adaptive web
    adapter and self-diagnose completeness; the Work + crawl are shared across users.

    ``start_chapter`` lets a fresh hook begin partway in (skip chapters already read elsewhere); it
    only applies the first time a work is hooked, not when joining an already-hooked shared Work."""
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
        work = await catalog.hook_entry(db, entry, start_chapter=start_chapter)
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
