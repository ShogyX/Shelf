"""vt_submissions: durable VirusTotal lookup ledger (free-tier quota backing)

Wave C VirusTotal hard gate:
- vt_submissions: append-only, one row per SUCCESSFUL hash lookup (a clone of usenet_grabs). Backs
  the per-minute (4) and per-day (500) free-tier caps in torrent_scan.vt_blocked_until durably across
  restarts (the in-memory ratelimit spacer can't). Indexed on created_at.

Idempotent (inspect-before-create) like 0034. Mirrors the ORM model so a create_all-built DB and a
migrated DB converge. No backfill.

Revision ID: 0035_vt_submissions
Revises: 0034_work_source_search
Create Date: 2026-06-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035_vt_submissions"
down_revision: Union[str, None] = "0034_work_source_search"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())

    if "vt_submissions" not in names:
        op.create_table(
            "vt_submissions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_vt_submissions_created_at", "vt_submissions", ["created_at"])


def downgrade() -> None:
    try:
        op.drop_table("vt_submissions")
    except Exception:  # noqa: BLE001
        pass
