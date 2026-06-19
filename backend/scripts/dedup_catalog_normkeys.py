"""One-off maintenance: refresh stale catalog `norm_key`s, then regroup.

Root cause of duplicate catalog cards: ~1.4k rows carry a `norm_key` from an OLD normalizer that
stripped accents/CJK ("abel s nchez", "1001"), so the union-find's exact-key merge never collapses
them with their correctly-normalized twin ("abel sanchez"). This recomputes `norm_key` with the
CURRENT `norm_title` for every mismatched row, then forces one regroup so the duplicates merge.

Safe: only UPDATEs `norm_key` (no row deletes) + the standard, idempotent regroup. Backs up the DB
(consistent snapshot) first. Guarded to run only against the real backend DB.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime

# Guard: must run from the backend dir against the real DB (the prod-DB incident rule — no relative
# ./shelf.db surprises). Resolve + assert the absolute path before any write.
DB = os.path.abspath("shelf.db")
assert os.path.basename(os.getcwd()) == "backend" and os.path.exists(DB), (
    f"run from /root/Shelf/backend; DB not found at {DB}")

from app.db import SessionLocal, engine  # noqa: E402
from app.ingestion.catalog_groups import _WATERMARK_KEY, regroup_catalog  # noqa: E402
from app.ingestion.extract import norm_title  # noqa: E402
from sqlalchemy import text  # noqa: E402


_DUP_SQL = ("SELECT COUNT(*) FROM (SELECT 1 FROM catalog_groups "
            "GROUP BY lower(title), lower(coalesce(author,'')), media_label HAVING COUNT(*)>1)")


def main() -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup = f"{DB}.pre-normkeydedup-{stamp}.bak"
    # Consistent snapshot via stdlib sqlite3 (VACUUM INTO can't run inside SQLAlchemy's transaction).
    raw = sqlite3.connect(DB)
    before = raw.execute(_DUP_SQL).fetchone()[0]
    print(f"duplicate groups before: {before}")
    print(f"backing up -> {backup}")
    raw.execute("VACUUM INTO ?", (backup,))
    raw.close()
    assert os.path.exists(backup) and os.path.getsize(backup) > 0, "backup failed"

    db = SessionLocal()
    db.execute(text("PRAGMA busy_timeout=60000"))
    fixed = 0
    rows = db.execute(text("SELECT id, title, norm_key FROM catalog_works")).all()
    for rid, title, nk in rows:
        want = norm_title(title or "")
        if want != (nk or ""):
            db.execute(text("UPDATE catalog_works SET norm_key=:k WHERE id=:i"),
                       {"k": want, "i": rid})
            fixed += 1
    print(f"refreshed norm_key on {fixed} stale rows")
    # Force the next regroup (the watermark gate would otherwise see an unchanged signature).
    db.execute(text("DELETE FROM app_settings WHERE key=:k"), {"k": _WATERMARK_KEY})
    db.commit()

    print("regrouping…")
    summary = regroup_catalog(db, throttle=False)
    print("regroup:", summary)

    after = db.execute(text(_DUP_SQL)).scalar() or 0
    db.close()
    print(f"duplicate groups after: {after}  (removed {before - after})")


if __name__ == "__main__":
    sys.exit(main())
