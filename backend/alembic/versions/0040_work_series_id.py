"""works.series_id (stable canonical series identity)

One additive nullable column on ``works``: ``series_id`` — "hc:<id>" from Hardcover's series
resolution, else "name:<norm>" fallback. Lets the library/dedup key a series by a stable id instead
of its free-text name, so two same-named series don't collide and an owned volume whose catalog title
drifted is still recognized as in-series (Project 2 / S-DUP-2 / S-DUP-3). Indexed for the ownership
probe in series._annotate.

Idempotent (inspect-before-add) like 0036–0039. Also registered in db._ADDITIVE_COLUMNS so an
existing DB gets it additively at boot (create_all won't ALTER an existing table). No backfill — rows
acquire a series_id the next time their series is resolved.

Revision ID: 0040_work_series_id
Revises: 0039_index_site_media_kinds
Create Date: 2026-06-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0040_work_series_id"
down_revision: Union[str, None] = "0039_index_site_media_kinds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("works")}
    if "series_id" not in cols:
        op.add_column("works", sa.Column("series_id", sa.String(length=64), nullable=True))
    idx = {i["name"] for i in insp.get_indexes("works")}
    if "ix_works_series_id" not in idx:
        op.create_index("ix_works_series_id", "works", ["series_id"])


def downgrade() -> None:
    try:
        op.drop_index("ix_works_series_id", table_name="works")
    except Exception:  # noqa: BLE001
        pass
    try:
        op.drop_column("works", "series_id")
    except Exception:  # noqa: BLE001
        pass
