"""chapter_contents.raw_checksum — pre-localize content checksum

``store_chapter_content`` localizes (downloads + rewrites) every remote chapter <img> on each
ingest, then checksums the POST-localize body. On the ~12h refresh that re-fetches every image
even when the chapter is unchanged — wasteful, and broken for comic CDNs whose image URLs rotate
(content-addressed cache misses on re-fetch). Storing the PRE-localize (sanitized) checksum lets a
refresh detect "unchanged" and skip localize entirely.

Nullable: rows written before this column get NULL and simply re-localize once on the next refresh,
populating it. Idempotent (inspect-before-add) like 0028–0032.

Revision ID: 0033_chapter_content_raw_checksum
Revises: 0032_cover_url_partial_index
Create Date: 2026-06-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033_chapter_content_raw_checksum"
down_revision: Union[str, None] = "0032_cover_url_partial_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "chapter_contents" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("chapter_contents")}
    if "raw_checksum" not in cols:
        op.add_column("chapter_contents", sa.Column("raw_checksum", sa.String(64), nullable=True))


def downgrade() -> None:
    try:
        op.drop_column("chapter_contents", "raw_checksum")
    except Exception:  # noqa: BLE001
        pass
