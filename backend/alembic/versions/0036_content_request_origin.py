"""content_requests.origin + origin_detail: where a ledger row came from

Wave D origin tags:
- origin: NULL/"request" = a direct request · "series" = a sibling auto-requested by the auto-series
  hook · "goodreads" = a waiting-on-hook virtual row (set at read time, never stored).
- origin_detail: the series name for an auto-pulled sibling (so the Wanted page can show
  'from series "…"').

Idempotent (inspect-before-create) like 0034/0035. Mirrors the ORM model so a create_all-built DB and
a migrated DB converge. No backfill: existing rows stay NULL, which surfaces as "request".

Revision ID: 0036_content_request_origin
Revises: 0035_vt_submissions
Create Date: 2026-06-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036_content_request_origin"
down_revision: Union[str, None] = "0035_vt_submissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("content_requests")}
    if "origin" not in cols:
        op.add_column("content_requests", sa.Column("origin", sa.String(length=16), nullable=True))
    if "origin_detail" not in cols:
        op.add_column("content_requests",
                      sa.Column("origin_detail", sa.String(length=255), nullable=True))


def downgrade() -> None:
    for col in ("origin_detail", "origin"):
        try:
            op.drop_column("content_requests", col)
        except Exception:  # noqa: BLE001
            pass
