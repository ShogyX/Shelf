"""download_jobs: usenet acquisition pipeline

Adds the download_jobs table tracking a matched catalog book grabbed through the
Prowlarr→SABnzbd pipeline and imported into the library on completion.

Mirrors the boot-time create_all path in app/db.py (the table is also created by
Base.metadata.create_all); both converge on the same schema.

Revision ID: 0021_download_jobs
Revises: 0020_catalog_discovery
Create Date: 2026-06-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_download_jobs"
down_revision: Union[str, None] = "0020_catalog_discovery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "download_jobs" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "download_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("catalog_work_id", sa.Integer(), sa.ForeignKey("catalog_works.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("target_shelf_id", sa.Integer(), sa.ForeignKey("bookshelves.id"), nullable=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("release_title", sa.String(1024), nullable=True),
        sa.Column("indexer", sa.String(128), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fmt", sa.String(16), nullable=True),
        sa.Column("nzo_id", sa.String(64), nullable=True),
        sa.Column("sab_category", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("storage_path", sa.String(1024), nullable=True),
        sa.Column("work_id", sa.Integer(), sa.ForeignKey("works.id"), nullable=True),
        sa.Column("grab_kind", sa.String(8), nullable=False, server_default="manual"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_download_jobs_catalog_work_id", "download_jobs", ["catalog_work_id"])
    op.create_index("ix_download_jobs_user_id", "download_jobs", ["user_id"])
    op.create_index("ix_download_jobs_nzo_id", "download_jobs", ["nzo_id"])
    op.create_index("ix_download_jobs_status", "download_jobs", ["status"])


def downgrade() -> None:
    op.drop_table("download_jobs")
