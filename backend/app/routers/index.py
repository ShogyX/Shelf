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
    IndexedPageDetailOut,
    IndexedPageOut,
    IndexSearchOut,
    IndexSiteIn,
    IndexSiteOut,
    WorkOut,
)

router = APIRouter()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _site_out(db: Session, site: IndexSite) -> IndexSiteOut:
    rows = dict(
        db.execute(
            select(IndexedPage.status, func.count(IndexedPage.id))
            .where(IndexedPage.site_id == site.id)
            .group_by(IndexedPage.status)
        ).all()
    )
    words = (
        db.scalar(select(func.sum(IndexedPage.word_count)).where(IndexedPage.site_id == site.id))
        or 0
    )
    total = sum(rows.values())
    return IndexSiteOut(
        id=site.id, root_url=site.root_url, domain=site.domain, title=site.title,
        status=site.status, max_pages=site.max_pages, max_depth=site.max_depth,
        same_host_only=site.same_host_only, last_error=site.last_error,
        pages_total=total, pages_fetched=rows.get("fetched", 0),
        pages_pending=rows.get("pending", 0), pages_failed=rows.get("failed", 0),
        words=int(words), created_at=site.created_at,
    )


# ---------------------------------------------------------------------- sites
@router.get("/index/sites", response_model=list[IndexSiteOut])
def list_sites(db: Session = Depends(get_db)) -> list[IndexSiteOut]:
    sites = db.scalars(select(IndexSite).order_by(IndexSite.created_at.desc())).all()
    return [_site_out(db, s) for s in sites]


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
def list_catalog(
    q: str | None = Query(None, description="Search title / author / synopsis"),
    site_id: int | None = None,
    hooked: bool | None = None,
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[CatalogGroupOut]:
    """The searchable catalog of discovered works, grouped + deduped across sites so the
    same title from several sources is one card with selectable sources."""
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
