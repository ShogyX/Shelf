"""list_subscriptions.auto_series / auto_follow_series

Two additive boolean columns on list_subscriptions: per-list options to also fetch the rest of a
fetched title's series now (auto_series) and/or start a series follow for future volumes
(auto_follow_series). Registered in db._ADDITIVE_COLUMNS so an existing DB gets them at boot.

Idempotent (inspect-before-add) like 0036–0041.

Revision ID: 0042_list_sub_series_opts
Revises: 0041_list_subscriptions
Create Date: 2026-06-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042_list_sub_series_opts"
down_revision: Union[str, None] = "0041_list_subscriptions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "list_subscriptions" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("list_subscriptions")}
    for name in ("auto_series", "auto_follow_series"):
        if name not in cols:
            op.add_column("list_subscriptions",
                          sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    for name in ("auto_follow_series", "auto_series"):
        try:
            op.drop_column("list_subscriptions", name)
        except Exception:  # noqa: BLE001
            pass
