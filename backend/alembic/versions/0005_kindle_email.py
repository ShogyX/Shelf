"""add user_settings.kindle_email

Revision ID: 0005_kindle
Revises: 0004_expected
Create Date: 2026-05-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_kindle"
down_revision: Union[str, None] = "0004_expected"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("user_settings") as batch:
        batch.add_column(sa.Column("kindle_email", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user_settings") as batch:
        batch.drop_column("kindle_email")
