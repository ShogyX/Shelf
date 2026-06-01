"""users + sessions, and per-user reading progress / settings

Adds:
  * users, user_sessions
  * reading_states.user_id (+ drop legacy UNIQUE(work_id), add UNIQUE(user_id, work_id))
  * user_settings.user_id (UNIQUE)

Mirrors the create_all + additive boot path (db.py _ensure_columns /
_migrate_reading_states_per_user).

Revision ID: 0008_auth_users
Revises: 0007_folders_index
Create Date: 2026-06-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_auth_users"
down_revision: Union[str, None] = "0007_folders_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("username", sa.String(64), nullable=False, unique=True, index=True),
            sa.Column("display_name", sa.String(128), nullable=True),
            sa.Column("password_hash", sa.String(255), nullable=False),
            sa.Column("role", sa.String(16), nullable=False, server_default="user"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_table(bind, "user_sessions"):
        op.create_table(
            "user_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("token", sa.String(128), nullable=False, unique=True, index=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not _has_column(bind, "reading_states", "user_id"):
        with op.batch_alter_table("reading_states") as batch:
            batch.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    if not _has_column(bind, "user_settings", "user_id"):
        with op.batch_alter_table("user_settings") as batch:
            batch.add_column(sa.Column("user_id", sa.Integer(), nullable=True))

    # Drop the legacy UNIQUE(work_id) index; add UNIQUE(user_id, work_id).
    insp = sa.inspect(bind)
    for idx in insp.get_indexes("reading_states"):
        if idx.get("unique") and idx.get("column_names") == ["work_id"]:
            op.drop_index(idx["name"], table_name="reading_states")
    names = {i["name"] for i in sa.inspect(bind).get_indexes("reading_states")}
    if "uq_reading_user_work" not in names:
        op.create_index("uq_reading_user_work", "reading_states", ["user_id", "work_id"], unique=True)
    su = {i["name"] for i in sa.inspect(bind).get_indexes("user_settings")}
    if "uq_user_settings_user" not in su:
        op.create_index("uq_user_settings_user", "user_settings", ["user_id"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in ("uq_reading_user_work",):
        if name in {i["name"] for i in sa.inspect(bind).get_indexes("reading_states")}:
            op.drop_index(name, table_name="reading_states")
    if "uq_user_settings_user" in {i["name"] for i in sa.inspect(bind).get_indexes("user_settings")}:
        op.drop_index("uq_user_settings_user", table_name="user_settings")
    with op.batch_alter_table("reading_states") as batch:
        batch.drop_column("user_id")
    with op.batch_alter_table("user_settings") as batch:
        batch.drop_column("user_id")
    op.drop_table("user_sessions")
    op.drop_table("users")
