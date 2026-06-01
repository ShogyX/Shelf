"""add works.total_chapters_expected

Revision ID: 0004_expected
Revises: 0003_paragraph
Create Date: 2026-05-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_expected"
down_revision: Union[str, None] = "0003_paragraph"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("works") as batch:
        batch.add_column(sa.Column("total_chapters_expected", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("works") as batch:
        batch.drop_column("total_chapters_expected")
