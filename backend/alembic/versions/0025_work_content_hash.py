"""work content hash: dedupe imports by file-byte sha256

Adds:
  * works.content_hash — sha256 of imported file bytes, so a re-import of the same book under a
    different name/path (or as another format) updates the SAME Work instead of creating a
    duplicate (13C). Indexed for the import-time lookup.

Mirrors the boot-time additive-column + index path in app/db.py; both converge on the same schema.
Idempotent (guards on the existing column) so it is safe alongside create_all.

Revision ID: 0025_work_content_hash
Revises: 0024_unique_race_constraints
Create Date: 2026-06-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_work_content_hash"
down_revision: Union[str, None] = "0024_unique_race_constraints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("works")}
    if "content_hash" not in cols:
        op.add_column("works", sa.Column("content_hash", sa.String(64), nullable=True))
    idx = {i["name"] for i in insp.get_indexes("works")}
    if "ix_works_content_hash" not in idx:
        op.create_index("ix_works_content_hash", "works", ["content_hash"])


def downgrade() -> None:
    try:
        op.drop_index("ix_works_content_hash", table_name="works")
    except Exception:  # noqa: BLE001
        pass
    try:
        op.drop_column("works", "content_hash")
    except Exception:  # noqa: BLE001
        pass
