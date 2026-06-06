"""shelf path monitoring + send-to-email automation

Adds per-shelf path mapping/monitoring: bookshelves.watch_path + notify_email, and
watched_folders.shelf_id/user_id so a watched folder can feed a specific shelf and fire its
automation events on discovery.

Mirrors the boot-time additive path in app/db.py (_ensure_columns); both converge.

Revision ID: 0022_shelf_path_monitoring
Revises: 0021_download_jobs
Create Date: 2026-06-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_shelf_path_monitoring"
down_revision: Union[str, None] = "0021_download_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    adds = {
        "bookshelves": [
            sa.Column("notify_email", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("watch_path", sa.String(1024), nullable=True),
        ],
        "watched_folders": [
            sa.Column("shelf_id", sa.Integer(), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
        ],
    }
    for table, cols in adds.items():
        for col in cols:
            if not _has(bind, table, col.name):
                op.add_column(table, col)


def downgrade() -> None:
    for table, names in (("bookshelves", ("notify_email", "watch_path")),
                         ("watched_folders", ("shelf_id", "user_id"))):
        for n in names:
            op.drop_column(table, n)
