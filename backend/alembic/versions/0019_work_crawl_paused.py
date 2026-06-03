"""works.crawl_paused — operator-paused crawling (deleted/paused job won't auto-revive)

Mirrors the additive boot path (db.py: _ensure_columns), so this is a no-op on an
already-booted database.

Revision ID: 0019_work_crawl_paused
Revises: 0018_source_config
Create Date: 2026-06-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_work_crawl_paused"
down_revision: Union[str, None] = "0018_source_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "works") and not _has_column(bind, "works", "crawl_paused"):
        op.add_column(
            "works",
            sa.Column("crawl_paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    pass  # additive; nothing to undo
