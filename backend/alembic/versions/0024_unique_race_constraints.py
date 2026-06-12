"""Race-hardening unique constraints: dedupe pre-existing collisions, then enforce.

The historical check-then-insert patterns let concurrent writers create duplicate
StockItems (same norm_key), duplicate ACTIVE CrawlJobs (same work+kind), and duplicate
Works (same source ref — folder sync racing the download import). These indexes turn
those races into IntegrityErrors the insert sites now catch-and-reuse (db.insert_or_reuse).

Keys are deliberately scoped (see fix plan Section 12 guardrails):
  * crawl_jobs: (work_id, kind) WHERE active — NOT work_id alone; a work legitimately runs
    backfill + descramble (+ refresh) concurrently and descramble depends on a live backfill.
  * works: (source_id, source_work_ref) — NOT bare local_path; two watched folders covering
    the same file legitimately create two Works with distinct refs.
  * download_jobs: NO constraint — piggyback followers legitimately share catalog_work_id.

Mirrors the boot-time path (db.dedupe_unique_collisions + db.enforce_unique_indexes); both
converge on the same schema. Idempotent.

Revision ID: 0024_unique_race_constraints
Revises: 0023_fetch_verify
Create Date: 2026-06-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_unique_race_constraints"
down_revision: Union[str, None] = "0023_fetch_verify"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # --- dedupe FIRST (the unique creation fails if collisions remain) -----------------
    bind.execute(sa.text(
        "DELETE FROM stock_items WHERE id NOT IN ("
        " SELECT id FROM ("
        "  SELECT id, ROW_NUMBER() OVER (PARTITION BY norm_key"
        "   ORDER BY (status = 'stocked') DESC, id) AS rn FROM stock_items"
        " ) WHERE rn = 1)"
    ))
    bind.execute(sa.text(
        "DELETE FROM crawl_jobs WHERE status IN ('scheduled','running','paused')"
        " AND id NOT IN ("
        " SELECT id FROM ("
        "  SELECT id, ROW_NUMBER() OVER (PARTITION BY work_id, kind"
        "   ORDER BY (status = 'running') DESC, id) AS rn"
        "  FROM crawl_jobs WHERE status IN ('scheduled','running','paused')"
        " ) WHERE rn = 1)"
    ))
    # works duplicates carry user data — keep the row with the most chapters, repoint
    # library/shelf memberships, drop the spares (plain SQL mirror of stock._migrate_work_links).
    dups = bind.execute(sa.text(
        "SELECT source_id, source_work_ref FROM works"
        " WHERE source_id IS NOT NULL AND source_work_ref IS NOT NULL"
        " GROUP BY source_id, source_work_ref HAVING COUNT(*) > 1"
    )).all()
    for sid, ref in dups:
        rows = bind.execute(sa.text(
            "SELECT w.id, (SELECT COUNT(*) FROM chapters c WHERE c.work_id = w.id) AS n"
            " FROM works w WHERE w.source_id = :s AND w.source_work_ref = :r"
            " ORDER BY n DESC, w.id ASC"
        ), {"s": sid, "r": ref}).all()
        keep = rows[0][0]
        for wid, _n in rows[1:]:
            bind.execute(sa.text(
                "UPDATE OR IGNORE library_items SET work_id = :k WHERE work_id = :o"),
                {"k": keep, "o": wid})
            bind.execute(sa.text(
                "UPDATE OR IGNORE bookshelf_items SET work_id = :k WHERE work_id = :o"),
                {"k": keep, "o": wid})
            # chapter bodies first (linked through chapters), then the rows keyed by work_id.
            bind.execute(sa.text(
                "DELETE FROM chapter_contents WHERE chapter_id IN"
                " (SELECT id FROM chapters WHERE work_id = :o)"), {"o": wid})
            for table in ("library_items", "bookshelf_items", "reading_states",
                          "chapters"):
                bind.execute(sa.text(f"DELETE FROM {table} WHERE work_id = :o"), {"o": wid})
            bind.execute(sa.text("DELETE FROM works WHERE id = :o"), {"o": wid})

    # --- then enforce -------------------------------------------------------------------
    bind.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_norm_key ON stock_items (norm_key)"))
    bind.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_work_source_ref"
        " ON works (source_id, source_work_ref)"
        " WHERE source_id IS NOT NULL AND source_work_ref IS NOT NULL"))
    bind.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_crawl_active ON crawl_jobs (work_id, kind)"
        " WHERE status IN ('scheduled','running','paused')"))


def downgrade() -> None:
    for name in ("uq_crawl_active", "uq_work_source_ref", "uq_stock_norm_key"):
        op.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
