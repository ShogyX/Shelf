"""sources.config — per-source settings/credentials (e.g. J-Novel access token)

Mirrors the additive boot path (db.py: _ensure_columns), so this is a no-op on an
already-booted database.

Revision ID: 0018_source_config
Revises: 0017_bookshelf_goodreads_shelf
Create Date: 2026-06-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_source_config"
down_revision: Union[str, None] = "0017_bookshelf_goodreads_shelf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "sources") and not _has_column(bind, "sources", "config"):
        op.add_column("sources", sa.Column("config", sa.JSON()))


def downgrade() -> None:
    pass  # additive; nothing to undo
