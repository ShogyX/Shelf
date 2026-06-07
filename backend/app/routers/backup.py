"""Operator backup & restore: download a tiered, zipped snapshot of the instance, and restore one
onto a fresh install. Admin-only (it carries every user, credential and integration key)."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from .. import backup as backup_mod
from ..db import get_db

router = APIRouter()
log = logging.getLogger("shelf.backup")


@router.get("/admin/backup")
def download_backup(
    level: str = Query("settings", pattern="^(settings|data|full)$"),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Stream a backup zip. ``level``: settings (config only) | data (whole DB) | full (+ media)."""
    tmp = Path(tempfile.mkstemp(prefix=f"shelf-backup-{level}-", suffix=".zip")[1])
    try:
        manifest = backup_mod.export_archive(db, level, tmp)
    except Exception as exc:  # noqa: BLE001 — surface a clean error, clean up the temp file
        tmp.unlink(missing_ok=True)
        log.exception("backup export failed")
        raise HTTPException(500, f"backup failed: {exc}") from exc
    stamp = manifest["created_at"][:10]
    fname = f"shelf-backup-{level}-{stamp}.zip"
    # Delete the temp file once the response has been fully sent.
    return FileResponse(
        tmp, media_type="application/zip", filename=fname,
        background=BackgroundTask(lambda: tmp.unlink(missing_ok=True)),
    )


@router.post("/admin/restore")
async def restore_backup(
    file: UploadFile,
    wipe: bool = Query(False, description="Required to overwrite a non-empty instance"),
    db: Session = Depends(get_db),
) -> dict:
    """Import a backup zip. Refuses a non-empty instance unless ``wipe=true`` (which clears all
    existing data first) — a restore is meant for a FRESH install, not a merge."""
    if not backup_mod.database_is_empty(db):
        if not wipe:
            raise HTTPException(
                409, "This instance already has data. Restore is for a fresh install; pass "
                     "wipe=true to erase the current data and replace it with the backup.")
        backup_mod.wipe_database(db)
    tmp = Path(tempfile.mkstemp(prefix="shelf-restore-", suffix=".zip")[1])
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(1 << 20):  # 1 MiB chunks
                out.write(chunk)
        result = backup_mod.import_archive(db, tmp)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("backup restore failed")
        raise HTTPException(500, f"restore failed: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    # Rebuild the derived discovery-grouping caches the levels don't carry.
    try:
        from ..ingestion import catalog_groups
        catalog_groups.regroup_catalog(db)
    except Exception:  # noqa: BLE001 — regroup is best-effort; the next tick will retry
        log.exception("post-restore regroup failed")
    return {
        "restored": True,
        "level": result["manifest"].get("level"),
        "loaded": result["loaded"],
    }
