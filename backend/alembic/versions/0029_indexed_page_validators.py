"""indexed_pages HTTP cache validators: conditional-GET on crawl re-fetch

Adds:
  * indexed_pages.etag          — the last fetch's ETag, replayed as If-None-Match
  * indexed_pages.last_modified — the last fetch's Last-Modified, replayed as If-Modified-Since

so a re-fetched page that is UNCHANGED returns an empty 304 instead of a full re-download +
re-parse (F04 — the ~12h discovery-refresh re-crawl is the main beneficiary).

Mirrors the boot-time additive-column path in app/db.py; both converge on the same schema.
Idempotent (guards on the existing columns) so it is safe alongside create_all.

Revision ID: 0029_indexed_page_validators
Revises: 0028_notifications
Create Date: 2026-06-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_indexed_page_validators"
down_revision: Union[str, None] = "0028_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("indexed_pages")}
    if "etag" not in cols:
        op.add_column("indexed_pages", sa.Column("etag", sa.String(256), nullable=True))
    if "last_modified" not in cols:
        op.add_column("indexed_pages", sa.Column("last_modified", sa.String(64), nullable=True))


def downgrade() -> None:
    for col in ("last_modified", "etag"):
        try:
            op.drop_column("indexed_pages", col)
        except Exception:  # noqa: BLE001
            pass
