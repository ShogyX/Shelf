"""Library-manager integrations API (Readarr / Kapowarr).

Connect a service, test it, and sync its library into the catalog. The API key is
stored but never returned (only `has_api_key`).
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..integrations import IntegrationError, client_for, is_pipeline_kind
from ..integrations import metadata as meta_mod
from ..integrations import metadata_sync, sync as isync
from ..models import CatalogWork, Integration, User
from ..schemas import (
    IntegrationIn,
    IntegrationOut,
    IntegrationTestOut,
    IntegrationUpdate,
    ProviderCatalogOut,
)

log = logging.getLogger("shelf.integrations")
router = APIRouter()


def _to_out(db: Session, integ: Integration) -> IntegrationOut:
    from ..integrations.provider_catalog import category_for, resolve_limits

    count = db.scalar(
        select(func.count(CatalogWork.id)).where(CatalogWork.integration_id == integ.id)
    ) or 0
    is_meta = meta_mod.is_metadata_kind(integ.kind)
    is_pipe = is_pipeline_kind(integ.kind)
    if is_meta:  # metadata links, not catalog grabs, are this provider's footprint
        from ..models import MetadataLink
        count = db.scalar(
            select(func.count(MetadataLink.id)).where(MetadataLink.provider == integ.kind)
        ) or 0
    rpm, timeout = resolve_limits(integ.kind, integ.config)
    # Never return secrets stored inside config (api_key already lives outside config as a
    # write-only field). zlib_pass → a boolean flag the UI can show.
    cfg_out = dict(integ.config or {})
    _SECRET_CFG_KEYS = ("zlib_pass",)
    for k in _SECRET_CFG_KEYS:
        if k in cfg_out:
            cfg_out[k + "_set"] = bool(cfg_out.pop(k))
    return IntegrationOut(
        id=integ.id, kind=integ.kind, name=integ.name, base_url=integ.base_url,
        enabled=integ.enabled, root_folder=integ.root_folder,
        auto_map_folders=integ.auto_map_folders, config=cfg_out,
        category=category_for(integ.kind), is_metadata=is_meta,
        is_pipeline=is_pipe, has_api_key=bool(integ.api_key),
        requests_per_minute=rpm, timeout=timeout,
        last_sync_at=integ.last_sync_at, last_error=integ.last_error, catalog_count=int(count),
    )


@router.get("/integrations/catalog", response_model=list[ProviderCatalogOut])
def integration_catalog() -> list[ProviderCatalogOut]:
    """The static directory of every connectable integration — what each is, what it provides, how
    Shelf matches with it, and its default request limit. Drives the Settings provider boxes."""
    from ..integrations.provider_catalog import PROVIDER_CATALOG
    return [
        ProviderCatalogOut(
            kind=p["kind"], category=p["category"], label=p["label"], tagline=p["tagline"],
            provides=p.get("provides", []), use=p.get("use", ""), requests=p.get("requests", ""),
            matching=p.get("matching", ""), auth=p.get("auth", "none"),
            per_user=p.get("per_user", False), default_rpm=p.get("rpm", 60),
            default_timeout=p.get("timeout", 20.0),
        )
        for p in PROVIDER_CATALOG
    ]


async def _metadata_sync(db: Session, integ: Integration) -> dict:
    """Run a metadata provider's sync: search providers (ranobedb / Google Books) match+enrich
    the library and watch for new releases; Goodreads imports the wishlist as queued auto-hooks.
    Records the outcome (last_sync_at + last_error) on the integration via the shared helper."""
    return await metadata_sync.sync_metadata_integration(db, integ)


def _default_name(kind: str, base_url: str) -> str:
    host = urlparse(base_url).netloc or kind
    return f"{kind.capitalize()} ({host})"


@router.get("/integrations", response_model=list[IntegrationOut])
def list_integrations(db: Session = Depends(get_db)) -> list[IntegrationOut]:
    integs = db.scalars(select(Integration).order_by(Integration.created_at.desc())).all()
    return [_to_out(db, i) for i in integs]


@router.post("/integrations", response_model=IntegrationOut)
async def add_integration(
    payload: IntegrationIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> IntegrationOut:
    base = payload.base_url.strip().rstrip("/")
    integ = Integration(
        kind=payload.kind,
        name=(payload.name or "").strip() or _default_name(payload.kind, base or payload.kind),
        base_url=base,
        api_key=payload.api_key.strip(),
        enabled=payload.enabled,
        root_folder=(payload.root_folder or None),
        auto_map_folders=payload.auto_map_folders,
        config=payload.config or None,
        # Goodreads is per-user: the wishlist lands in the connecting user's library + their
        # goodreads_target shelf. Library-manager integrations stay operator-wide (no owner).
        user_id=(user.id if payload.kind == "goodreads" else None),
    )
    db.add(integ)
    db.commit()
    db.refresh(integ)
    # Best-effort initial sync (metadata: match/enrich or import shelf; managers: pull library;
    # pipeline: just verify connectivity — there's no library to pull).
    if integ.enabled:
        try:
            if integ.kind == "libgen":
                pass  # open-library pipeline: nothing to pull; reachability is checked via /test
            elif is_pipeline_kind(integ.kind):
                await isync.pipeline_status(db, integ)
            elif meta_mod.is_metadata_kind(integ.kind):
                await _metadata_sync(db, integ)
            else:
                await isync.sync_integration(db, integ)
        except Exception as exc:  # noqa: BLE001
            integ.last_error = str(exc)
            db.commit()
    return _to_out(db, integ)


@router.patch("/integrations/{integration_id}", response_model=IntegrationOut)
def update_integration(
    integration_id: int, payload: IntegrationUpdate, db: Session = Depends(get_db)
) -> IntegrationOut:
    integ = db.get(Integration, integration_id)
    if integ is None:
        raise HTTPException(404, "Integration not found")
    if payload.name is not None:
        integ.name = payload.name.strip() or integ.name
    if payload.base_url is not None:
        integ.base_url = payload.base_url.strip().rstrip("/")
    if payload.api_key:  # only replace when a new key is supplied
        integ.api_key = payload.api_key.strip()
    if payload.enabled is not None:
        integ.enabled = payload.enabled
    if payload.root_folder is not None:
        integ.root_folder = payload.root_folder or None
    if payload.auto_map_folders is not None:
        integ.auto_map_folders = payload.auto_map_folders
    if payload.config is not None:
        integ.config = payload.config or None
    db.commit()
    db.refresh(integ)
    return _to_out(db, integ)


@router.delete("/integrations/{integration_id}")
def delete_integration(integration_id: int, db: Session = Depends(get_db)) -> dict:
    integ = db.get(Integration, integration_id)
    if integ is None:
        raise HTTPException(404, "Integration not found")
    for cw in db.scalars(
        select(CatalogWork).where(CatalogWork.integration_id == integration_id)
    ).all():
        db.delete(cw)
    if meta_mod.is_metadata_kind(integ.kind):
        from ..models import MetadataLink
        for ml in db.scalars(
            select(MetadataLink).where(MetadataLink.provider == integ.kind)
        ).all():
            db.delete(ml)
    db.delete(integ)
    db.commit()
    return {"deleted": integration_id}


@router.post("/integrations/{integration_id}/test", response_model=IntegrationTestOut)
async def test_integration(
    integration_id: int, db: Session = Depends(get_db)
) -> IntegrationTestOut:
    integ = db.get(Integration, integration_id)
    if integ is None:
        raise HTTPException(404, "Integration not found")
    if integ.kind == "libgen":
        from ..ingestion import libgen as lg
        res = await lg.test_connection(integ)
        integ.last_error = None if res.get("ok") else res.get("error")
        db.commit()
        return IntegrationTestOut(ok=bool(res.get("ok")), app=res.get("app"),
                                  detail=res.get("detail"), error=res.get("error"))
    client = meta_mod.provider_for(integ) if meta_mod.is_metadata_kind(integ.kind) else client_for(integ)
    try:
        info = await client.test_connection()
        roots: list[str] = []
        if not meta_mod.is_metadata_kind(integ.kind):
            try:
                roots = [rf.path for rf in await client.root_folders()]
            except IntegrationError:
                roots = []
        integ.last_error = None
        db.commit()
        return IntegrationTestOut(
            ok=True, app=info.get("app"), version=info.get("version"),
            detail=info.get("detail"), root_folders=roots,
        )
    except IntegrationError as exc:
        log.warning("integration %s test_connection failed: %s", integration_id,
                    str(exc).replace("\n", " ").replace("\r", " "))  # strip CR/LF (log-forging)
        summary = f"connection failed ({type(exc).__name__})"
        integ.last_error = summary
        db.commit()
        return IntegrationTestOut(ok=False, error=summary)


@router.post("/integrations/{integration_id}/sync", response_model=dict)
async def sync_now(integration_id: int, db: Session = Depends(get_db)) -> dict:
    integ = db.get(Integration, integration_id)
    if integ is None:
        raise HTTPException(404, "Integration not found")
    if integ.kind == "libgen":
        from ..ingestion import libgen as lg
        return await lg.test_connection(integ)
    if is_pipeline_kind(integ.kind):
        return await isync.pipeline_status(db, integ)
    if meta_mod.is_metadata_kind(integ.kind):
        return await _metadata_sync(db, integ)
    return await isync.sync_integration(db, integ)
