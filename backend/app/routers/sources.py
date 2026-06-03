from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..db import get_db
from ..ingestion.base import registry
from ..ingestion.engine import get_fetcher, sync_all_sources
from ..models import Source
from ..schemas import AdapterInfoOut, SourceOut, SourceUpdate

router = APIRouter()


@router.get("/sources", response_model=list[SourceOut], dependencies=[Depends(require_admin)])
def list_sources(db: Session = Depends(get_db)) -> list[Source]:
    # Admin-only: the Sources page (compliance + rate limits) is an operator surface. Non-admins
    # discover/add titles via the catalog + Add page (which use /adapters, kept open).
    sync_all_sources(db)
    return list(db.scalars(select(Source).order_by(Source.display_name)).all())


@router.get("/adapters", response_model=list[AdapterInfoOut])
def list_adapters() -> list[AdapterInfoOut]:
    out = []
    for a in registry.all():
        c = a.compliance
        out.append(
            AdapterInfoOut(
                key=a.key,
                display_name=a.display_name,
                license_basis=c.license_basis,
                tos_permitted_default=c.tos_permitted_default,
                needs_attestation=c.needs_attestation,
                description=a.description,
                enabled=a.enabled,
            )
        )
    return out


@router.patch("/sources/{source_id}", response_model=SourceOut,
              dependencies=[Depends(require_admin)])
def update_source(source_id: int, payload: SourceUpdate, db: Session = Depends(get_db)) -> Source:
    src = db.get(Source, source_id)
    if src is None:
        raise HTTPException(404, "Source not found")
    data = payload.model_dump(exclude_unset=True)
    # A budget/interval change is an explicit "apply this and let it continue now": reset the
    # live pacing state (below) so a raised cap / shorter interval takes effect immediately and a
    # source stranded on its old spent budget resumes, rather than waiting out the old throttle.
    pacing_changed = "max_daily_requests" in data or "min_request_interval_s" in data
    for field, value in data.items():
        setattr(src, field, value)
    db.commit()
    db.refresh(src)
    # Re-sync the live fetcher budget (resetting runtime pacing when the budget/interval changed).
    get_fetcher().configure_source(
        src.key, src.min_request_interval_s, src.max_daily_requests, src.robots_respected,
        render_js=src.render_js, reset_throttle=pacing_changed,
    )
    # The index crawl is the web_index source: a budget change should let stuck crawls continue
    # immediately, without waiting for a restart. Lift any crawl cooldown (a genuine block re-arms
    # backoff on the next failed fetch) and re-queue pages that budget *pacing* previously stranded
    # as 'failed' — index_tick then self-heals the now-'done' sites and finishes them. Other
    # sources' per-title pacing is unaffected.
    if pacing_changed and src.key == "web_index":
        from sqlalchemy import update

        from .. import cache
        from ..models import IndexedPage, IndexSite

        db.execute(
            update(IndexSite)
            .where(IndexSite.cooldown_until.is_not(None))
            .values(cooldown_until=None, consecutive_errors=0)
        )
        db.execute(
            update(IndexedPage)
            .where(IndexedPage.status == "failed", IndexedPage.last_error.like("%daily budget%"))
            .values(status="pending", attempts=0, next_attempt_at=None, last_error=None)
        )
        db.commit()
        cache.clear("index")
    return src
