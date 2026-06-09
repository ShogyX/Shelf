"""Operator backup & restore: download a tiered, zipped snapshot of the instance, and restore one
onto a fresh install. Admin-only (it carries every user, credential and integration key)."""
from __future__ import annotations

import logging
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import backup as backup_mod
from ..db import get_db
from ..schemas import RestoreCommitIn

router = APIRouter()
log = logging.getLogger("shelf.backup")

# An uploaded backup is staged once (it can be multi-GB), inspected, then committed with the
# admin's per-section choices — without re-uploading. Stages are keyed by an opaque token and swept
# if abandoned, so a cancelled restore never leaves a giant file behind.
_STAGE_PREFIX = "shelf-restore-stage-"
_STAGE_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_STAGE_MAX_AGE_S = 2 * 60 * 60  # 2h


def _stage_path(token: str) -> Path:
    if not _STAGE_TOKEN_RE.match(token or ""):
        raise HTTPException(400, "invalid restore token")
    return Path(tempfile.gettempdir()) / f"{_STAGE_PREFIX}{token}.zip"


def _sweep_stale_stages() -> None:
    cutoff = time.time() - _STAGE_MAX_AGE_S
    for p in Path(tempfile.gettempdir()).glob(f"{_STAGE_PREFIX}*.zip"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            pass

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


@router.post("/admin/restore/inspect")
async def inspect_restore(file: UploadFile, db: Session = Depends(get_db)) -> dict:
    """Stage an uploaded backup and return its restore plan (per-section row counts for the backup
    vs. the current instance) plus a token. Nothing is changed yet — the admin then picks what to
    import and calls /admin/restore/commit with the token."""
    _sweep_stale_stages()
    token = secrets.token_hex(16)
    path = _stage_path(token)
    try:
        with open(path, "wb") as out:
            while chunk := await file.read(1 << 20):  # 1 MiB chunks
                out.write(chunk)
        plan = backup_mod.restore_plan(db, path)
    except ValueError as exc:  # not a Shelf backup / unsupported schema
        path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        path.unlink(missing_ok=True)
        log.exception("backup inspect failed")
        raise HTTPException(500, f"could not read backup: {exc}") from exc
    plan["token"] = token
    return plan


@router.post("/admin/restore/commit")
def commit_restore(body: RestoreCommitIn, db: Session = Depends(get_db)) -> dict:
    """Apply a staged backup with the admin's per-section choices (skip | merge | replace).
    Excluded/skipped sections are left exactly as they are, so existing config (integrations,
    notifications, …) survives a content migration."""
    path = _stage_path(body.token)
    if not path.exists():
        raise HTTPException(404, "Upload expired or was already used — please re-select the backup.")
    try:
        result = backup_mod.import_selective(db, path, dict(body.sections))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("backup restore failed")
        raise HTTPException(500, f"restore failed: {exc}") from exc
    finally:
        path.unlink(missing_ok=True)
    # Rebuild the derived discovery-grouping caches when the catalog wasn't fully restored.
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
