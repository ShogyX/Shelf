"""add reading_states.paragraph_index

Revision ID: 0003_paragraph
Revises: 0002_render_js
Create Date: 2026-05-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_paragraph"
down_revision: Union[str, None] = "0002_render_js"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("reading_states") as batch:
        batch.add_column(
            sa.Column("paragraph_index", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("reading_states") as batch:
        batch.drop_column("paragraph_index")
