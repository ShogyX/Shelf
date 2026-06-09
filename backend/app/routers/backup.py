"""Operator backup & restore: download a tiered, zipped snapshot of the instance, and restore one
onto a fresh install. Admin-only (it carries every user, credential and integration key)."""
from __future__ import annotations

import logging
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import backup as backup_mod
from ..db import get_db

router = APIRouter()
log = logging.getLogger("shelf.backup")

# Single-flight guard: building a backup (especially ``full``, which reads the whole media tree) is
# heavy, and a reverse proxy / browser that retries a slow download must NOT kick off a second
# concurrent build — that retry storm is what previously saturated disk I/O and filled /tmp. At most
# one backup builds at a time; a concurrent request gets a clean 409 instead of piling on.
_BACKUP_LOCK = threading.Lock()


@router.get("/admin/backup")
def download_backup(
    level: str = Query("settings", pattern="^(settings|data|full)$"),
) -> StreamingResponse:
    """Stream a backup zip. ``level``: settings (config only) | data (whole DB) | full (+ media).

    The archive is generated on the fly and streamed straight to the response — nothing is staged on
    disk, and the first bytes flow immediately so a slow tunnel/proxy doesn't time out waiting for a
    multi-GB build to finish."""
    if not _BACKUP_LOCK.acquire(blocking=False):
        raise HTTPException(409, "A backup is already being prepared. Please wait for it to "
                                 "finish before starting another.")

    def _generate():
        try:
            yield from backup_mod.stream_archive(level)
        except Exception:  # noqa: BLE001 — mid-stream failure: log it; headers are already sent
            log.exception("backup stream failed")
            raise
        finally:
            _BACKUP_LOCK.release()

    stamp = datetime.utcnow().strftime("%Y-%m-%d")
    fname = f"shelf-backup-{level}-{stamp}.zip"
    return StreamingResponse(
        _generate(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
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
