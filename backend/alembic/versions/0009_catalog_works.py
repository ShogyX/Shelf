"""catalog_works (discovered literary works) + works health columns

Adds:
  * catalog_works — works discovered while indexing, searchable + hookable.
  * works.health / health_detail / health_checked_at — completeness diagnosis.

Mirrors the create_all + additive boot path (db.py _ensure_columns; the
catalog_works table itself is created by Base.metadata.create_all on boot).

Revision ID: 0009_catalog_works
Revises: 0008_auth_users
Create Date: 2026-06-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_catalog_works"
down_revision: Union[str, None] = "0008_auth_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    for name, ddl in (
        ("health", sa.Column("health", sa.String(16), nullable=False, server_default="unknown")),
        ("health_detail", sa.Column("health_detail", sa.Text(), nullable=True)),
        ("health_checked_at", sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True)),
    ):
        if _has_table(bind, "works") and not _has_column(bind, "works", name):
            with op.batch_alter_table("works") as batch:
                batch.add_column(ddl)

    if _has_table(bind, "indexed_pages") and not _has_column(bind, "indexed_pages", "priority"):
        with op.batch_alter_table("indexed_pages") as batch:
            batch.add_column(
                sa.Column("priority", sa.Integer(), nullable=False, server_default="0")
            )

    if not _has_table(bind, "catalog_works"):
        op.create_table(
            "catalog_works",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("site_id", sa.Integer(), sa.ForeignKey("index_sites.id"), nullable=False, index=True),
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
            sa.Column("discovered_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("site_id", "work_url", name="uq_catalog_site_url"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "catalog_works"):
        op.drop_table("catalog_works")
    for name in ("health_checked_at", "health_detail", "health"):
        if _has_table(bind, "works") and _has_column(bind, "works", name):
            with op.batch_alter_table("works") as batch:
                batch.drop_column(name)
