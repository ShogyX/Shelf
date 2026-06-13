"""request telemetry: add per-request OUTCOME (success/blocked/timeout/error)

request_stats gains an ``outcome`` dimension and its unique key becomes
(bucket, host, category, outcome). The table is ephemeral telemetry (continuously rebuilt, pruned to
30 days), so the migration simply DROPS and recreates it with the new shape rather than doing a
fragile SQLite unique-constraint rebuild — losing at most a few hours of counts.

Revision ID: 0027_request_stats_outcome
Revises: 0026_request_stats
Create Date: 2026-06-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027_request_stats_outcome"
down_revision: Union[str, None] = "0026_request_stats"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create(insp) -> None:
    op.create_table(
        "request_stats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bucket", sa.String(length=16), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False, server_default="success"),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("bucket", "host", "category", "outcome",
                            name="uq_reqstat_bucket_host_cat_outcome"),
    )
    op.create_index("ix_request_stats_bucket", "request_stats", ["bucket"])
    op.create_index("ix_request_stats_host", "request_stats", ["host"])


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "request_stats" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("request_stats")}
        if "outcome" in cols:
            return  # already migrated (or created fresh with the new shape)
        op.drop_table("request_stats")
    _create(sa.inspect(bind))


def downgrade() -> None:
    try:
        op.drop_column("request_stats", "outcome")
    except Exception:  # noqa: BLE001
        pass
