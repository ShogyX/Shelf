"""per-title crawl policy on works

Adds works.crawl_interval_s / crawl_daily_limit / crawl_window_start /
crawl_window_end / crawl_count_today / crawl_day. Mirrors the additive boot path
(db.py _ensure_columns).

Revision ID: 0011_crawl_policy
Revises: 0010_work_tracker
Create Date: 2026-06-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_crawl_policy"
down_revision: Union[str, None] = "0010_work_tracker"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLS = (
    ("crawl_interval_s", sa.Float(), {}),
    ("crawl_daily_limit", sa.Integer(), {}),
    ("crawl_window_start", sa.Integer(), {}),
    ("crawl_window_end", sa.Integer(), {}),
    ("crawl_count_today", sa.Integer(), {"nullable": False, "server_default": "0"}),
    ("crawl_day", sa.String(10), {}),
)


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "works"):
        return
    for name, type_, kw in _COLS:
        if not _has_column(bind, "works", name):
            with op.batch_alter_table("works") as batch:
                batch.add_column(sa.Column(name, type_, nullable=kw.get("nullable", True),
                                           server_default=kw.get("server_default")))


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "works"):
        return
    for name, _type, _kw in reversed(_COLS):
        if _has_column(bind, "works", name):
            with op.batch_alter_table("works") as batch:
                batch.drop_column(name)
