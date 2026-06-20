"""content_requests.release_date + rescan_queued_at (Watchlist redesign)

Two additive nullable columns on content_requests:
- release_date (Date): a Planned title's provider release date — set with status="planned" when a
  title isn't yet released (a future provider date/year); the re-evaluation sweep flips it to "open"
  + searches once the date passes. NULL/past = released (never blocks a fetchable title).
- rescan_queued_at (DateTime, indexed): mass-rescan queue marker. The rescan_drain_tick picks the
  oldest queued rows and force-re-acquires them sequentially, clearing the marker as it goes.

Idempotent (inspect-before-add) like 0034/0036/0037. Mirrors the ORM model so a create_all-built DB
and a migrated DB converge. No backfill.

Revision ID: 0038_content_request_planned_rescan
Revises: 0037_subscriptions
Create Date: 2026-06-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038_content_request_planned_rescan"
down_revision: Union[str, None] = "0037_subscriptions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("content_requests")}
    if "release_date" not in cols:
        op.add_column("content_requests", sa.Column("release_date", sa.Date(), nullable=True))
    if "rescan_queued_at" not in cols:
        op.add_column("content_requests",
                      sa.Column("rescan_queued_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index("ix_content_requests_rescan_queued_at", "content_requests",
                        ["rescan_queued_at"])


def downgrade() -> None:
    try:
        op.drop_index("ix_content_requests_rescan_queued_at", table_name="content_requests")
    except Exception:  # noqa: BLE001
        pass
    for col in ("rescan_queued_at", "release_date"):
        try:
            op.drop_column("content_requests", col)
        except Exception:  # noqa: BLE001
            pass
