"""per-user library membership + bookshelves + auto-hook destination + apprise push

Adds:
  * library_items (user ↔ work membership: the per-user library)
  * bookshelves + bookshelf_items (per-user organization with per-shelf automation flags)
  * queued_hooks.user_id / target_shelf_id (per-user auto-hook destination)
  * user_settings.apprise_url (per-user push-notification target)

Mirrors the additive boot path (db.py: create_all + _ensure_columns + _seed_library_membership),
so this is a no-op on an already-booted database.

Revision ID: 0015_per_user_library
Revises: 0014_web_index_budget_recover
Create Date: 2026-06-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_per_user_library"
down_revision: Union[str, None] = "0014_web_index_budget_recover"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "library_items"):
        op.create_table(
            "library_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), index=True),
            sa.Column("work_id", sa.Integer(), sa.ForeignKey("works.id"), index=True),
            sa.Column("added_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("user_id", "work_id", name="uq_library_user_work"),
        )
    if not _has_table(bind, "bookshelves"):
        op.create_table(
            "bookshelves",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), index=True),
            sa.Column("name", sa.String(128)),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("auto_update", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("auto_kindle", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("notify_on_add", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("goodreads_target", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("user_id", "name", name="uq_bookshelf_user_name"),
        )
    if not _has_table(bind, "bookshelf_items"):
        op.create_table(
            "bookshelf_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("shelf_id", sa.Integer(), sa.ForeignKey("bookshelves.id"), index=True),
            sa.Column("work_id", sa.Integer(), sa.ForeignKey("works.id"), index=True),
            sa.Column("added_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("shelf_id", "work_id", name="uq_shelf_work"),
        )
    for col in (
        sa.Column("user_id", sa.Integer()),
        sa.Column("target_shelf_id", sa.Integer()),
    ):
        if _has_table(bind, "queued_hooks") and not _has_column(bind, "queued_hooks", col.name):
            op.add_column("queued_hooks", col)
    if _has_table(bind, "user_settings") and not _has_column(bind, "user_settings", "apprise_url"):
        op.add_column("user_settings", sa.Column("apprise_url", sa.String(2048)))


def downgrade() -> None:
    pass  # additive; nothing to undo
