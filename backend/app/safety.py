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
import re
import secrets as _secrets
from contextlib import contextmanager
from contextvars import ContextVar

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


# ---------------------------------------------------------------------------------------------------
# Account hard-delete protection.
#
# Disabling a user (is_active=false) is reversible and unguarded. HARD-deleting a user row is not —
# and on several occasions a maintenance/restore script (or an agent editing the codebase) wiped
# users that were in active use. This guards EVERY path that emits a DELETE on the `users` table —
# the admin API, a bulk `delete(User)`, an ORM `db.delete(user)`, or raw `DELETE FROM users` — and
# refuses it unless the operator's secret (SHELF_USER_DELETE_SECRET) authorizes the current context.
# A naive delete therefore fails loudly instead of silently dropping accounts. Only the production DB
# is guarded (a throwaway/test DB is never gated), so test fixtures that wipe tables are unaffected.
# ---------------------------------------------------------------------------------------------------
class UserDeleteProtected(RuntimeError):
    """A DELETE on the users table was attempted without the configured delete secret."""


# Per-context "this delete is authorized" flag (contextvars → safe under threads + async).
_user_delete_ok: ContextVar[bool] = ContextVar("shelf_user_delete_ok", default=False)


def user_delete_protection_active() -> bool:
    """True when a delete secret is configured AND we're on the real (non-disposable) DB. A
    throwaway/test DB is never protected — so test fixtures that delete users (and pick up the secret
    from a loaded .env) are unaffected, consistent with require_destructive_ok()."""
    return bool((get_settings().user_delete_secret or "").strip()) and not db_is_disposable()


def verify_user_delete_secret(secret: str | None) -> bool:
    """Constant-time check of a supplied secret against SHELF_USER_DELETE_SECRET (False if unset)."""
    want = (get_settings().user_delete_secret or "").strip()
    return bool(want) and _secrets.compare_digest(str(secret or ""), want)


@contextmanager
def authorized_user_delete(secret: str | None = None, *, pending_reject: bool = False):
    """Authorize a user-row delete for the duration of the block. Requires the delete secret unless
    ``pending_reject`` (declining a never-approved signup — not an in-use account). Raises
    ``UserDeleteProtected`` on a bad/missing secret while protection is active; a no-op when it isn't."""
    if user_delete_protection_active() and not pending_reject and not verify_user_delete_secret(secret):
        raise UserDeleteProtected(
            "Hard-deleting a user requires the delete secret. Disable the account (is_active=false) "
            "instead, or supply SHELF_USER_DELETE_SECRET."
        )
    token = _user_delete_ok.set(True)
    try:
        yield
    finally:
        _user_delete_ok.reset(token)


_DELETE_USERS_RE = re.compile(r"\bdelete\b.*\bfrom\b\s+[\"'`]?users\b", re.I | re.S)


def _deletes_users(clauseelement) -> bool:
    """Cheap test (fast-rejects non-DELETEs): does this statement DELETE from the `users` table?"""
    from sqlalchemy.sql.dml import Delete
    if isinstance(clauseelement, Delete):
        return getattr(clauseelement.table, "name", None) == "users"
    txt = getattr(clauseelement, "text", None)          # raw text() clause
    return isinstance(txt, str) and bool(_DELETE_USERS_RE.search(txt))


def _before_execute(conn, clauseelement, multiparams, params, execution_options):
    # Fast path: only a DELETE against `users` is ever guarded — everything else returns immediately.
    if not _deletes_users(clauseelement):
        return
    if _user_delete_ok.get() or not user_delete_protection_active() or db_is_disposable():
        return
    raise UserDeleteProtected(
        "Refusing a DELETE on the users table (production DB). Hard-deleting users is protected — "
        "disable the account (is_active=false), or authorize it with the delete secret "
        "(admin UI / safety.authorized_user_delete(secret); set SHELF_USER_DELETE_SECRET)."
    )


def install_user_delete_guard(engine) -> None:
    """Attach the user-delete guard to an Engine (idempotent). Call once after engine creation."""
    from sqlalchemy import event
    if not event.contains(engine, "before_execute", _before_execute):
        event.listen(engine, "before_execute", _before_execute)
