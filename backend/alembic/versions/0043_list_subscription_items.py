"""list_subscription_items (cached snapshot of an imported list's titles)

A new table backing the list-import CACHE + change-scan. Each imported list's items (title, author, ref,
media_kind, cover_url) are persisted at import time so ``GET /list-imports/{id}/items`` serves from the DB
instantly (no re-fetch), and ``list_sync_tick`` diffs the lightweight external fetch against these cached
rows to find ADDED / REMOVED titles (marking removals via ``removed_at`` rather than deleting). Work/cover
resolution stays lazy — the scan never resolves images.

New TABLE → created by create_all at boot on existing DBs (so no _ADDITIVE_COLUMNS entry, which is only
for columns on existing tables); this migration records it for alembic. Idempotent (inspect-before).

Revision ID: 0043_list_subscription_items
Revises: 0042_list_sub_series_opts
Create Date: 2026-06-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043_list_subscription_items"
down_revision: Union[str, None] = "0042_list_sub_series_opts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "list_subscription_items" in insp.get_table_names():
        return
    op.create_table(
        "list_subscription_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(),
                  sa.ForeignKey("list_subscriptions.id", ondelete="CASCADE"),
                  index=True, nullable=False),
        sa.Column("norm_key", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("ref", sa.String(length=255), nullable=True),
        sa.Column("media_kind", sa.String(length=16), nullable=False, server_default="text"),
        sa.Column("cover_url", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("subscription_id", "norm_key", name="uq_listsubitem_sub_key"),
    )


def downgrade() -> None:
    try:
        op.drop_table("list_subscription_items")
    except Exception:  # noqa: BLE001
        pass
