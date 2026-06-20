"""index_sites.allowed_media_kinds (per-source media-kind allowlist)

One additive nullable JSON column on index_sites: ``allowed_media_kinds`` — a subset of
{"text","comic"}. NULL/[] = the crawl site serves all kinds; when set, the site only contributes
catalog members of those kinds to acquisition matching (so a novels-only source can't false-match a
comic, and vice-versa).

Idempotent (inspect-before-add) like 0036/0037/0038. Also registered in db._ADDITIVE_COLUMNS so an
existing DB gets it additively at boot (create_all won't ALTER an existing table). No backfill.

Revision ID: 0039_index_site_media_kinds
Revises: 0038_content_request_planned_rescan
Create Date: 2026-06-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039_index_site_media_kinds"
down_revision: Union[str, None] = "0038_content_request_planned_rescan"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("index_sites")}
    if "allowed_media_kinds" not in cols:
        op.add_column("index_sites", sa.Column("allowed_media_kinds", sa.JSON(), nullable=True))


def downgrade() -> None:
    try:
        op.drop_column("index_sites", "allowed_media_kinds")
    except Exception:  # noqa: BLE001
        pass
