"""catalog discovery: popularity/genre/theme rows

Adds the persisted discovery layer behind the Index page's category rows:
  * new signal columns on catalog_works (popularity / rating / rating_count / year /
    group_id / enriched_at / enrich_source);
  * catalog_groups — one row per logical work (clustered across sources) with a
    precomputed normalized popularity score, so rows are cheap indexed reads;
  * catalog_tags — genre/theme/demographic/format labels rolled up onto a group;
  * catalog_categories — materialized summary of which tags are populous enough to be rows.

Mirrors the boot-time additive path in app/db.py (create_all + _ensure_columns/_ensure_indexes);
both converge on the same schema.

Revision ID: 0020_catalog_discovery
Revises: 0019_work_crawl_paused
Create Date: 2026-06-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_catalog_discovery"
down_revision: Union[str, None] = "0019_work_crawl_paused"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = {
        "popularity": sa.Column("popularity", sa.Float(), nullable=False, server_default="0"),
        "rating": sa.Column("rating", sa.Float(), nullable=True),
        "rating_count": sa.Column("rating_count", sa.Integer(), nullable=True),
        "year": sa.Column("year", sa.Integer(), nullable=True),
        "group_id": sa.Column("group_id", sa.Integer(), nullable=True),
        "enriched_at": sa.Column("enriched_at", sa.DateTime(), nullable=True),
        "enrich_source": sa.Column("enrich_source", sa.String(32), nullable=True),
    }
    for name, col in cols.items():
        if not _has_column(bind, "catalog_works", name):
            op.add_column("catalog_works", col)

    if not sa.inspect(bind).has_table("catalog_groups"):
        op.create_table(
            "catalog_groups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("norm_key", sa.String(512), nullable=False, server_default="", index=True),
            sa.Column("media_bucket", sa.String(16), nullable=False, server_default="text", index=True),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("author", sa.String(255), nullable=True),
            sa.Column("cover_url", sa.String(1024), nullable=True),
            sa.Column("synopsis", sa.Text(), nullable=True),
            sa.Column("language", sa.String(16), nullable=True),
            sa.Column("media_label", sa.String(16), nullable=False, server_default="Novel"),
            sa.Column("chapters", sa.Integer(), nullable=True),
            sa.Column("popularity_norm", sa.Float(), nullable=False, server_default="0", index=True),
            sa.Column("source_domain", sa.String(255), nullable=True),
            sa.Column("member_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("hooked_work_id", sa.Integer(), sa.ForeignKey("works.id"), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )

    if not sa.inspect(bind).has_table("catalog_tags"):
        op.create_table(
            "catalog_tags",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("group_id", sa.Integer(), sa.ForeignKey("catalog_groups.id"), nullable=False, index=True),
            sa.Column("kind", sa.String(16), nullable=False, index=True),
            sa.Column("slug", sa.String(96), nullable=False, index=True),
            sa.Column("label", sa.String(96), nullable=False),
            sa.UniqueConstraint("group_id", "kind", "slug", name="uq_catalog_tag"),
        )

    if not sa.inspect(bind).has_table("catalog_categories"):
        op.create_table(
            "catalog_categories",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kind", sa.String(16), nullable=False, index=True),
            sa.Column("slug", sa.String(96), nullable=False, index=True),
            sa.Column("label", sa.String(96), nullable=False),
            sa.Column("media_bucket", sa.String(16), nullable=False, server_default="text", index=True),
            sa.Column("group_count", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint("kind", "slug", "media_bucket", name="uq_catalog_category"),
        )

    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_catalog_works_group ON catalog_works (group_id)",
        "CREATE INDEX IF NOT EXISTS ix_catalog_works_enrich ON catalog_works (enriched_at, popularity)",
        "CREATE INDEX IF NOT EXISTS ix_catalog_groups_pop ON catalog_groups (media_bucket, popularity_norm)",
        "CREATE INDEX IF NOT EXISTS ix_catalog_tags_kind_slug ON catalog_tags (kind, slug)",
    ):
        op.execute(stmt)


def downgrade() -> None:
    op.drop_table("catalog_categories")
    op.drop_table("catalog_tags")
    op.drop_table("catalog_groups")
    for name in ("enrich_source", "enriched_at", "group_id", "year", "rating_count", "rating", "popularity"):
        op.drop_column("catalog_works", name)
