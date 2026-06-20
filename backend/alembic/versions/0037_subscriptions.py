"""subscriptions: per-user follow of an author or series (Wave E, R14-R16)

Wave E follow author / series:
- subscriptions: one row per (user, kind, key) a user follows. The follow_tick re-enumerates each
  active sub on a 6h cadence and (auto_request) auto-fetches NEW titles via the normal acquire
  pipeline, tagging the ledger row origin="following". known_keys (JSON) is the diff baseline, seeded
  at subscribe time so day-1 backlog isn't auto-fired. UNIQUE(user_id, kind, key).

Idempotent (inspect-before-create) like 0034/0035/0036. Mirrors the ORM model so a create_all-built DB
and a migrated DB converge. No backfill.

Revision ID: 0037_subscriptions
Revises: 0036_content_request_origin
Create Date: 2026-06-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037_subscriptions"
down_revision: Union[str, None] = "0036_content_request_origin"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())

    if "subscriptions" not in names:
        op.create_table(
            "subscriptions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("key", sa.String(length=512), nullable=False),
            sa.Column("display_name", sa.String(length=255), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=True),
            sa.Column("auto_request", sa.Boolean(), nullable=True),
            sa.Column("known_keys", sa.JSON(), nullable=True),
            sa.Column("auto_added", sa.Integer(), nullable=True),
            sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", "kind", "key", name="uq_subscription_user_kind_key"),
        )
        op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
        op.create_index("ix_subscriptions_key", "subscriptions", ["key"])


def downgrade() -> None:
    try:
        op.drop_table("subscriptions")
    except Exception:  # noqa: BLE001
        pass
