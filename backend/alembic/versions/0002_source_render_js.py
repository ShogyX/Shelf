"""add sources.render_js

Revision ID: 0002_render_js
Revises: 43199b8e47a7
Create Date: 2026-05-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_render_js"
down_revision: Union[str, None] = "43199b8e47a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sources") as batch:
        batch.add_column(
            sa.Column("render_js", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("sources") as batch:
        batch.drop_column("render_js")
