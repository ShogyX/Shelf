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
os.environ.setdefault("SHELF_SCHEDULER_ENABLED", "false")


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
