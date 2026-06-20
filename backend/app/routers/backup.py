"""Operator backup & restore.

Backups are selectable objects in a store (:mod:`app.backups_store`): the app builds them there and
admins can upload externally-made ones. From the store you can download, delete, inspect a restore
plan, or restore with per-section choices. Restore is atomic (DB changes roll back on any error),
version-tolerant (column drift between builds is reconciled) and space-checked up front. Admin-only —
a backup carries every user, credential and integration key.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from .. import backup as backup_mod
from .. import backups_store as store
from ..db import get_db
from ..schemas import RestoreCommitIn

router = APIRouter()
log = logging.getLogger("shelf.backup")

# Guards the ad-hoc streaming download (GET /admin/backup) against retry storms. Store create/restore
# coordinate through store.OP_LOCK instead (they mutate the DB / walk the whole media tree).
_STREAM_LOCK = threading.Lock()


# --------------------------------------------------------------------- ad-hoc streaming download
@router.get("/admin/backup")
def download_backup(
    level: str = Query("settings", pattern="^(settings|data|full)$"),
) -> StreamingResponse:
    """Stream a freshly-built backup zip straight to the response (nothing staged on disk; first
    bytes flow immediately so a slow tunnel doesn't time out). For a backup you can manage/restore
    later, use POST /admin/backups instead, which saves it into the store."""
    if not _STREAM_LOCK.acquire(blocking=False):
        raise HTTPException(409, "A backup download is already in progress. Please wait for it to "
                                 "finish before starting another.")

    def _generate():
        try:
            yield from backup_mod.stream_archive(level)
        except Exception:  # noqa: BLE001 — mid-stream failure: headers already sent
            log.exception("backup stream failed")
            raise
        finally:
            _STREAM_LOCK.release()

    stamp = datetime.utcnow().strftime("%Y-%m-%d")
    fname = f"shelf-backup-{level}-{stamp}.zip"
    return StreamingResponse(
        _generate(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ------------------------------------------------------------------------------- backups store
@router.get("/admin/backups")
def list_backups() -> dict:
    """Every backup in the store (created + uploaded), the whole-DB snapshots, any in-progress build,
    and free space. ``db_snapshots`` are the raw shelf.db copies (pre-op safety + recovery files)
    restored by swapping the file wholesale; ``backups`` are the logical, mergeable zip archives."""
    return {
        "backups": store.list_backups(backup_mod.SCHEMA_VERSION),
        "db_snapshots": store.list_db_snapshots(),
        "free_bytes": store.free_bytes(),
        "schema_version": backup_mod.SCHEMA_VERSION,
    }


@router.post("/admin/backups/db-snapshots/{name}/restore")
def restore_db_snapshot(name: str) -> dict:
    """Restore a WHOLE-DB snapshot: stage it and restart the service, which swaps the file in at boot
    (the current DB is safety-copied first). This replaces ALL data with the snapshot — distinct from
    the per-section logical restore above. The restart drops in-flight requests by design."""
    try:
        store.request_db_restore(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    import subprocess  # detached so the restart outlives this response
    subprocess.Popen(
        ["bash", "-c", "sleep 2; systemctl restart shelf.service"],
        start_new_session=True,
    )
    log.warning("full-DB restore staged from snapshot %s — restarting service", name)
    return {"restoring": name, "status": "restarting"}


@router.delete("/admin/backups/db-snapshots/{name}")
def delete_db_snapshot(name: str) -> dict:
    """Delete a whole-DB snapshot file (these are multi-GB; the store can hold tens of them)."""
    try:
        store.delete_db_snapshot(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"deleted": name}


@router.post("/admin/backups")
def create_backup(level: str = Query("settings", pattern="^(settings|data|full)$")) -> dict:
    """Build a backup INTO the store (in the background — a full build can take minutes). Poll
    GET /admin/backups to see it finish."""
    try:
        name = store.start_build(level)
    except RuntimeError as exc:  # another op already running
        raise HTTPException(409, str(exc)) from exc
    return {"name": name, "status": "building", "level": level}


@router.post("/admin/backups/upload")
async def upload_backup(file: UploadFile) -> dict:
    """Add an externally-made backup (e.g. from another VM) to the store so it's selectable here.
    Streamed to disk in chunks and validated as a real Shelf backup before it's published."""
    name = store.sanitized_upload_name(file.filename or "backup.zip")
    final = store.safe_path(name)
    partial = final.parent / f"{name}.partial"
    try:
        with open(partial, "wb") as out:
            while chunk := await file.read(1 << 20):  # 1 MiB
                out.write(chunk)
    except OSError as exc:  # e.g. disk full
        partial.unlink(missing_ok=True)
        raise HTTPException(507, f"Could not save the upload (disk full?): {exc}") from exc
    if store.read_manifest(partial) is None:
        partial.unlink(missing_ok=True)
        raise HTTPException(400, "That file isn't a valid Shelf backup (no manifest).")
    partial.replace(final)
    return store.entry(final, schema_version=backup_mod.SCHEMA_VERSION)


@router.get("/admin/backups/{name}/download")
def download_stored(name: str) -> FileResponse:
    """Download a stored backup (streamed from disk; supports range requests for big archives)."""
    try:
        path = store.safe_path(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.exists():
        raise HTTPException(404, "backup not found")
    return FileResponse(path, media_type="application/zip", filename=name)


@router.delete("/admin/backups/{name}")
def delete_stored(name: str) -> dict:
    try:
        store.delete_backup(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"deleted": name}


@router.get("/admin/backups/{name}/plan")
def backup_plan(name: str, db: Session = Depends(get_db)) -> dict:
    """The restore plan for a stored backup: per-section row counts (backup vs. this instance) so
    the admin can choose what to import. Reads the manifest only — changes nothing."""
    try:
        path = store.safe_path(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.exists():
        raise HTTPException(404, "backup not found")
    try:
        plan = backup_mod.restore_plan(db, path)
    except ValueError as exc:  # not a Shelf backup / unsupported schema
        raise HTTPException(400, str(exc)) from exc
    plan["name"] = name
    return plan


@router.post("/admin/restore/commit")
def commit_restore(body: RestoreCommitIn, db: Session = Depends(get_db)) -> dict:
    """Restore a stored backup with the admin's per-section choices (skip | merge | replace).

    Safe by construction: the scheduler is paused so crawls don't fight the writer, the DB portion
    is one atomic transaction (any error rolls back — the DB is never left half-restored), and it
    refuses up front if the disk can't fit it."""
    try:
        path = store.safe_path(body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.exists():
        raise HTTPException(404, "backup not found")
    if not store.OP_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Another backup or restore is already running. Please wait.")

    from ..ingestion import scheduler
    paused = scheduler.pause_for_maintenance()
    try:
        result = backup_mod.import_selective(db, path, dict(body.sections))
    except backup_mod.NotEnoughSpace as exc:
        raise HTTPException(507, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — DB already rolled back inside import_selective
        log.exception("backup restore failed")
        raise HTTPException(500, "restore failed (no changes applied)") from exc
    finally:
        if paused:
            scheduler.resume_after_maintenance()
        store.OP_LOCK.release()
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
        "warnings": result.get("warnings", []),
    }
