"""add user_settings.delivery_config

Revision ID: 0006_delivery
Revises: 0005_kindle
Create Date: 2026-05-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_delivery"
down_revision: Union[str, None] = "0005_kindle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("user_settings") as batch:
        batch.add_column(sa.Column("delivery_config", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user_settings") as batch:
        batch.drop_column("delivery_config")
