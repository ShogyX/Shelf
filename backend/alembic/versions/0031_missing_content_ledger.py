"""missing-content ledger: content_requests + content_request_requesters

Adds the per-TITLE missing-content ledger:
- content_requests: one row per unobtainable title cluster (norm_key + media_bucket), carrying its
  status, failure reason, attempt count, and the jittered next_check_at the periodic re-check tick
  reads. UNIQUE(norm_key, media_bucket).
- content_request_requesters: who asked for a missing title (NULL user_id = system/stock request).
  UNIQUE(request_id, user_id).

Idempotent (inspect-before-create) like 0028/0029/0030. Mirrors the ORM models so a create_all-built
DB and a migrated DB converge.

Revision ID: 0031_missing_content_ledger
Revises: 0030_self_registration
Create Date: 2026-06-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031_missing_content_ledger"
down_revision: Union[str, None] = "0030_self_registration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())

    if "content_requests" not in names:
        op.create_table(
            "content_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("norm_key", sa.String(length=512), nullable=False),
            sa.Column("media_bucket", sa.String(length=16), nullable=False, server_default="text"),
            sa.Column("catalog_work_id", sa.Integer(),
                      sa.ForeignKey("catalog_works.id"), nullable=True),
            sa.Column("title", sa.String(length=512), nullable=False),
            sa.Column("author", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
            sa.Column("failure_reason", sa.String(length=32), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_provider", sa.String(length=32), nullable=True),
            sa.Column("first_requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("norm_key", "media_bucket", name="uq_content_request_cluster"),
        )
        op.create_index("ix_content_requests_norm_key", "content_requests", ["norm_key"])
        op.create_index("ix_content_requests_status", "content_requests", ["status"])
        op.create_index("ix_content_requests_catalog_work_id", "content_requests",
                        ["catalog_work_id"])
        op.create_index("ix_content_requests_next_check_at", "content_requests", ["next_check_at"])

    if "content_request_requesters" not in names:
        op.create_table(
            "content_request_requesters",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("request_id", sa.Integer(),
                      sa.ForeignKey("content_requests.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("request_id", "user_id", name="uq_content_request_requester"),
        )
        op.create_index("ix_content_request_requesters_request_id",
                        "content_request_requesters", ["request_id"])
        op.create_index("ix_content_request_requesters_user_id",
                        "content_request_requesters", ["user_id"])


def downgrade() -> None:
    for tbl in ("content_request_requesters", "content_requests"):
        try:
            op.drop_table(tbl)
        except Exception:  # noqa: BLE001
            pass
