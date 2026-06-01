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
