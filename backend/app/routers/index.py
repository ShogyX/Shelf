"""URL Index API — submit sites to auto-crawl, browse pages, full-text search, hook to library."""
from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import db as dbmod
from ..db import get_db, index_fts_delete
from ..ingestion import catalog
from ..ingestion.base import RawChapter, registry
from ..ingestion.engine import ComplianceError, ensure_source, store_chapter_content
from ..ingestion.indexer import start_index
from ..models import CatalogWork, Chapter, IndexedPage, IndexSite, Work
from ..schemas import (
    CatalogGroupOut,
    GrabOut,
    IndexedPageDetailOut,
    IndexedPageOut,
    IndexSearchOut,
    IndexSiteIn,
    IndexSiteOut,
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
    return IndexSiteOut(
        id=site.id, root_url=site.root_url, domain=site.domain, title=site.title,
        status=site.status, max_pages=site.max_pages, max_depth=site.max_depth,
        same_host_only=site.same_host_only, last_error=site.last_error,
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
    return [
        _build_site_out(
            s, status_by_site.get(s.id, {}), words_by_site.get(s.id) or 0,
            titles_by_site.get(s.id) or 0, last_by_site.get(s.id), now,
        )
        for s in sites
    ]


@router.get("/index/stats", response_model=IndexStatsOut)
def index_stats(db: Session = Depends(get_db)) -> IndexStatsOut:
    """Aggregate crawl observability: sites by status, pages, titles, requests, time."""
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
    return IndexStatsOut(
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


@router.post("/index/sites", response_model=IndexSiteOut)
def add_site(payload: IndexSiteIn, db: Session = Depends(get_db)) -> IndexSiteOut:
    try:
        site = start_index(
            db, payload.url,
            max_pages=payload.max_pages, max_depth=payload.max_depth,
            same_host_only=payload.same_host_only,
        )
    except ComplianceError as exc:
        raise HTTPException(403, str(exc)) from exc
    return _site_out(db, site)


@router.post("/index/sites/{site_id}/pause", response_model=IndexSiteOut)
def pause_site(site_id: int, db: Session = Depends(get_db)) -> IndexSiteOut:
    site = db.get(IndexSite, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    site.status = "paused"
    db.commit()
    return _site_out(db, site)


@router.post("/index/sites/{site_id}/resume", response_model=IndexSiteOut)
def resume_site(site_id: int, db: Session = Depends(get_db)) -> IndexSiteOut:
    site = db.get(IndexSite, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    site.status = "active"
    db.commit()
    return _site_out(db, site)


@router.delete("/index/sites/{site_id}")
def delete_site(site_id: int, db: Session = Depends(get_db)) -> dict:
    site = db.get(IndexSite, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
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
    return {"deleted": site_id}


# ---------------------------------------------------------------------- pages
@router.get("/index/pages", response_model=list[IndexedPageOut])
def list_pages(
    site_id: int | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[IndexedPageOut]:
    q = select(IndexedPage)
    if site_id is not None:
        q = q.where(IndexedPage.site_id == site_id)
    if status:
        q = q.where(IndexedPage.status == status)
    q = q.order_by(IndexedPage.depth, IndexedPage.id).limit(limit).offset(offset)
    out: list[IndexedPageOut] = []
    for p in db.scalars(q).all():
        snippet = (p.text or "")[:240].strip()
        out.append(
            IndexedPageOut(
                id=p.id, site_id=p.site_id, url=p.url, title=p.title, description=p.description,
                author=p.author, cover_url=p.cover_url, site_name=p.site_name,
                page_type=p.page_type, word_count=p.word_count, depth=p.depth, status=p.status,
                hooked_work_id=p.hooked_work_id, fetched_at=p.fetched_at,
                snippet=snippet or None,
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


@router.get("/index/search", response_model=list[IndexSearchOut])
def search(
    q: str = Query(..., min_length=1),
    site_id: int | None = None,
    limit: int = Query(30, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[IndexSearchOut]:
    if dbmod.fts_enabled:
        match = _fts_query(q)
        if not match:
            return []
        sql = (
            "SELECT p.id, p.site_id, p.url, p.title, "
            "snippet(indexed_pages_fts, 1, '<mark>', '</mark>', '…', 14) AS snip, "
            "bm25(indexed_pages_fts) AS score, p.description, p.author, p.cover_url "
            "FROM indexed_pages_fts f JOIN indexed_pages p ON p.id = f.rowid "
            "WHERE indexed_pages_fts MATCH :m "
            + ("AND p.site_id = :sid " if site_id is not None else "")
            + "ORDER BY score LIMIT :lim"
        )
        params: dict = {"m": match, "lim": limit}
        if site_id is not None:
            params["sid"] = site_id
        try:
            rows = db.execute(text(sql), params).all()
        except Exception:
            rows = []
        return [
            IndexSearchOut(
                page_id=r[0], site_id=r[1], url=r[2], title=r[3],
                snippet=r[4] or "", score=float(r[5]),
                description=r[6], author=r[7], cover_url=r[8],
            )
            for r in rows
        ]

    # Fallback: LIKE search (no FTS5 in this SQLite build).
    like = f"%{q}%"
    cond = (IndexedPage.title.like(like)) | (IndexedPage.text.like(like))
    sel = select(IndexedPage).where(IndexedPage.status == "fetched", cond)
    if site_id is not None:
        sel = sel.where(IndexedPage.site_id == site_id)
    out: list[IndexSearchOut] = []
    for p in db.scalars(sel.limit(limit)).all():
        idx = (p.text or "").lower().find(q.lower())
        start = max(0, idx - 60)
        snip = (p.text or "")[start:start + 200]
        out.append(
            IndexSearchOut(page_id=p.id, site_id=p.site_id, url=p.url, title=p.title,
                           snippet=snip, score=0.0,
                           description=p.description, author=p.author, cover_url=p.cover_url)
        )
    return out


# -------------------------------------------------------------------- catalog
@router.get("/catalog", response_model=list[CatalogGroupOut])
async def list_catalog(
    q: str | None = Query(None, description="Search title / author / synopsis"),
    site_id: int | None = None,
    hooked: bool | None = None,
    live: bool = Query(False, description="Also live-search connected integrations"),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[CatalogGroupOut]:
    """The searchable catalog of discovered works, grouped + deduped across sources (web
    crawl + Readarr + Kapowarr) so the same title is one card with selectable sources.
    With ``live=true`` and a query, also looks the term up live in connected integrations
    and merges the results."""
    if live and q and q.strip():
        from ..integrations import sync as isync
        try:
            await isync.search_integrations(db, q.strip())
        except Exception:  # noqa: BLE001 — live lookup is best-effort
            pass
    rows = catalog.find_rows(db, q=q, site_id=site_id, hooked=hooked, limit=600)
    groups = catalog.group_rows(rows, q=q)
    return [CatalogGroupOut(**g) for g in groups[offset:offset + limit]]


@router.get("/catalog/stats")
def catalog_stats(db: Session = Depends(get_db)) -> dict:
    total = db.scalar(select(func.count(CatalogWork.id))) or 0
    hooked = db.scalar(
        select(func.count(CatalogWork.id)).where(CatalogWork.hooked_work_id.is_not(None))
    ) or 0
    sites = db.scalar(select(func.count(func.distinct(CatalogWork.site_id)))) or 0
    groups = db.scalar(select(func.count(func.distinct(CatalogWork.norm_key)))) or 0
    return {"entries": total, "titles": groups, "hooked": hooked, "sites": sites}


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
async def hook_catalog(catalog_id: int, db: Session = Depends(get_db)) -> Work:
    """Move a discovered work into the library from its chosen source, pulling chapters
    via the adaptive web adapter and self-diagnosing completeness."""
    entry = db.get(CatalogWork, catalog_id)
    if entry is None:
        raise HTTPException(404, "Catalog entry not found")
    try:
        return await catalog.hook_entry(db, entry)
    except ComplianceError as exc:
        raise HTTPException(403, str(exc)) from exc


# ----------------------------------------------------------------------- hook
@router.post("/index/pages/{page_id}/hook", response_model=WorkOut)
def hook_page(page_id: int, db: Session = Depends(get_db)) -> Work:
    p = db.get(IndexedPage, page_id)
    if p is None:
        raise HTTPException(404, "Page not found")
    if p.status != "fetched" or not p.html:
        raise HTTPException(409, "Page has no fetched content yet.")
    src = ensure_source(db, registry.get("web_index"))

    ref = f"indexpage:{p.id}"
    work = db.scalar(select(Work).where(Work.source_id == src.id, Work.source_work_ref == ref))
    if work is None:
        work = Work(source_id=src.id, source_work_ref=ref)
        db.add(work)
    work.title = p.title or p.url
    work.author = p.author
    work.description = p.description or (p.text or "")[:300]
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
    return work


@router.post("/index/sites/{site_id}/hook", response_model=WorkOut)
def hook_site(site_id: int, db: Session = Depends(get_db)) -> Work:
    """Add every fetched page of a site to the library as one multi-chapter work."""
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
    return work
