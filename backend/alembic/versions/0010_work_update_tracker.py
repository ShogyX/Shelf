"""works.last_checked_at / last_update_at (update tracker)

Mirrors the additive boot path (db.py _ensure_columns).

Revision ID: 0010_work_tracker
Revises: 0009_catalog_works
Create Date: 2026-06-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_work_tracker"
down_revision: Union[str, None] = "0009_catalog_works"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for name in ("last_checked_at", "last_update_at"):
        if _has_table(bind, "works") and not _has_column(bind, "works", name):
            with op.batch_alter_table("works") as batch:
                batch.add_column(sa.Column(name, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    for name in ("last_update_at", "last_checked_at"):
        if _has_table(bind, "works") and _has_column(bind, "works", name):
            with op.batch_alter_table("works") as batch:
                batch.drop_column(name)
