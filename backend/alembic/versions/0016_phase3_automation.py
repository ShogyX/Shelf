"""phase-3 shelf automation: auto-kindle delivery cursor + per-user Goodreads owner

Adds:
  * library_items.auto_kindle_through (highest chapter index auto-sent to a member's Kindle)
  * integrations.user_id (the user a Goodreads connection belongs to → its wishlist
    auto-hooks land in that user's library + goodreads_target shelf)

Mirrors the additive boot path (db.py: _ensure_columns), so this is a no-op on an
already-booted database.

Revision ID: 0016_phase3_automation
Revises: 0015_per_user_library
Create Date: 2026-06-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_phase3_automation"
down_revision: Union[str, None] = "0015_per_user_library"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "library_items") and not _has_column(
        bind, "library_items", "auto_kindle_through"
    ):
        op.add_column("library_items", sa.Column("auto_kindle_through", sa.Integer()))
    if _has_table(bind, "integrations") and not _has_column(bind, "integrations", "user_id"):
        op.add_column("integrations", sa.Column("user_id", sa.Integer()))


def downgrade() -> None:
    pass  # additive; nothing to undo
