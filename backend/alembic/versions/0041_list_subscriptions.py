"""list_subscriptions (monitored external reading-list imports)

A new table backing the "import an external reading list/library" feature (AniList, Goodreads, Open
Library, Hardcover, MyAnimeList, Amazon wishlist). ``list_sync_tick`` re-fetches each active row on the
global admin cadence and auto-acquires NEW titles per ``variant``, diffing against ``known_keys``.

New TABLE → created by create_all at boot on existing DBs (so no _ADDITIVE_COLUMNS entry, which is only
for columns on existing tables); this migration records it for alembic. Idempotent (inspect-before).

Revision ID: 0041_list_subscriptions
Revises: 0040_work_series_id
Create Date: 2026-06-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041_list_subscriptions"
down_revision: Union[str, None] = "0040_work_series_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "list_subscriptions" in insp.get_table_names():
        return
    op.create_table(
        "list_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), index=True, nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("list_ref", sa.String(length=512), nullable=False),
        sa.Column("list_name", sa.String(length=128), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("variant", sa.String(length=16), nullable=False, server_default="ebook"),
        sa.Column("target_shelf_id", sa.Integer(), sa.ForeignKey("bookshelves.id"), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("known_keys", sa.JSON(), nullable=True),
        sa.Column("auto_added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "provider", "list_ref", name="uq_listsub_user_provider_ref"),
    )


def downgrade() -> None:
    try:
        op.drop_table("list_subscriptions")
    except Exception:  # noqa: BLE001
        pass
