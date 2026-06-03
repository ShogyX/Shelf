"""Metadata-provider operator API: inspect/confirm a work's provider links, surface related
titles (prequels/sequels/spin-offs), and manage the auto-hook queue.

The providers themselves (ranobedb/goodreads) are configured via the integrations API; these
endpoints expose what those links produced and let the operator act on it — confirm a link,
queue related titles for auto-hooking, or hook everything related right now.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..integrations import metadata_sync
from ..models import CatalogWork, MetadataLink, QueuedHook, Work
from ..schemas import (
    MetadataLinkOut,
    MetadataStatsOut,
    ProviderStats,
    QueuedHookOut,
    WorkRelatedOut,
)

router = APIRouter()


def _link_out(link: MetadataLink) -> MetadataLinkOut:
    return MetadataLinkOut(
        id=link.id, work_id=link.work_id, provider=link.provider, ref=link.ref,
        matched_title=link.matched_title, confidence=link.confidence, status=link.status,
        total_units=link.total_units, unit_kind=link.unit_kind,
        release_marker=link.release_marker,
        url=(link.payload or {}).get("url"),
        provider_status=(link.payload or {}).get("status"),
        last_checked_at=link.last_checked_at,
    )


def _qh_out(qh: QueuedHook) -> QueuedHookOut:
    return QueuedHookOut(
        id=qh.id, title=qh.title, author=qh.author, media_kind=qh.media_kind,
        reason=qh.reason, source=qh.source, relation=qh.relation, status=qh.status,
        related_work_id=qh.related_work_id, hooked_work_id=qh.hooked_work_id,
        detail=qh.detail, created_at=qh.created_at,
    )


@router.get("/metadata-stats", response_model=MetadataStatsOut)
def metadata_stats(db: Session = Depends(get_db)) -> MetadataStatsOut:
    """Per search-provider coverage of the hooked library: how many titles each provider matched
    (a link exists), split by confidence, and how many are still unrecognized."""
    total = db.scalar(select(func.count(Work.id)).where(Work.hooked.is_(True))) or 0
    providers: list[ProviderStats] = []
    # Goodreads is wishlist-only (no search/enrich), so it has no library-coverage metric.
    for kind in ("ranobedb", "googlebooks"):
        rows = db.execute(
            select(MetadataLink.confidence)
            .join(Work, Work.id == MetadataLink.work_id)
            .where(MetadataLink.provider == kind, Work.hooked.is_(True))
        ).all()
        matched = len(rows)
        high = sum(1 for (c,) in rows if (c or 0) >= 0.8)
        low = sum(1 for (c,) in rows if (c or 0) < 0.6)
        medium = matched - high - low
        providers.append(ProviderStats(
            provider=kind, total=total, matched=matched, unmatched=max(0, total - matched),
            high_confidence=high, medium_confidence=medium, low_confidence=low,
            match_ratio=round(matched / total, 3) if total else 0.0,
        ))
    return MetadataStatsOut(total_library_works=total, providers=providers)


@router.get("/works/{work_id}/metadata", response_model=list[MetadataLinkOut])
def work_links(work_id: int, db: Session = Depends(get_db)) -> list[MetadataLinkOut]:
    links = db.scalars(select(MetadataLink).where(MetadataLink.work_id == work_id)).all()
    return [_link_out(link) for link in links]


@router.get("/works/{work_id}/related", response_model=WorkRelatedOut)
def work_related(work_id: int, db: Session = Depends(get_db)) -> WorkRelatedOut:
    """Related titles (prequel/sequel/side-story/…) surfaced by this work's metadata links,
    each flagged with whether it's already in the library / already queued."""
    if db.get(Work, work_id) is None:
        raise HTTPException(404, "Work not found")
    links = db.scalars(select(MetadataLink).where(MetadataLink.work_id == work_id)).all()
    items: list[dict] = []
    seen: set[str] = set()
    for link in links:
        for r in (link.payload or {}).get("related", []) or []:
            title = (r.get("title") or "").strip()
            if not title:
                continue
            from ..ingestion.extract import norm_title
            nk = norm_title(title)
            if nk in seen:
                continue
            seen.add(nk)
            queued = db.scalar(
                select(QueuedHook.status).where(
                    QueuedHook.norm_key == nk, QueuedHook.status.in_(["pending", "hooked"])
                )
            )
            in_library = db.scalar(
                select(CatalogWork.id).where(
                    CatalogWork.norm_key == nk, CatalogWork.hooked_work_id.is_not(None)
                )
            ) is not None
            items.append({
                "title": title, "relation": r.get("relation") or "related",
                "provider": link.provider, "ref": r.get("ref"),
                "queued_status": queued, "in_library": in_library,
            })
    return WorkRelatedOut(work_id=work_id, related=items)


@router.post("/works/{work_id}/queue-related", response_model=dict)
def queue_related(work_id: int, db: Session = Depends(get_db)) -> dict:
    """Queue every related title from this work's metadata links for auto-hooking once found."""
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    links = db.scalars(select(MetadataLink).where(MetadataLink.work_id == work_id)).all()
    if not links:
        raise HTTPException(400, "This work has no metadata links to read related titles from.")
    queued = sum(metadata_sync.queue_related(db, work, link) for link in links)
    return {"work_id": work_id, "queued": queued}


@router.post("/metadata-links/{link_id}/confirm", response_model=MetadataLinkOut)
def confirm_link(link_id: int, db: Session = Depends(get_db)) -> MetadataLinkOut:
    """Operator confirms an auto-match (locks it from being re-scored/downgraded)."""
    link = db.get(MetadataLink, link_id)
    if link is None:
        raise HTTPException(404, "Metadata link not found")
    link.status = "confirmed"
    link.last_checked_at = datetime.now(UTC)
    db.commit()
    db.refresh(link)
    return _link_out(link)


@router.delete("/metadata-links/{link_id}", response_model=dict)
def delete_link(link_id: int, db: Session = Depends(get_db)) -> dict:
    link = db.get(MetadataLink, link_id)
    if link is None:
        raise HTTPException(404, "Metadata link not found")
    db.delete(link)
    db.commit()
    return {"deleted": link_id}


@router.get("/queued-hooks", response_model=list[QueuedHookOut])
def list_queued_hooks(
    status: str | None = None, db: Session = Depends(get_db)
) -> list[QueuedHookOut]:
    sel = select(QueuedHook).order_by(QueuedHook.created_at.desc())
    if status:
        sel = sel.where(QueuedHook.status == status)
    return [_qh_out(qh) for qh in db.scalars(sel.limit(500)).all()]


@router.post("/queued-hooks/process", response_model=dict)
async def process_queued_hooks(db: Session = Depends(get_db)) -> dict:
    """Run the auto-hook watcher now (instead of waiting for the periodic tick)."""
    return await metadata_sync.process_queued_hooks(db)


@router.delete("/queued-hooks/{hook_id}", response_model=dict)
def delete_queued_hook(hook_id: int, db: Session = Depends(get_db)) -> dict:
    qh = db.get(QueuedHook, hook_id)
    if qh is None:
        raise HTTPException(404, "Queued hook not found")
    db.delete(qh)
    db.commit()
    return {"deleted": hook_id}
