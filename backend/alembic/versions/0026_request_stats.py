"""request telemetry: per-host/category/hour outbound-request counts

Adds the ``request_stats`` table — one row per (UTC-hour bucket, destination host, category) with a
running count, written by the in-memory telemetry flush (app/telemetry.py) and read by the
Settings → Index request dashboard (totals, rates, trends).

Mirrors the boot-time create_all path in app/db.py (both converge on the same schema). Idempotent
(guards on the existing table) so it is safe alongside create_all.

Revision ID: 0026_request_stats
Revises: 0025_work_content_hash
Create Date: 2026-06-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026_request_stats"
down_revision: Union[str, None] = "0025_work_content_hash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "request_stats" in insp.get_table_names():
        return
    op.create_table(
        "request_stats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bucket", sa.String(length=16), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("bucket", "host", "category", name="uq_reqstat_bucket_host_cat"),
    )
    op.create_index("ix_request_stats_bucket", "request_stats", ["bucket"])
    op.create_index("ix_request_stats_host", "request_stats", ["host"])


def downgrade() -> None:
    try:
        op.drop_table("request_stats")
    except Exception:  # noqa: BLE001
        pass
