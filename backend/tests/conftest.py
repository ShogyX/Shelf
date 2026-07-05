"""Point the app at a throwaway SQLite DB before any app module is imported."""
from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="shelf-test-")
# FORCE the throwaway DB/dirs — assign, never setdefault. The test fixtures wipe whole tables, so
# an inherited SHELF_DATABASE_URL (even an empty one, which setdefault would NOT override) must
# never be able to point the suite at a real database. This guarantees tests can't touch prod data.
os.environ["SHELF_DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SHELF_MEDIA_DIR"] = f"{_tmp}/media"
os.environ["SHELF_COVERS_DIR"] = f"{_tmp}/covers"
# The download-destination defaults are now env-backed (SHELF_STOCK_DIR / SHELF_AUDIOBOOK_DIR /
# SHELF_BACKUP_DIR). FORCE them for the suite so the operator's real .env can't leak prod paths in:
# empty stock/audiobook = exercise the code defaults (as before these existed); backups to the tmp dir.
os.environ["SHELF_STOCK_DIR"] = ""
os.environ["SHELF_AUDIOBOOK_DIR"] = ""
os.environ["SHELF_BACKUP_DIR"] = f"{_tmp}/backups"
os.environ["SHELF_CONTENT_LANGUAGES"] = "en"   # deterministic default; don't inherit the operator's .env
os.environ.setdefault("SHELF_SCHEDULER_ENABLED", "false")
# The DB above is a throwaway tmp path (so the destructive-op guard already permits resets), but set
# the explicit opt-in too — belt-and-suspenders so the test fixtures' table-wipes can never be gated.
os.environ["SHELF_ALLOW_DESTRUCTIVE"] = "1"


import pytest


@pytest.fixture(autouse=True)
def _clear_read_cache():
    """The read-endpoint TTL cache is process-global; clear it between tests so a cached
    result from one test's DB state can't leak into the next (the TTL hasn't expired in the
    milliseconds between calls)."""
    from app import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _reset_indexer_sweep_throttle():
    """indexer._last_done_sweep (the F18 done-site maintenance throttle) is process-global; reset it
    so each test's first index_tick runs the sweep rather than inheriting a prior test's timestamp."""
    from app.ingestion import indexer
    indexer._last_done_sweep = None
    yield


@pytest.fixture(autouse=True)
def _reset_cover_host_cache():
    """imgproxy._cover_hosts_cache (SEC-M1 allowlist) is a process-global TTL cache; reset it so one
    test's source rows can't leak into another's cover-host allowlist."""
    from app.routers import imgproxy
    imgproxy._cover_hosts_cache = (-1e9, frozenset())
    yield
