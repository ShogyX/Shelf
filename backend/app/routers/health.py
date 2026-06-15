import logging
import os
import shutil

from fastapi import APIRouter, Response
from sqlalchemy import text

from ..config import get_settings
from ..db import engine
from ..media import media_dir

router = APIRouter()
log = logging.getLogger("shelf.health")

# Below this free space on the DB/media volume the instance is degraded — SQLite write bursts
# (and the WAL) need headroom, and a full disk is the failure mode this probe exists to catch.
_MIN_FREE_BYTES = 256 * 1024 * 1024


def probe() -> dict:
    """Compute the health dict (no HTTP). Reused by the /health endpoint and the monitor tick.
    ``status`` is 'ok' or 'degraded'; on degraded a 'db'/'disk' key explains why."""
    settings = get_settings()
    out: dict = {"status": "ok", "app": settings.app_name}

    # DB liveness — a short, bounded SELECT 1. A locked/wedged multi-GB SQLite (the failure mode
    # this codebase fights) fails here instead of falsely reporting healthy.
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA busy_timeout=2000")
            conn.execute(text("SELECT 1"))
        out["db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        log.warning("health: DB check failed: %s", exc)
        out["status"], out["db"] = "degraded", "error"

    # Free disk on the DB/media volume.
    try:
        target = media_dir()
        os.makedirs(target, exist_ok=True)
        free = shutil.disk_usage(target).free
        out["disk_free_mb"] = round(free / (1024 * 1024))
        if free < _MIN_FREE_BYTES:
            out["status"] = "degraded"
            out["disk"] = "low"
    except Exception as exc:  # noqa: BLE001
        log.warning("health: disk check failed: %s", exc)

    # WAL size (informational): a ballooning -wal collapses write throughput.
    try:
        db_path = engine.url.database
        if db_path and os.path.exists(db_path + "-wal"):
            out["wal_mb"] = round(os.path.getsize(db_path + "-wal") / (1024 * 1024))
    except Exception:  # noqa: BLE001
        pass
    return out


@router.get("/health")
def health(response: Response) -> dict:
    """Readiness probe: the install script + systemd gate on this, so it must reflect REAL health.
    Checks the DB answers a query, the DB/media volume has free space, and reports the WAL size
    (the balloon-under-crawl failure mode). Returns 503 when not ready so Restart=always can act."""
    out = probe()
    if out["status"] != "ok":
        response.status_code = 503
    return out
