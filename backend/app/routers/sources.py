from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..ingestion.base import registry
from ..ingestion.engine import get_fetcher, sync_all_sources
from ..models import Source
from ..schemas import AdapterInfoOut, SourceOut, SourceUpdate

router = APIRouter()


@router.get("/sources", response_model=list[SourceOut])
def list_sources(db: Session = Depends(get_db)) -> list[Source]:
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


@router.patch("/sources/{source_id}", response_model=SourceOut)
def update_source(source_id: int, payload: SourceUpdate, db: Session = Depends(get_db)) -> Source:
    src = db.get(Source, source_id)
    if src is None:
        raise HTTPException(404, "Source not found")
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(src, field, value)
    db.commit()
    db.refresh(src)
    # Re-sync the live fetcher budget.
    get_fetcher().configure_source(
        src.key, src.min_request_interval_s, src.max_daily_requests, src.robots_respected,
        render_js=src.render_js,
    )
    return src
