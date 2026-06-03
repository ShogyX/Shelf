"""bookshelves.goodreads_shelf — per-shelf external Goodreads list auto-hooked onto it

Mirrors the additive boot path (db.py: _ensure_columns), so this is a no-op on an
already-booted database.

Revision ID: 0017_bookshelf_goodreads_shelf
Revises: 0016_phase3_automation
Create Date: 2026-06-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_bookshelf_goodreads_shelf"
down_revision: Union[str, None] = "0016_phase3_automation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "bookshelves") and not _has_column(bind, "bookshelves", "goodreads_shelf"):
        op.add_column("bookshelves", sa.Column("goodreads_shelf", sa.String(128)))


def downgrade() -> None:
    pass  # additive; nothing to undo
