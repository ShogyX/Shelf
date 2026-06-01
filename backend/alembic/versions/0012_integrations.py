"""integrations + catalog provider columns (Readarr / Kapowarr)

Adds the integrations table and provider-aware columns on catalog_works
(provider / provider_ref / integration_id / extra, and a nullable site_id).
catalog_works is a derived cache (rebuilt from crawl + integration sync), so the
pre-integration table is dropped + recreated rather than rebuilt in place.

Revision ID: 0012_integrations
Revises: 0011_crawl_policy
Create Date: 2026-06-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_integrations"
down_revision: Union[str, None] = "0011_crawl_policy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _create_catalog_works() -> None:
    op.create_table(
        "catalog_works",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False, server_default="web_index", index=True),
        sa.Column("provider_ref", sa.String(255), nullable=True, index=True),
        sa.Column("integration_id", sa.Integer(), sa.ForeignKey("integrations.id"), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("index_sites.id"), nullable=True, index=True),
        sa.Column("domain", sa.String(255), nullable=False, index=True),
        sa.Column("work_url", sa.String(2048), nullable=False),
        sa.Column("norm_key", sa.String(512), nullable=False, server_default="", index=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("author", sa.String(255), nullable=True),
        sa.Column("cover_url", sa.String(1024), nullable=True),
        sa.Column("synopsis", sa.Text(), nullable=True),
        sa.Column("language", sa.String(16), nullable=True, server_default="en"),
        sa.Column("media_kind", sa.String(16), nullable=False, server_default="text"),
        sa.Column("kind", sa.String(16), nullable=False, server_default="work"),
        sa.Column("chapters_advertised", sa.Integer(), nullable=True),
        sa.Column("chapters_listed", sa.Integer(), nullable=True),
        sa.Column("health", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("health_detail", sa.Text(), nullable=True),
        sa.Column("diagnosed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hooked_work_id", sa.Integer(), sa.ForeignKey("works.id"), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("site_id", "work_url", name="uq_catalog_site_url"),
    )


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "integrations"):
        op.create_table(
            "integrations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kind", sa.String(32), nullable=False, index=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("base_url", sa.String(512), nullable=False),
            sa.Column("api_key", sa.String(255), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("root_folder", sa.String(1024), nullable=True),
            sa.Column("quality_profile_id", sa.Integer(), nullable=True),
            sa.Column("metadata_profile_id", sa.Integer(), nullable=True),
            sa.Column("auto_map_folders", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Provider-aware catalog: drop the pre-integration cache table + recreate it.
    if _has_table(bind, "catalog_works") and not _has_column(bind, "catalog_works", "provider"):
        op.drop_table("catalog_works")
    if not _has_table(bind, "catalog_works"):
        _create_catalog_works()


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "catalog_works"):
        op.drop_table("catalog_works")
    if _has_table(bind, "integrations"):
        op.drop_table("integrations")
