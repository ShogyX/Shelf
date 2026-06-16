"""cover_url partial indexes for the cover-cache localize scan (F07)

The cover-cache batch scans catalog_groups, catalog_works, works and indexed_pages for
``cover_url LIKE 'http%'`` to localize remote covers. A SQLite PARTIAL index on cover_url (same
predicate) lets those scans hit only the still-remote rows instead of the whole table.

Idempotent (inspect-before-create) like 0028–0031. Mirrors the ORM ``__table_args__`` so a
create_all-built DB and a migrated DB converge.

Revision ID: 0032_cover_url_partial_index
Revises: 0031_missing_content_ledger
Create Date: 2026-06-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032_cover_url_partial_index"
down_revision: Union[str, None] = "0031_missing_content_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index_name, table_name) — every table that actually has a cover_url column.
_INDEXES = (
    ("ix_works_cover_url_remote", "works"),
    ("ix_catalog_groups_cover_url_remote", "catalog_groups"),
    ("ix_catalog_works_cover_url_remote", "catalog_works"),
    ("ix_indexed_pages_cover_url_remote", "indexed_pages"),
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    for index_name, table_name in _INDEXES:
        if table_name not in tables:
            continue
        existing = {ix["name"] for ix in insp.get_indexes(table_name)}
        if index_name in existing:
            continue
        op.create_index(
            index_name, table_name, ["cover_url"],
            sqlite_where=sa.text("cover_url LIKE 'http%'"),
        )


def downgrade() -> None:
    for index_name, table_name in _INDEXES:
        try:
            op.drop_index(index_name, table_name=table_name)
        except Exception:  # noqa: BLE001
            pass
