"""Integration sync: pull a connected library into the catalog, copy its metadata, and
auto-map its download root folders as watched folders so pulled files import."""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Integration, WatchedFolder
from .base import IntegrationError, client_for, is_pipeline_kind

log = logging.getLogger("shelf.integrations.sync")


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def discover_root_folders(db: Session, integration: Integration, client) -> list[str]:
    """Read the service's root folders; adopt the first as the default if unset."""
    try:
        rfs = await client.root_folders()
    except IntegrationError as exc:
        log.info("root folders unavailable for %s: %s", integration.kind, exc)
        return []
    paths = [rf.path for rf in rfs if rf.path]
    if paths and not integration.root_folder:
        integration.root_folder = paths[0]
        db.commit()
    return paths


def _map_one_folder(db: Session, integration: Integration, path: str) -> bool:
    """Create + initial-sync a watched folder for a root path Shelf can actually read.
    Returns True if a new folder was mapped. Silently skips paths Shelf can't see (the
    integration may run in another container) — manual mapping stays available."""
    from ..ingestion.local_folder import sync_folder
    from ..ingestion.watcher import manager

    if not path or not os.path.isdir(path):
        return False
    if db.scalar(select(WatchedFolder).where(WatchedFolder.path == path)):
        return False
    folder = WatchedFolder(
        path=path, display_name=f"{integration.name} ({integration.kind})",
        recursive=True, enabled=True,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    try:
        sync_folder(db, folder)
        manager.add(folder.id, folder.path, folder.recursive)
    except Exception:  # noqa: BLE001 — mapping is best-effort
        log.exception("initial sync failed for mapped folder %s", path)
    return True


async def sync_integration(db: Session, integration: Integration) -> dict:
    """Pull the integration's library into the catalog + (optionally) map its folders."""
    summary = {"library": 0, "errors": 0, "folders_mapped": 0, "error": None}
    client = client_for(integration)
    try:
        works = await client.list_library()
    except IntegrationError as exc:
        integration.last_error = str(exc)
        db.commit()
        summary["error"] = str(exc)
        return summary

    from ..ingestion import catalog

    for ext in works:
        try:
            if catalog.upsert_external(db, integration, ext) is not None:
                summary["library"] += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            summary["errors"] += 1

    if integration.auto_map_folders:
        for path in await discover_root_folders(db, integration, client):
            try:
                if _map_one_folder(db, integration, path):
                    summary["folders_mapped"] += 1
            except Exception:  # noqa: BLE001
                db.rollback()

    integration.last_sync_at = _utcnow()
    # Surface partial failures: a fresh last_sync_at with a silently-cleared error would read as
    # "all good" even when items failed to upsert.
    if summary["errors"]:
        integration.last_error = f"{summary['errors']} item(s) failed to sync"
    else:
        integration.last_error = None
    db.commit()
    log.info("synced %s: %s", integration.kind, summary)
    return summary


async def search_integrations(db: Session, term: str, *, kinds=None) -> int:
    """Live-lookup `term` across enabled integrations and merge results into the catalog.
    Returns how many external results were upserted."""
    from ..ingestion import catalog

    count = 0
    integrations = db.scalars(select(Integration).where(Integration.enabled.is_(True))).all()
    for integ in integrations:
        if kinds and integ.kind not in kinds:
            continue
        # Pipeline kinds (Prowlarr/SABnzbd) have no library lookup — skip them so we don't
        # build a client and roll back a transaction per live search for nothing.
        if is_pipeline_kind(integ.kind):
            continue
        try:
            client = client_for(integ)
            for ext in await client.lookup(term):
                if catalog.upsert_external(db, integ, ext) is not None:
                    count += 1
        except IntegrationError as exc:
            log.info("lookup failed for %s: %s", integ.kind, exc)
        except Exception:  # noqa: BLE001
            db.rollback()
    return count


async def grab_external(db: Session, entry) -> dict:
    """Ask the integration to add + search this catalog title. The file is imported later
    by the watched folder. Records grab state on the entry. Raises IntegrationError."""
    integ = db.get(Integration, entry.integration_id) if entry.integration_id else None
    if integ is None:
        raise IntegrationError("this title is not linked to a connected integration")
    if not integ.enabled:
        raise IntegrationError(f"{integ.name} is disabled")
    if is_pipeline_kind(integ.kind):
        # Pipeline kinds are driven by the matching engine + download orchestrator, not the
        # library-grab path; guard so a stray CatalogWork can't 500 on NotImplementedError.
        raise IntegrationError(f"{integ.name} ({integ.kind}) is not a library-grab target")
    client = client_for(integ)
    result = await client.grab(
        entry.extra or {},
        root_folder=integ.root_folder,
        quality_profile_id=integ.quality_profile_id,
        metadata_profile_id=integ.metadata_profile_id,
    )
    entry.extra = {**(entry.extra or {}), "grab_status": "requested", "grab_result": result}
    db.commit()
    return {"integration": integ.name, "kind": integ.kind, "result": result}


async def pipeline_status(db: Session, integration: Integration) -> dict:
    """Health/refresh for an acquisition-pipeline integration (Prowlarr/SABnzbd). They have
    no library to pull, so 'sync' just re-tests connectivity and records last_sync/last_error."""
    client = client_for(integration)
    try:
        info = await client.test_connection()
        integration.last_sync_at = _utcnow()
        integration.last_error = None
        db.commit()
        return {"ok": True, **{k: v for k, v in info.items() if v is not None}}
    except IntegrationError as exc:
        integration.last_error = str(exc)
        db.commit()
        return {"ok": False, "error": str(exc)}


async def sync_all() -> None:
    """Scheduler entrypoint: sync every enabled integration."""
    from ..db import SessionLocal

    from . import metadata as meta_mod
    from . import metadata_sync

    db = SessionLocal()
    try:
        for integ in db.scalars(
            select(Integration).where(Integration.enabled.is_(True))
        ).all():
            try:
                if integ.kind == "libgen":
                    # Open-library fallback: driven by the ingestion module, has no API client to
                    # health-check (client_for would raise 'unknown integration kind'). Nothing to sync.
                    continue
                if is_pipeline_kind(integ.kind):
                    # Search source / downloader — nothing to pull; just refresh health.
                    await pipeline_status(db, integ)
                elif meta_mod.is_metadata_kind(integ.kind):
                    # Match+enrich (ranobedb/googlebooks) or import the wishlist (goodreads), then
                    # watch for releases — recording last_sync_at/last_error on the integration.
                    await metadata_sync.sync_metadata_integration(db, integ)
                else:
                    await sync_integration(db, integ)
            except Exception:  # noqa: BLE001
                log.exception("sync failed for integration %s", integ.id)
                db.rollback()
    finally:
        db.close()
