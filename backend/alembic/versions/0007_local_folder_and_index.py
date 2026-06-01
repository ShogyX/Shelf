"""local folders, watched-media columns, and the URL index (+ FTS)

Adds:
  * works.media_kind / local_path / local_mtime / local_size
  * watched_folders
  * index_sites, indexed_pages (+ contentful FTS5 mirror indexed_pages_fts)

Mirrors the create_all + additive (_ensure_columns / _ensure_fts) boot path so a
fresh Alembic run produces the same schema.

Revision ID: 0007_folders_index
Revises: 0006_delivery
Create Date: 2026-06-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_folders_index"
down_revision: Union[str, None] = "0006_delivery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # 1) New columns on works (guarded so it co-exists with the additive boot path).
    with op.batch_alter_table("works") as batch:
        if not _has_column(bind, "works", "media_kind"):
            batch.add_column(sa.Column("media_kind", sa.String(16), nullable=False,
                                       server_default="text"))
        if not _has_column(bind, "works", "local_path"):
            batch.add_column(sa.Column("local_path", sa.String(1024), nullable=True))
        if not _has_column(bind, "works", "local_mtime"):
            batch.add_column(sa.Column("local_mtime", sa.Float(), nullable=True))
        if not _has_column(bind, "works", "local_size"):
            batch.add_column(sa.Column("local_size", sa.Integer(), nullable=True))
    existing_idx = {i["name"] for i in sa.inspect(bind).get_indexes("works")}
    if "ix_works_local_path" not in existing_idx:
        op.create_index("ix_works_local_path", "works", ["local_path"])

    # 2) Watched local folders.
    if not _has_table(bind, "watched_folders"):
        op.create_table(
            "watched_folders",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("path", sa.String(1024), nullable=False, unique=True),
            sa.Column("display_name", sa.String(255), nullable=True),
            sa.Column("recursive", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )

    # 3) URL index sites + pages.
    if not _has_table(bind, "index_sites"):
        op.create_table(
            "index_sites",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("root_url", sa.String(2048), nullable=False),
            sa.Column("domain", sa.String(255), nullable=False, index=True),
            sa.Column("title", sa.String(512), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("max_pages", sa.Integer(), nullable=False, server_default="200"),
            sa.Column("max_depth", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("same_host_only", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_table(bind, "indexed_pages"):
        op.create_table(
            "indexed_pages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("site_id", sa.Integer(), sa.ForeignKey("index_sites.id"),
                      nullable=False, index=True),
            sa.Column("url", sa.String(2048), nullable=False, index=True),
            sa.Column("title", sa.String(512), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("author", sa.String(255), nullable=True),
            sa.Column("cover_url", sa.String(1024), nullable=True),
            sa.Column("site_name", sa.String(255), nullable=True),
            sa.Column("page_type", sa.String(64), nullable=True),
            sa.Column("html", sa.Text(), nullable=True),
            sa.Column("text", sa.Text(), nullable=True),
            sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending",
                      index=True),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("hooked_work_id", sa.Integer(), sa.ForeignKey("works.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("site_id", "url", name="uq_indexed_page_site_url"),
        )

    # 4) Contentful FTS5 mirror (SQLite only; harmless no-op elsewhere).
    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS indexed_pages_fts USING fts5("
            "title, body, tokenize='unicode61 remove_diacritics 2')"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TABLE IF EXISTS indexed_pages_fts")
    op.drop_table("indexed_pages")
    op.drop_table("index_sites")
    op.drop_table("watched_folders")
    existing_idx = {i["name"] for i in sa.inspect(bind).get_indexes("works")}
    if "ix_works_local_path" in existing_idx:
        op.drop_index("ix_works_local_path", table_name="works")
    with op.batch_alter_table("works") as batch:
        batch.drop_column("local_size")
        batch.drop_column("local_mtime")
        batch.drop_column("local_path")
        batch.drop_column("media_kind")
