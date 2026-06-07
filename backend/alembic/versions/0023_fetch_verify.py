"""fetch verify: broken-release registry + download candidate cascade

Adds:
  * broken_releases — dead/wrong NZBs recorded by stable identity so they are never retried.
  * download_jobs.{candidates, attempt, release_key, verified} — the candidate cascade and
    post-download content-verification bookkeeping.

Mirrors the boot-time create_all + additive-column path in app/db.py; both converge on the same
schema. Idempotent (guards on existing table / columns) so it is safe alongside create_all.

Revision ID: 0023_fetch_verify
Revises: 0022_shelf_path_monitoring
Create Date: 2026-06-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_fetch_verify"
down_revision: Union[str, None] = "0022_shelf_path_monitoring"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if "broken_releases" not in insp.get_table_names():
        op.create_table(
            "broken_releases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("release_key", sa.String(255), nullable=False),
            sa.Column("release_title", sa.String(1024), nullable=True),
            sa.Column("reason", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_broken_releases_release_key", "broken_releases",
                        ["release_key"], unique=True)

    cols = {c["name"] for c in insp.get_columns("download_jobs")}
    with op.batch_alter_table("download_jobs") as batch:
        if "candidates" not in cols:
            batch.add_column(sa.Column("candidates", sa.JSON(), nullable=True))
        if "attempt" not in cols:
            batch.add_column(sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"))
        if "release_key" not in cols:
            batch.add_column(sa.Column("release_key", sa.String(255), nullable=True))
        if "verified" not in cols:
            batch.add_column(sa.Column("verified", sa.Boolean(), nullable=False,
                                       server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("download_jobs") as batch:
        for col in ("verified", "release_key", "attempt", "candidates"):
            try:
                batch.drop_column(col)
            except Exception:  # noqa: BLE001
                pass
    op.drop_table("broken_releases")
