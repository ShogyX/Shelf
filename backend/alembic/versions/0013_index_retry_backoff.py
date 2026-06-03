"""index crawl retry + adaptive site backoff

Adds indexed_pages.attempts / next_attempt_at (transient-failure retry) and
index_sites.consecutive_errors / cooldown_until (escalating per-site cooldown when
a site blocks or rate-limits the crawler). Mirrors the additive boot path
(db.py _ensure_columns), so this is a no-op on an already-booted database.

Revision ID: 0013_index_retry_backoff
Revises: 0012_integrations
Create Date: 2026-06-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_index_retry_backoff"
down_revision: Union[str, None] = "0012_integrations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLS = {
    "indexed_pages": (
        ("attempts", sa.Integer(), {"nullable": False, "server_default": "0"}),
        ("next_attempt_at", sa.DateTime(timezone=True), {}),
    ),
    "index_sites": (
        ("consecutive_errors", sa.Integer(), {"nullable": False, "server_default": "0"}),
        ("cooldown_until", sa.DateTime(timezone=True), {}),
    ),
}


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for table, cols in _COLS.items():
        if not _has_table(bind, table):
            continue
        for name, type_, kw in cols:
            if not _has_column(bind, table, name):
                with op.batch_alter_table(table) as batch:
                    batch.add_column(sa.Column(name, type_, nullable=kw.get("nullable", True),
                                               server_default=kw.get("server_default")))


def downgrade() -> None:
    bind = op.get_bind()
    for table, cols in _COLS.items():
        if not _has_table(bind, table):
            continue
        for name, _type, _kw in reversed(cols):
            if _has_column(bind, table, name):
                with op.batch_alter_table(table) as batch:
                    batch.drop_column(name)
