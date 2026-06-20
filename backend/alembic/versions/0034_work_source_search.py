"""per-(work, source) search state: work_source_searches + source_attempts

Wave B fine-grained acquisition gate:
- work_source_searches: one row per (content_request, durable source) carrying the per-source search
  status, last result/reason, the CAS lease, and the next_retry_at the source-retry tick reads.
  UNIQUE(content_request_id, source); indexed on next_retry_at.
- source_attempts: append-only record of every durable-source search issued (source, ok, created_at)
  — powers the opt-in per-source daily availability cap.

Idempotent (inspect-before-create) like 0031/0033. Mirrors the ORM models so a create_all-built DB
and a migrated DB converge. No backfill: legacy ContentRequests lazily get pending children on the
next acquire; the retry tick's legacy sweep covers unattended legacy unavailable rows.

Revision ID: 0034_work_source_search
Revises: 0033_chapter_content_raw_checksum
Create Date: 2026-06-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034_work_source_search"
down_revision: Union[str, None] = "0033_chapter_content_raw_checksum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())

    if "work_source_searches" not in names:
        op.create_table(
            "work_source_searches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("content_request_id", sa.Integer(),
                      sa.ForeignKey("content_requests.id"), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("last_http_status", sa.Integer(), nullable=True),
            sa.Column("reason", sa.String(length=64), nullable=True),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lease_token", sa.String(length=36), nullable=True),
            sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint("content_request_id", "source", name="uq_work_source_search"),
        )
        op.create_index("ix_work_source_searches_content_request_id",
                        "work_source_searches", ["content_request_id"])
        op.create_index("ix_work_source_searches_status", "work_source_searches", ["status"])
        op.create_index("ix_work_source_search_next_retry",
                        "work_source_searches", ["next_retry_at"])

    if "source_attempts" not in names:
        op.create_table(
            "source_attempts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_source_attempts_source", "source_attempts", ["source"])
        op.create_index("ix_source_attempts_created_at", "source_attempts", ["created_at"])


def downgrade() -> None:
    for tbl in ("source_attempts", "work_source_searches"):
        try:
            op.drop_table(tbl)
        except Exception:  # noqa: BLE001
            pass
