"""Production-database safety guard.

On 2026-06-18 a reset/repro run from ``backend/`` operated on the relative ``./shelf.db`` PRODUCTION
file and bulk-deleted works/catalog/integrations/a user (the test-fixture table set), leaving orphans.
The enabler: the DB URL is a relative path, ``SessionLocal()`` defaults to it, and nothing stopped a
table-level delete from hitting prod.

``require_destructive_ok()`` is the belt-and-suspenders fix: every bulk/table-level delete or reset
utility (test fixtures, maintenance scripts) calls it, so such code can only run against a clearly
throwaway DB — or when the operator has explicitly opted in. The real prevention is still the operating
rule "never run repro/reset code against the live DB"; this makes accidental violations fail loudly.
"""
from __future__ import annotations

import os

from .config import get_settings


def db_is_disposable(url: str | None = None) -> bool:
    """True only for a provably throwaway DB — an in-memory DB or a tmp/test path. Never the prod file."""
    u = (url or get_settings().database_url).lower()
    return (
        ":memory:" in u
        or "/tmp/" in u
        or "shelf-test-" in u            # tests/conftest.py mkdtemp prefix
        or u.endswith("/test.db")
    )


def require_destructive_ok(reason: str = "") -> None:
    """Raise unless we're provably NOT on production, or the operator set ``SHELF_ALLOW_DESTRUCTIVE=1``.

    Call before any bulk/table-level DELETE or whole-table reset. A scoped ORM delete that goes through
    the proper cascade (``purge_work``/``_purge_user``) does NOT need this — only blunt resets do."""
    if db_is_disposable() or os.environ.get("SHELF_ALLOW_DESTRUCTIVE") == "1":
        return
    raise RuntimeError(
        "Refusing a destructive DB operation against what looks like the PRODUCTION database "
        f"({get_settings().database_url}). {reason}\n"
        "If you really mean it, run against a tmp/test DB or set SHELF_ALLOW_DESTRUCTIVE=1."
    )
