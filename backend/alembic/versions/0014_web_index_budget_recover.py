"""recover stranded web_index daily budget + budget-failed pages

Pre-existing installs kept an OLD web_index daily request budget (2000, briefly 50000) because
ensure_source only seeds a Source's budget on row CREATE. The index crawler hit that cap constantly
and (on the legacy path) marked thousands of pages permanently 'failed' with "daily budget …
exhausted". The budget is now UNLIMITED (0) — the per-source interval is the only throttle. This
resets ONLY a known old auto-default to unlimited and re-queues those budget-stranded pages so the
crawl resumes. Mirrors the additive boot path (db.py _recover_web_index_budget), so it is a no-op
on an already-booted database.

Revision ID: 0014_web_index_budget_recover
Revises: 0013_index_retry_backoff
Create Date: 2026-06-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_web_index_budget_recover"
down_revision: Union[str, None] = "0013_index_retry_backoff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mirrors WebIndexAdapter.compliance.max_daily_requests (kept literal so the migration doesn't
# import application code). 0 = unlimited (per-source interval is the only throttle).
_NEW_BUDGET = 0


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "sources"):
        # One-time normalization to the new unlimited default (migrations run once, so this
        # mirrors the sentinel-gated boot path in db._recover_web_index_budget).
        bind.execute(
            sa.text("UPDATE sources SET max_daily_requests = :new WHERE key = 'web_index'"),
            {"new": _NEW_BUDGET},
        )
    if _has_table(bind, "indexed_pages"):
        bind.execute(
            sa.text(
                "UPDATE indexed_pages SET status = 'pending', attempts = 0, "
                "next_attempt_at = NULL, last_error = NULL "
                "WHERE status = 'failed' AND last_error LIKE '%daily budget%'"
            )
        )


def downgrade() -> None:
    # Data recovery: nothing to undo (the old 'failed'/budget state isn't worth restoring).
    pass
