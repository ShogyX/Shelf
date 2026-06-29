"""URL Index API — submit sites to auto-crawl, browse pages, full-text search, hook to library."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, exists, func, select, text
from sqlalchemy.orm import Session

from .. import cache
from .. import db as dbmod
from ..auth import current_user, require_admin, require_permission
from ..db import get_db
from ..library import add_to_library, validate_shelf
from ..ingestion import acquire, blocklist, catalog, index_admin
from ..ingestion.base import RawChapter, registry
from ..ingestion.engine import ensure_source, store_chapter_content
from ..models import (
    CatalogWork,
    Chapter,
    DownloadJob,
    IndexBlock,
    IndexedPage,
    IndexSite,
    LibraryItem,
    User,
    Work,
)
from .. import config_store
from ..schemas import (
    AuthorAcquireIn,
    AuthorBooksOut,
    BookCatalogConfigIn,
    CatalogGroupOut,
    CatalogRowOut,
    CrawlTuningIn,
    DownloadJobOut,
    FetchPriorityIn,
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

log = logging.getLogger("shelf.index")
router = APIRouter()

# Candidate-row ceiling for a SEARCH (vs the 2000 popularity slice used for a no-query browse). A
# filtered query is already narrowed by the q filter, so this only bounds a pathologically-broad
# search; obscure matches no longer fall off the popularity-ranked cliff at scale (P2).
_SEARCH_CANDIDATE_LIMIT = 20000

# Capability gates for the user-facing Index surface (admins implicitly pass all). Infrastructure
# management endpoints keep their separate require_admin gate.
_INDEX_VIEW = Depends(require_permission("index.view"))
_INDEX_HOOK = Depends(require_permission("index.hook"))
_INDEX_ACQUIRE = Depends(require_permission("index.acquire"))


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC for arithmetic."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# Per-site stats assembly lives in the index_admin service (it's index-site domain logic); the
# read endpoints below import it back for serialization.
_build_site_out = index_admin.build_site_out


# ---------------------------------------------------------------------- sites
@router.get("/index/request-stats", dependencies=[Depends(require_admin)])
def request_stats(hours: int = Query(48, ge=1, le=720), db: Session = Depends(get_db)) -> dict:
    """Outbound-request telemetry for the Settings → Index dashboard: totals, derived rates, and an
    hourly time series by destination host + category (crawl/metadata/integration/…)."""
    from .. import telemetry
    return telemetry.summary(db, hours=hours)


# grab_kind → the pipeline ROUTE that ran the download. Anything not listed is the usenet pipeline
# (manual/auto/stock/fuzz all go SAB). librivox is the public-domain audiobook route.
_GRAB_ROUTE = {"torrent": "torrent", "libgen": "anna's archive", "librivox": "librivox"}
_ACTIVE_JOB = ("queued", "downloading", "completed", "retry", "deferred")
# ContentRequest.failure_reason → a human explanation (matches ledger.REASONS).
_FAILURE_LABEL = {
    "no_match": "No confident release matched — the pipeline searched every route and found nothing "
                "(or nothing above the confidence threshold).",
    "unverified": "Downloaded a candidate, but its content didn't verify as the requested title.",
    "all_broken": "Every candidate release was already known-broken (dead/wrong link).",
    "rate_limited": "A provider rate-limited the request.",
    "blocked": "Blocked (anti-bot / Cloudflare).",
    "timeout": "Timed out reaching a provider.",
    "error": "Errored during search/download.",
}


@router.get("/stats/pipeline", dependencies=[Depends(require_admin)])
def pipeline_stats(db: Session = Depends(get_db)) -> dict:
    """Acquisition-pipeline outcomes for the Settings → Statistics page: per-route download
    success/failure (usenet / torrent / Anna's Archive / LibriVox), web-crawl hooks, and — for titles
    that couldn't be obtained — WHY (the missing-content ledger's failure-reason taxonomy)."""
    from datetime import UTC, datetime

    from ..models import ContentRequest, DownloadJob, Subscription, WorkSourceSearch

    routes: dict[str, dict] = {}
    for gk, st, n in db.execute(
        select(DownloadJob.grab_kind, DownloadJob.status, func.count())
        .group_by(DownloadJob.grab_kind, DownloadJob.status)
    ).all():
        d = routes.setdefault(_GRAB_ROUTE.get(gk or "", "usenet"),
                              {"route": "", "imported": 0, "failed": 0, "active": 0})
        if st == "imported":
            d["imported"] += n
        elif st == "failed":
            d["failed"] += n
        elif st in _ACTIVE_JOB:
            d["active"] += n
    # hit_rate = imported / (imported + failed) per route — the "% hit" shown in Insights.
    by_route = [
        {**v, "route": r,
         "hit_rate": round(v["imported"] / (v["imported"] + v["failed"]), 3)
                     if (v["imported"] + v["failed"]) else None}
        for r, v in sorted(routes.items())
    ]
    totals = {k: sum(v[k] for v in routes.values()) for k in ("imported", "failed", "active")}

    # "web fetch" successes aren't DownloadJobs — they're web-crawl catalog rows hooked into a Work.
    # provider='web_index' matches ~87% of catalog_works, so force the PARTIAL index (created by
    # db._ensure_indexes at boot) — it answers this COUNT as a covering scan of just the hooked rows.
    from sqlalchemy import text
    web_hooked = db.scalar(text(
        "SELECT COUNT(*) FROM catalog_works INDEXED BY ix_catalog_works_web_hooked "
        "WHERE provider = 'web_index' AND hooked_work_id IS NOT NULL")) or 0

    req_status = {s: n for s, n in db.execute(
        select(ContentRequest.status, func.count()).group_by(ContentRequest.status)).all()}
    failure_reasons = [
        {"reason": (r or "error"), "count": n, "label": _FAILURE_LABEL.get(r or "error", "Unknown.")}
        for r, n in db.execute(
            select(ContentRequest.failure_reason, func.count())
            .where(ContentRequest.status == "unavailable")
            .group_by(ContentRequest.failure_reason)
            .order_by(func.count().desc())).all()
    ]
    # Wave B per-source search state: per source, how many titles are terminal (searched: no_match/
    # exhausted/skipped), queued for retry (unavailable), or in flight (matched/searching) — plus how
    # many of the queued are due to re-search NOW (the retry tick's backlog).
    now = datetime.now(UTC)
    src: dict[str, dict] = {}
    for source, st, n in db.execute(
        select(WorkSourceSearch.source, WorkSourceSearch.status, func.count())
        .group_by(WorkSourceSearch.source, WorkSourceSearch.status)).all():
        d = src.setdefault(source, {"source": source, "searched": 0, "queued": 0, "in_flight": 0})
        if st in ("no_match", "exhausted", "skipped"):
            d["searched"] += n
        elif st == "unavailable":
            d["queued"] += n
        elif st in ("matched", "searching"):
            d["in_flight"] += n
    due_now = db.scalar(select(func.count()).where(
        WorkSourceSearch.status == "unavailable",
        WorkSourceSearch.next_retry_at.is_not(None),
        WorkSourceSearch.next_retry_at <= now)) or 0

    # Wave E follows: active subscriptions by kind + total titles auto-added by the follow tick.
    follow = {"authors": 0, "series": 0, "auto_added": 0}
    for kind, n, added in db.execute(
        select(Subscription.kind, func.count(), func.coalesce(func.sum(Subscription.auto_added), 0))
        .where(Subscription.active.is_(True)).group_by(Subscription.kind)).all():
        follow["authors" if kind == "author" else "series"] = int(n)
        follow["auto_added"] += int(added or 0)

    return {
        "downloads": {"by_route": by_route, "totals": totals},
        "web_fetch": {"hooked": int(web_hooked)},
        "requests": {k: int(req_status.get(k, 0))
                     for k in ("resolved", "unavailable", "open", "searching")},
        "failure_reasons": failure_reasons,
        "sources": {"by_source": [src[s] for s in sorted(src)], "due_now": int(due_now)},
        "following": follow,
    }


# --- Insights aggregations (time-series) — for the redesigned Settings → Insights charts ---------

def _daily_acquisitions(db: Session, days: int) -> list[dict]:
    """Per-day imported/failed download counts + mean acquire seconds over the last `days`, zero-filled
    to a continuous daily series (oldest → newest). Acquire time = completed_at − created_at (imported)."""
    from datetime import UTC, datetime, timedelta
    from sqlalchemy import case
    from ..models import DownloadJob

    cutoff = (datetime.now(UTC) - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(
        select(
            func.date(DownloadJob.completed_at).label("d"),
            func.sum(case((DownloadJob.status == "imported", 1), else_=0)).label("imported"),
            func.sum(case((DownloadJob.status == "failed", 1), else_=0)).label("failed"),
            func.avg(case((DownloadJob.status == "imported",
                           (func.julianday(DownloadJob.completed_at)
                            - func.julianday(DownloadJob.created_at)) * 86400.0), else_=None)).label("acq"),
        )
        .where(DownloadJob.completed_at.is_not(None), DownloadJob.completed_at >= cutoff)
        .group_by("d")
    ).all()
    by_day = {r.d: r for r in rows}
    out: list[dict] = []
    base = datetime.now(UTC).date()
    for i in range(days - 1, -1, -1):
        d = (base - timedelta(days=i)).isoformat()
        r = by_day.get(d)
        out.append({"date": d,
                    "imported": int(r.imported) if r else 0,
                    "failed": int(r.failed) if r else 0,
                    "acquire_s": round(float(r.acq), 1) if (r and r.acq is not None) else None})
    return out


@router.get("/stats/acquisitions", dependencies=[Depends(require_admin)])
def stats_acquisitions(days: int = Query(14, ge=1, le=120), db: Session = Depends(get_db)) -> dict:
    """Daily imported/failed acquisitions (+ mean acquire seconds) over the window — the Insights
    'Acquisitions over time' area chart and the Downloaded sparkline."""
    return {"days": _daily_acquisitions(db, days)}


@router.get("/stats/library-growth", dependencies=[Depends(require_admin)])
def stats_library_growth(days: int = Query(90, ge=7, le=365), db: Session = Depends(get_db)) -> dict:
    """Library size over time: titles added per day + the running cumulative total — the Insights
    'Library growth' area chart and the Titles-in-library sparkline."""
    from datetime import UTC, datetime, timedelta
    from ..models import Work

    cutoff = (datetime.now(UTC) - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(
        select(func.date(Work.created_at).label("d"), func.count())
        .where(Work.hooked.is_(True), Work.created_at >= cutoff)
        .group_by("d")
    ).all()
    added = {d: int(n) for d, n in rows}
    total = db.scalar(select(func.count()).where(Work.hooked.is_(True))) or 0
    # Cumulative line: start from the size at the window's left edge (total minus everything added since).
    base = datetime.now(UTC).date()
    in_window = sum(added.values())
    running = int(total) - in_window
    out: list[dict] = []
    for i in range(days - 1, -1, -1):
        d = (base - timedelta(days=i)).isoformat()
        running += added.get(d, 0)
        out.append({"date": d, "added": added.get(d, 0), "total": running})
    return {"days": out, "total": int(total)}


@router.get("/stats/overview", dependencies=[Depends(require_admin)])
def stats_overview(db: Session = Depends(get_db)) -> dict:
    """The four Insights KPI tiles: downloaded (30d), acquisition success rate (30d), mean acquire time
    (30d), and titles in library — each with a small daily sparkline series."""
    from datetime import UTC, datetime, timedelta
    from sqlalchemy import case
    from ..models import DownloadJob, Work

    cutoff30 = datetime.now(UTC) - timedelta(days=30)
    imp, fail, acq = db.execute(
        select(
            func.sum(case((DownloadJob.status == "imported", 1), else_=0)),
            func.sum(case((DownloadJob.status == "failed", 1), else_=0)),
            func.avg(case((DownloadJob.status == "imported",
                           (func.julianday(DownloadJob.completed_at)
                            - func.julianday(DownloadJob.created_at)) * 86400.0), else_=None)),
        ).where(DownloadJob.completed_at.is_not(None), DownloadJob.completed_at >= cutoff30)
    ).one()
    imp, fail = int(imp or 0), int(fail or 0)
    titles = db.scalar(select(func.count()).where(Work.hooked.is_(True))) or 0
    series = _daily_acquisitions(db, 14)
    growth = stats_library_growth(days=14, db=db)["days"]
    return {
        "downloaded_30d": imp,
        "success_rate": round(imp / (imp + fail), 3) if (imp + fail) else None,
        "avg_acquire_s": round(float(acq), 1) if acq is not None else None,
        "titles_in_library": int(titles),
        "spark": {
            "downloaded": [d["imported"] for d in series],
            "success": [round(d["imported"] / (d["imported"] + d["failed"]), 3)
                        if (d["imported"] + d["failed"]) else 0 for d in series],
            "acquire_s": [d["acquire_s"] or 0 for d in series],
            "titles": [d["total"] for d in growth],
        },
    }


@router.get("/stats/vt-usage", dependencies=[Depends(require_admin)])
def stats_vt_usage(hours: int = Query(720, ge=1, le=8760), db: Session = Depends(get_db)) -> dict:
    """VirusTotal request usage (the Insights / Integrations VT panel) — wraps the per-host telemetry."""
    from .. import telemetry
    return telemetry.host_usage(db, host="virustotal.com", hours=hours)


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
        max_pages=config_store.effective("index_max_pages"),
    )


@router.put("/index/config", response_model=IndexConfigOut,
             dependencies=[Depends(require_admin)])
def put_index_config(payload: IndexConfigIn, db: Session = Depends(get_db)) -> IndexConfigOut:
    """Set the global idle-page stop threshold applied to NEW crawls."""
    return index_admin.set_index_config(db, payload.stop_after_idle_pages)


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
    return index_admin.update_site(db, site_id, payload.model_dump(exclude_unset=True))


@router.post("/index/sites", response_model=IndexSiteOut,
             dependencies=[Depends(require_admin)])
def add_site(payload: IndexSiteIn, db: Session = Depends(get_db)) -> IndexSiteOut:
    return index_admin.add_site(db, payload)


@router.post("/index/sites/{site_id}/pause", response_model=IndexSiteOut,
             dependencies=[Depends(require_admin)])
def pause_site(site_id: int, db: Session = Depends(get_db)) -> IndexSiteOut:
    return index_admin.pause_site(db, site_id)


@router.post("/index/sites/{site_id}/resume", response_model=IndexSiteOut,
             dependencies=[Depends(require_admin)])
def resume_site(site_id: int, db: Session = Depends(get_db)) -> IndexSiteOut:
    return index_admin.resume_site(db, site_id)


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
    return index_admin.delete_site(db, site_id, purge=purge)


# ---------------------------------------------------------------------- pages
@router.get("/index/pages", response_model=list[IndexedPageOut], dependencies=[_INDEX_VIEW])
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


@router.get("/index/pages/{page_id}", response_model=IndexedPageDetailOut, dependencies=[_INDEX_VIEW])
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


@router.get("/index/search", response_model=list[IndexSearchOut], dependencies=[_INDEX_VIEW])
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
@router.get("/catalog", response_model=list[CatalogGroupOut], dependencies=[_INDEX_VIEW])
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
        cache.clear_catalog()  # integration results may have changed the catalog

    base: list[dict] | None = None if (live or resolved) else cached
    if base is None:
        def _group() -> list[dict]:
            # A browse (no query) genuinely wants the most-popular slice, so the 2000-row
            # popularity cap is right. But a SEARCH must not drop low-popularity matches just because
            # 2000 more-popular OTHER titles exist — the q filter already narrows the set, so widen
            # the candidate ceiling for filtered queries so obscure matches still surface (P2).
            cand_limit = _SEARCH_CANDIDATE_LIMIT if (q and q.strip()) else 2000
            rows = catalog.find_rows(db, q=q, site_id=site_id, hooked=hooked, limit=cand_limit)
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
    # Hide 18+ groups unless the viewer opted into that content for the group's category.
    adult_cats = set(catalog.effective_adult_categories(db, user))
    groups = [g for g in groups
              if not g.get("is_adult")
              or catalog.media_category(g.get("media_label", "")) in adult_cats]
    # Hide 'secondary' entries — study guides, summaries, workbooks, unofficial doujinshi/spin-offs —
    # that merely share a real work's title and otherwise flood search with confusing near-duplicates.
    from ..ingestion.catalog_junk import is_secondary_work
    groups = [g for g in groups if not is_secondary_work(g.get("title"))]
    if hide_books:
        # Drop groups whose ONLY sources are pipeline-only book providers (keep mixed groups, e.g.
        # a title also available on Project Gutenberg).
        groups = [g for g in groups
                  if any(s.get("provider") not in BOOK_PROVIDERS for s in (g.get("sources") or []))]
    # Browse: collapse per-volume cards of a series into one (cuts over-cardinality). NOT for a
    # SEARCH — a query like "mistborn vol 2" is looking for a specific volume, so show every match
    # (14A presentation-layer alternative; the work-grouping + acquire flow are unchanged).
    if not (q and q.strip()):
        groups = catalog.collapse_series_cards(groups)
    # in_library / in_stock is per-viewer; the grouped set above is cached user-agnostically, so
    # stamp membership here (a hooked title the viewer doesn't own is in stock, not theirs). Audiobook
    # availability is global — stamped too, since this search path builds dicts via group_rows (NOT
    # _serialize_groups), so it would otherwise miss the audiobook fields.
    lib = _user_library_work_ids(db, user)
    audio_idx = _audiobook_index(db)
    return [CatalogGroupOut(**_with_confidence(_with_audiobook(_with_membership(g, lib), audio_idx)))
            for g in groups[offset:offset + limit]]


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


@router.post("/catalog/groups/{group_id}/refetch-cover", dependencies=[Depends(require_admin)])
async def refetch_group_cover(group_id: int, db: Session = Depends(get_db)) -> dict:
    """Manually re-fetch a comic group's cover from AniList (the 'get new cover art' button). Forced:
    overwrites even an existing cover. Covers are otherwise sticky — never auto-refetched once set."""
    return await catalog.refetch_group_cover(db, group_id)


@router.get("/catalog/facets", dependencies=[_INDEX_VIEW])
def catalog_facets(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Complete filter options (all media types + source domains) for the Index page. The media
    types are capped to the categories the user may view."""
    hide_books = _hide_pipeline_books(db)
    ckey = f"catalog-facets:{'direct' if hide_books else 'all'}"
    cached = cache.get(ckey)
    if cached is None:
        cached = catalog.catalog_facets(db, hide_books=hide_books)
        # Long TTL: filter options only change when the catalog is regrouped, and the regroup tick (plus
        # any catalog write) calls cache.clear_catalog() — so freshness rides invalidation, not a short
        # timer. This keeps the Index page off the whole-catalog scan on every visit.
        cache.put(ckey, cached, ttl=1800.0)
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
    cache.put("catalog-stats", out, ttl=1800.0)   # invalidated on regroup; the distinct-norm_key scan isn't cheap
    return out


@router.get("/catalog/audiobooks", dependencies=[_INDEX_VIEW])
def catalog_audiobooks(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    """Downloaded audiobooks (the shared operator audio pool) for the Index 'Audiobooks' lane — ONLY the
    ones with a local file. Audiobooks aren't catalog entries (they're acquired Works), so this is a
    small standalone query off Works, not part of the grouped catalog. Cheap + cached."""
    from ..models import Work
    cached = cache.get("catalog-audiobooks")
    if cached is None:
        rows = db.execute(
            select(Work.id, Work.title, Work.author, Work.cover_url)
            .where(Work.media_kind == "audio", Work.local_path.is_not(None))
            .order_by(Work.created_at.desc()).limit(200)   # show effectively all (the lane scrolls)
        ).all()
        cached = [{"work_id": r[0], "title": r[1], "author": r[2], "cover_url": r[3]} for r in rows]
        cache.put("catalog-audiobooks", cached, ttl=300.0)
    return cached


# --------------------------------------------------------------- discovery rows
_BUCKETS = ("comic", "text")
MEDIA_CATEGORIES = catalog.MEDIA_CATEGORIES  # Manga & Comics / Novel / Book (display order)
_GENRE_ROWS = 8         # marquee genre lanes per media section
_THEME_ROWS = 6         # theme lanes per media section
_MIN_CATEGORY = 8       # a tag needs at least this many titles to become a row/browse target
_ROW_ITEMS = 20         # titles shown in a (horizontally-scrolled) row


def _user_library_work_ids(db: Session, user) -> set[int]:
    """The set of work ids the user has in THEIR library — distinguishes 'in library' (the user added
    it) from 'in stock' (operator pre-fetched + hooked, available to acquire but not yet theirs)."""
    if user is None:
        return set()
    return set(db.scalars(select(LibraryItem.work_id).where(LibraryItem.user_id == user.id)).all())


def _with_membership(item: dict, lib: set[int]) -> dict:
    """Stamp per-viewer in_library / in_stock onto an already-serialized group dict from its
    hooked_work_id, WITHOUT mutating the (often shared/cached) original. in_library = the viewer
    added it to their own library; a hooked title that isn't theirs is in stock (instantly
    acquirable). Used to apply membership AFTER a user-agnostic cache."""
    wid = item.get("hooked_work_id")
    return {**item, "in_library": bool(wid and wid in lib),
            "in_stock": bool(wid and wid not in lib)}


def _audiobook_index(db: Session) -> dict[str, int]:
    """norm_title → on-disk audiobook Work id. Lets the catalog mark a title's separate 'listen' format
    as available — the audiobook is a distinct shared Work, never hooked to the ebook entry, so search /
    Discover otherwise can't tell that BOTH formats are in stock.

    MEMOIZED (~2 min): this is GLOBAL state but is consulted once per LANE while building a cold
    /catalog/rows page (~45 lanes), so recomputing the norm_title scan each call added ~1.7s to a cold
    discovery load. The catalog rows cache (30 min) already gates how fresh audiobook_in_stock is, so a
    2-min memo on the index is well within that."""
    cached = cache.get("audiobook-index")
    if cached is not None:
        return cached
    from ..ingestion.extract import norm_title
    idx: dict[str, int] = {}
    for aid, atitle in db.execute(
        select(Work.id, Work.title).where(Work.media_kind == "audio", Work.local_path.is_not(None))
    ).all():
        idx.setdefault(norm_title(atitle or ""), aid)
    cache.put("audiobook-index", idx, ttl=120.0)
    return idx


def _with_audiobook(item: dict, audio_idx: dict[str, int]) -> dict:
    """Stamp audiobook availability onto a serialized group dict (global state, not per-viewer)."""
    wid = audio_idx.get(item.get("norm_key") or "")
    return {**item, "audiobook_work_id": wid, "audiobook_in_stock": wid is not None}


def _match_confidence(sources: list | None, author: str | None) -> str:
    """Confidence that the auto-picked match is the RIGHT work. The source is chosen automatically;
    this drives the UI's 'fix match' chip. The grouping is already author-gated, so the only HONEST
    surface signal (without persisting grouping-strength) is whether we even know the author: a known
    author → 'high'; no author metadata at all → 'medium' (can't fully verify — chip offers a review).
    Author-STRING differences are deliberately NOT treated as low: they're name variants ('Mary Shelley'
    vs 'Mary Wollstonecraft Shelley'), not wrong merges, and flagging them cried wolf on real books."""
    return "high" if (author or "").strip() else "medium"


def _with_confidence(item: dict) -> dict:
    return {**item, "match_confidence": _match_confidence(item.get("sources"), item.get("author"))}


def _serialize_groups(db: Session, groups: list, lib_work_ids: set[int] | None = None) -> list[dict]:
    """CatalogGroup rows → CatalogGroupOut dicts, with each group's selectable sources resolved
    from its member catalog rows in ONE batched query (no N+1). ``lib_work_ids`` (the caller's own
    library) marks each hooked title in_library vs merely in stock."""
    from collections import defaultdict

    from ..ingestion.catalog_junk import is_secondary_work
    # Drop 'secondary' entries (study guides / summaries / unofficial doujinshi) from the discovery
    # lanes + browse, the same way search does — they only share a real title and confuse the user.
    groups = [g for g in groups if not is_secondary_work(g.title)]
    if not groups:
        return []
    lib = lib_work_ids or set()
    ids = [g.id for g in groups]
    members = db.scalars(
        select(CatalogWork).where(CatalogWork.group_id.in_(ids))
    ).all()
    by_group: dict[int, list[CatalogWork]] = defaultdict(list)
    for m in members:
        by_group[m.group_id].append(m)
    # Audiobook availability: the "listen" format is a SEPARATE shared audio Work matched by normalized
    # title (never hooked to the ebook entry), so each group reports whether a downloaded audiobook exists.
    audio_by_norm = _audiobook_index(db)
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
            "media_kind": g.media_bucket, "media_label": g.media_label,
            "media_category": catalog.media_category(g.media_label), "chapters": g.chapters,
            "is_adult": bool(g.is_adult), "hooked_work_id": g.hooked_work_id,
            # in_library = the current user added it; a hooked title NOT in their library is in stock
            # (operator pre-fetched, instantly acquirable).
            "in_library": bool(g.hooked_work_id and g.hooked_work_id in lib),
            "in_stock": bool(g.hooked_work_id and g.hooked_work_id not in lib),
            "audiobook_work_id": audio_by_norm.get(g.norm_key or ""),
            "audiobook_in_stock": (g.norm_key or "") in audio_by_norm,
            "match_confidence": _match_confidence(sources, g.author),
            "sources": sources,
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


def _adult_labels(adult_cats) -> set[str]:
    """The fine media labels in which 18+ content is visible to the viewer (from their categories)."""
    return {lab for c in (adult_cats or []) for lab in catalog.category_labels(c)}


def _sorted_groups_query(*, dimension: str | None, value: str | None,
                         media: str | None, sort: str, direct_only: bool = False,
                         adult_cats=None):
    """Build the base SELECT for browsing groups by an optional (kind, slug) tag + media CATEGORY
    (Manga & Comics / Novel / Book), sorted. The category filter expands to its fine media labels
    (comics → Manga/Manhua/Webtoon/Comic). With ``direct_only`` it excludes groups whose only
    sources are pipeline-only book providers. ``adult_cats`` are the categories where the viewer may
    see 18+ content — a group flagged ``is_adult`` is hidden unless its category is among them."""
    from ..models import CatalogGroup, CatalogTag
    sel = select(CatalogGroup)
    if dimension in ("genre", "theme") and value:
        sel = sel.join(CatalogTag, CatalogTag.group_id == CatalogGroup.id).where(
            CatalogTag.kind == dimension, CatalogTag.slug == value
        )
    if media in MEDIA_CATEGORIES:
        sel = sel.where(CatalogGroup.media_label.in_(catalog.category_labels(media)))
    # 18+ gate: keep a group only if it isn't adult, or its media is one the viewer opted into.
    al = _adult_labels(adult_cats)
    if al:
        sel = sel.where((CatalogGroup.is_adult.is_(False)) | (CatalogGroup.media_label.in_(al)))
    else:
        sel = sel.where(CatalogGroup.is_adult.is_(False))
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


@router.get("/catalog/rows", response_model=list[CatalogRowOut], dependencies=[_INDEX_VIEW])
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
    adult_cats = catalog.effective_adult_categories(db, user)  # 18+ categories the viewer opted into
    adult_set = set(adult_cats)
    direct = _hide_pipeline_books(db)  # pipeline-only book items hidden when no Prowlarr+SABnzbd
    # Adult visibility is per-viewer, so it's part of the cache key (the common 'no 18+' case shares
    # one entry); the per-user allow-list is still applied after the cache.
    adultsig = ",".join(sorted(adult_cats)) or "none"
    ckey = f"catalog-rows:{media or 'all'}:{'direct' if direct else 'all'}:adult={adultsig}"
    # in_library vs in_stock is per-viewer, so it's applied AFTER the (shared) cache — never baked
    # into it — without mutating the cached item dicts (new item dicts per response).
    lib = _user_library_work_ids(db, user)

    def _finalize(src: list[dict]) -> list[dict]:
        return [{**r, "items": [_with_membership(it, lib) for it in (r.get("items") or [])]}
                for r in src if r["media_category"] in allowed]

    cached = cache.get(ckey)
    if cached is not None:
        return _finalize(cached)
    # One section per media CATEGORY (Manga & Comics / Novel / Book) — the comic subtypes share a
    # section. The frontend shows only the categories the user enabled; the server returns them all.
    cats_to_show = [media] if media in MEDIA_CATEGORIES else list(MEDIA_CATEGORIES)
    rows: list[dict] = []
    for category in cats_to_show:
        labels = catalog.category_labels(category)
        adult_here = category in adult_set  # may this viewer see 18+ titles in this category?
        # Most Popular lane (source-diversity-capped) — works even before any genre enrichment.
        pop = db.scalars(
            _sorted_groups_query(dimension=None, value=None, media=category, sort="popularity",
                                 direct_only=direct, adult_cats=adult_cats)
            .limit(_ROW_ITEMS * 4)
        ).all()
        pop = _diversity_cap(pop, _ROW_ITEMS)
        if pop:
            count_sel = select(func.count(CatalogGroup.id)).where(
                CatalogGroup.media_label.in_(labels))
            if not adult_here:
                count_sel = count_sel.where(CatalogGroup.is_adult.is_(False))
            if direct:
                count_sel = count_sel.where(_has_direct_source())
            total = db.scalar(count_sel) or 0
            rows.append({"kind": "popular", "slug": "", "label": "Most Popular",
                         "media_category": category, "count": int(total),
                         "items": _serialize_groups(db, pop)})
        # Genre then theme lanes — the most populous categories in this media category. The same
        # genre across comic subtypes (Action under Manga + Webtoon) is summed into ONE lane.
        for kind, cap in (("genre", _GENRE_ROWS), ("theme", _THEME_ROWS)):
            cnt = func.sum(CatalogCategory.group_count).label("cnt")
            cats = db.execute(
                select(CatalogCategory.slug, func.min(CatalogCategory.label), cnt)
                .where(CatalogCategory.kind == kind, CatalogCategory.media_label.in_(labels))
                .group_by(CatalogCategory.slug)
                .having(cnt >= _MIN_CATEGORY)
                .order_by(cnt.desc()).limit(cap)
            ).all()
            for slug, clabel, count in cats:
                if not adult_here and catalog.is_adult_genre(slug):
                    continue  # don't surface an explicit-18+ genre lane to a viewer who opted out
                items = db.scalars(
                    _sorted_groups_query(dimension=kind, value=slug, media=category,
                                         sort="popularity", direct_only=direct,
                                         adult_cats=adult_cats).limit(_ROW_ITEMS)
                ).all()
                if items:
                    rows.append({"kind": kind, "slug": slug, "label": clabel,
                                 "media_category": category, "count": int(count),
                                 "items": _serialize_groups(db, items)})
    cache.put(ckey, rows, ttl=1800.0)   # invalidated on regroup/catalog writes — no per-visit recompute
    return _finalize(rows)


@router.get("/catalog/categories", dependencies=[_INDEX_VIEW])
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
        # Roll the per-fine-label category rows up to their media CATEGORY (comic subtypes summed
        # into one), so the browse nav offers one "Action" per category, not one per subtype.
        mc = case(
            (CatalogCategory.media_label.in_(catalog._COMIC_LABELS), catalog.COMICS_CATEGORY),
            else_=CatalogCategory.media_label,
        ).label("media_category")
        cnt = func.sum(CatalogCategory.group_count).label("cnt")
        sel = select(CatalogCategory.kind, CatalogCategory.slug, func.min(CatalogCategory.label), mc, cnt)
        if media in MEDIA_CATEGORIES:
            sel = sel.where(CatalogCategory.media_label.in_(catalog.category_labels(media)))
        if hide_books:
            sel = sel.where(CatalogCategory.media_label != "Book")
        sel = (sel.group_by(CatalogCategory.kind, CatalogCategory.slug, mc)
               .having(cnt >= _MIN_CATEGORY).order_by(cnt.desc()))
        cached = [{"kind": k, "slug": s, "label": lab, "media_category": mcat, "count": int(c)}
                  for (k, s, lab, mcat, c) in db.execute(sel).all()]
        cache.put(ckey, cached, ttl=120.0)
    allowed = set(catalog.effective_categories(db, user))
    adult_cats = set(catalog.effective_adult_categories(db, user))

    def _visible(c: dict) -> bool:
        if c["media_category"] not in allowed:
            return False
        # Hide an explicit-18+ genre lane (Smut, Hentai, …) unless the viewer opted into 18+ here.
        if catalog.is_adult_genre(c["slug"]) and c["media_category"] not in adult_cats:
            return False
        return True

    # in_library vs in_stock is per-viewer, so it's applied AFTER the (shared) cache — without
    # mutating the cached dicts (new item dicts per response).
    lib = _user_library_work_ids(db, user)

    def _row(c: dict) -> dict:
        if not c.get("items"):
            return c
        return {**c, "items": [_with_membership(it, lib) for it in c["items"]]}

    return {"categories": [_row(c) for c in cached if _visible(c)]}


@router.get("/catalog/browse", response_model=list[CatalogGroupOut], dependencies=[_INDEX_VIEW])
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
    sel = _sorted_groups_query(dimension=dim, value=value, media=media, sort=sort,
                               direct_only=_hide_pipeline_books(db),
                               adult_cats=catalog.effective_adult_categories(db, user))
    if not media:  # the "all" browse must still honour the user's category cap (expand to labels)
        allowed_labels = [lab for c in allowed for lab in catalog.category_labels(c)]
        sel = sel.where(CatalogGroup.media_label.in_(allowed_labels))
    groups = db.scalars(sel.limit(limit).offset(offset)).all()
    return _serialize_groups(db, groups, _user_library_work_ids(db, user))


@router.post("/catalog/{catalog_id}/grab", response_model=GrabOut, dependencies=[_INDEX_ACQUIRE])
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
        log.warning("grab_external failed for catalog %s: %s", catalog_id,
                    str(exc).replace("\n", " ").replace("\r", " "))  # strip CR/LF (log-forging)
        raise HTTPException(502, "could not grab this title from the configured integration") from exc
    name = info["integration"]
    return GrabOut(
        ok=True, integration=name,
        message=f"Queued in {name}. It will appear in your library once downloaded "
                "into a watched folder.",
    )


# Coarse stage-based progress (no live SAB %): the Sources "Active jobs" bar fills by lifecycle stage.
_JOB_PERCENT = {"queued": 5, "deferred": 5, "retry": 10, "downloading": 50, "completed": 90,
                "imported": 100, "failed": 100}


def _job_out(j: DownloadJob) -> DownloadJobOut:
    # "verifying" = SAB finished (completed) but the content/VirusTotal gate hasn't passed yet.
    verifying = j.status == "completed" and not j.verified
    return DownloadJobOut(
        id=j.id, catalog_work_id=j.catalog_work_id, title=j.title, release_title=j.release_title,
        indexer=j.indexer, size=j.size, fmt=j.fmt, status=j.status,
        verifying=verifying, percent=_JOB_PERCENT.get(j.status, 0),
        grab_kind=j.grab_kind,
        work_id=j.work_id, error=j.error, not_before=j.not_before, created_at=j.created_at,
        updated_at=j.updated_at, completed_at=j.completed_at,
    )


@router.post("/catalog/{catalog_id}/grab-pipeline", response_model=DownloadJobOut, dependencies=[_INDEX_ACQUIRE])
async def grab_pipeline(
    catalog_id: int, guid: str | None = Query(None, description="Grab this specific release"),
    fuzz: bool = Query(False, description="Book-fuzz: try every match (low-confidence too) and "
                                         "content-verify each"),
    shelf_id: int | None = Query(None, description="Place the imported book on this bookshelf"),
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
    shelf_id = validate_shelf(db, user.id, shelf_id)

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
        job = await downloads.grab_release(db, cw, candidates=candidates, user_id=user.id,
                                           shelf_id=shelf_id, kind=kind)
    except IntegrationError as exc:
        log.warning("grab_release failed: %s", exc)
        raise HTTPException(400, "could not start the download for this release") from exc
    return _job_out(job)


@router.get("/catalog/{catalog_id}/series", response_model=SeriesOut)
async def catalog_series(catalog_id: int, db: Session = Depends(get_db)) -> SeriesOut:
    """Detect this book's series and list its volumes (ordered) — for 'fetch the whole series'."""
    cw = db.get(CatalogWork, catalog_id)
    if cw is None:
        raise HTTPException(404, "Catalog entry not found")
    from ..ingestion import series
    return SeriesOut(**await series.detect_series(db, cw))


@router.post("/catalog/{catalog_id}/series/acquire", dependencies=[_INDEX_ACQUIRE])
async def acquire_series_ep(
    catalog_id: int, payload: SeriesAcquireIn,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> dict:
    """Acquire the whole series (all=true, CANON-ONLY unless specials=true) or a custom selection
    (refs) via the caller's route priority. Each volume lands in the caller's library."""
    return await catalog.acquire_series(
        db, catalog_id, user=user, want_all=payload.all, refs=payload.refs,
        shelf_id=payload.shelf_id, include_specials=payload.specials,
    )


@router.get("/catalog/{catalog_id}/author", response_model=AuthorBooksOut)
async def catalog_author(catalog_id: int, db: Session = Depends(get_db)) -> AuthorBooksOut:
    """List this title's author's books (ordered) — for 'request all by {author}'. ``count`` is the
    FULL roster so the UI confirm is honest even though the acquire is server-capped."""
    return AuthorBooksOut(**await catalog.enumerate_author(db, catalog_id))


@router.post("/catalog/{catalog_id}/author/acquire", dependencies=[_INDEX_ACQUIRE])
async def acquire_author_ep(
    catalog_id: int, payload: AuthorAcquireIn,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> dict:
    """Acquire every (not-owned) book by this title's author (all=true), or a custom selection (refs),
    via the caller's route priority. Server-capped at SERIES_ACQUIRE_CAP."""
    return await catalog.acquire_author(
        db, catalog_id, user=user, want_all=payload.all, refs=payload.refs,
        shelf_id=payload.shelf_id,
    )


@router.get("/downloads", response_model=list[DownloadJobOut])
def list_downloads(
    status: str | None = Query(None, description="filter: a status, or 'active' / 'finished'"),
    limit: int = Query(200, ge=1, le=1000),
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> list[DownloadJobOut]:
    """Acquisition (fetch) jobs — the caller's own (admins see all), newest first, optionally filtered
    by status so the UI can browse just the active ones or just the failures."""
    sel = select(DownloadJob).order_by(DownloadJob.created_at.desc())
    if user.role != "admin":
        sel = sel.where(DownloadJob.user_id == user.id)
    if status == "active":
        sel = sel.where(DownloadJob.status.in_(("queued", "searching", "downloading", "retry", "deferred")))
    elif status == "finished":
        sel = sel.where(DownloadJob.status.in_(("imported", "failed")))
    elif status:
        sel = sel.where(DownloadJob.status == status)
    return [_job_out(j) for j in db.scalars(sel.limit(limit)).all()]


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


@router.post("/catalog/{catalog_id}/acquire", dependencies=[_INDEX_ACQUIRE])
async def acquire_catalog(
    catalog_id: int, route: str | None = Query(None, description="Force a specific route"),
    shelf_id: int | None = Query(None, description="Place the result on this bookshelf"),
    variant: str = Query("ebook", description="ebook | audiobook | both"),
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> dict:
    """Acquire a catalog work via the caller's route priority (or a forced ``route``): hook a web
    source, grab via a connected manager, or download through the usenet pipeline — whichever the
    priority resolves to first. ``variant`` fetches the ebook, the audiobook (audio categories, on the
    separate audiobook path), or BOTH. The result lands in the caller's library (+ ``shelf_id``)."""
    return await catalog.acquire_catalog(db, catalog_id, user=user, route=route, shelf_id=shelf_id,
                                         variant=variant)


@router.post("/catalog/{catalog_id}/hook", response_model=WorkOut, dependencies=[_INDEX_HOOK])
async def hook_catalog(
    catalog_id: int,
    start_chapter: int = Query(1, ge=1, description="Hook from this chapter (skip earlier ones)"),
    shelf_id: int | None = Query(None, description="Place the hooked work on this bookshelf"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Work:
    """Add a discovered work to the caller's library. If it's already hooked (by anyone), just add
    membership and surface it — no re-crawl, no new jobs. Otherwise pull it via the adaptive web
    adapter and self-diagnose completeness; the Work + crawl are shared across users.

    ``start_chapter`` lets a fresh hook begin partway in (skip chapters already read elsewhere); it
    only applies the first time a work is hooked, not when joining an already-hooked shared Work."""
    return await catalog.acquire_via_hook(
        db, catalog_id, user=user, start_chapter=start_chapter, shelf_id=shelf_id,
    )


# --------------------------------------------------------------- remove + block
@router.delete("/catalog/{catalog_id}", dependencies=[Depends(require_admin)])
def remove_catalog(
    catalog_id: int,
    block: bool = Query(True, description="also bar this content from being re-added"),
    block_domain: bool = Query(False, description="block the whole domain, not just this URL"),
    db: Session = Depends(get_db),
) -> dict:
    """Remove broken/unwanted content from the index. By default it's also blocked so a later
    crawl won't re-discover it. The hooked library copy (if any) is left untouched."""
    return catalog.remove_catalog(db, catalog_id, block=block, block_domain=block_domain)


@router.post("/catalog/purge-broken", dependencies=[Depends(require_admin)])
def purge_broken(
    block: bool = Query(True),
    db: Session = Depends(get_db),
) -> dict:
    """Bulk-remove every crawled (web_index) catalog entry whose diagnosed health is broken
    and that hasn't been hooked into the library. Each removed URL is blocked when block=True."""
    return catalog.purge_broken(db, block=block)


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
@router.post("/index/pages/{page_id}/hook", response_model=WorkOut, dependencies=[_INDEX_HOOK])
def hook_page(
    page_id: int,
    shelf_id: int | None = Query(None, description="Place the hooked work on this bookshelf"),
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> Work:
    p = db.get(IndexedPage, page_id)
    if p is None:
        raise HTTPException(404, "Page not found")
    if p.status != "fetched" or not p.html:
        raise HTTPException(409, "Page has no fetched content yet.")
    shelf_id = validate_shelf(db, user.id, shelf_id)
    if p.hooked_work_id is not None:  # already hooked → membership only, no re-store
        work = db.get(Work, p.hooked_work_id)
        if work is not None:
            add_to_library(db, user.id, work.id, shelf_id=shelf_id)
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
    add_to_library(db, user.id, work.id, shelf_id=shelf_id)
    return work


@router.post("/index/sites/{site_id}/hook", response_model=WorkOut, dependencies=[_INDEX_HOOK])
def hook_site(
    site_id: int,
    shelf_id: int | None = Query(None, description="Place the hooked work on this bookshelf"),
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> Work:
    """Add every fetched page of a site to the caller's library as one multi-chapter work."""
    site = db.get(IndexSite, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    shelf_id = validate_shelf(db, user.id, shelf_id)
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
    add_to_library(db, user.id, work.id, shelf_id=shelf_id)
    return work
