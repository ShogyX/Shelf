"""self-registration: user email + approval_status, password_reset_tokens, per-user default shelves

Adds the self-registration + password-recovery storage:
- users.email (nullable, unique): stored for recovery, NOT verified at signup.
- users.approval_status ("approved" | "pending"): the approval-mode gate; existing + admin-created
  users default to "approved".
- password_reset_tokens: single-use, time-limited forgot-password tokens.
- user_settings.work_default_shelves: per-user {str(work_id): shelf_id} default-shelf map.

Idempotent (inspect-before-create) like 0028/0029. Mirrors db._ADDITIVE_COLUMNS so a create_all-built
DB and a migrated DB converge.

Revision ID: 0030_self_registration
Revises: 0029_indexed_page_validators
Create Date: 2026-06-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030_self_registration"
down_revision: Union[str, None] = "0029_indexed_page_validators"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())

    if "users" in names:
        cols = {c["name"] for c in insp.get_columns("users")}
        if "email" not in cols:
            op.add_column("users", sa.Column("email", sa.String(length=255), nullable=True))
            op.create_index("ix_users_email", "users", ["email"], unique=True)
        if "approval_status" not in cols:
            op.add_column("users", sa.Column(
                "approval_status", sa.String(length=16),
                nullable=False, server_default="approved",
            ))

    if "user_settings" in names:
        cols = {c["name"] for c in insp.get_columns("user_settings")}
        if "work_default_shelves" not in cols:
            op.add_column("user_settings", sa.Column("work_default_shelves", sa.JSON(), nullable=True))

    if "password_reset_tokens" not in names:
        op.create_table(
            "password_reset_tokens",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("token", sa.String(length=128), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])
        op.create_index(
            "ix_password_reset_tokens_token", "password_reset_tokens", ["token"], unique=True
        )


def downgrade() -> None:
    try:
        op.drop_table("password_reset_tokens")
    except Exception:  # noqa: BLE001
        pass
    for col in ("approval_status", "email"):
        try:
            op.drop_column("users", col)
        except Exception:  # noqa: BLE001
            pass
    try:
        op.drop_column("user_settings", "work_default_shelves")
    except Exception:  # noqa: BLE001
        pass
