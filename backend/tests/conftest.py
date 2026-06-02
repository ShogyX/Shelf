"""Point the app at a throwaway SQLite DB before any app module is imported."""
from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="shelf-test-")
os.environ.setdefault("SHELF_DATABASE_URL", f"sqlite:///{_tmp}/test.db")
os.environ.setdefault("SHELF_SCHEDULER_ENABLED", "false")
# Keep generated media/cover artifacts out of the repo tree during tests.
os.environ.setdefault("SHELF_MEDIA_DIR", f"{_tmp}/media")
os.environ.setdefault("SHELF_COVERS_DIR", f"{_tmp}/covers")


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
