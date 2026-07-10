"""Authorized, backed-up prod cleanup for the deep-review P1 data-integrity findings.

Run via the app's OWN deletion paths (never raw ad-hoc DELETEs):
  P1-1  stale metadata_links — links left pointing at RECYCLED work ids by the works-table rebuild
        (detected OFFLINE by re-scoring the stored matched_title vs the work's CURRENT title).
  P1-2/P2-1  bogus catalog rows — Gutenberg "Readers also downloaded" /also nav-chrome (1,533) and
        boilerplate-titled junk (test/untitled/…) — deleted via catalog._delete_catalog_entry
        (which also removes the landing page + FTS rows), then their now-empty groups dropped.

Safety: refuses to run unless a pre-cleanup .bak exists; prints the absolute prod DB path; DRY-RUN by
default (read-only) — pass --commit to mutate. Only touches NON-hooked rows (library works untouched).
"""
import glob
import os
import sys

from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import CatalogGroup, CatalogWork, MetadataLink, Work
from app.integrations.metadata import ProviderMatch
from app.integrations.metadata_sync import MATCH_THRESHOLD, _confidence

COMMIT = "--commit" in sys.argv
_BOGUS_KEYS = ("readers also downloaded", "test", "untitled", "prologue",
               "epilogue", "contents", "table of contents")


def main() -> None:
    dbpath = os.path.abspath(get_settings().database_url.split("///")[-1])
    print(f"PROD DB: {dbpath}")
    baks = glob.glob(os.path.join(os.path.dirname(dbpath) or ".", "shelf.db.pre-catalog-cleanup-*.bak"))
    if not baks:
        sys.exit("REFUSING: no shelf.db.pre-catalog-cleanup-*.bak backup found next to the DB.")
    print(f"backup present: {sorted(baks)[-1]}")

    db = SessionLocal()
    from app.ingestion.catalog import _delete_catalog_entry

    # 1) Stale metadata links (offline re-score).
    stale_ids: list[int] = []
    for lk in db.scalars(select(MetadataLink)).all():
        w = db.get(Work, lk.work_id)
        if w is None:
            stale_ids.append(lk.id)
            continue
        if lk.matched_title and _confidence(
                w.title, w.author, ProviderMatch(title=lk.matched_title, ref=lk.ref),
                w.media_kind) < MATCH_THRESHOLD:
            stale_ids.append(lk.id)

    # 2) Bogus catalog entries — NON-hooked only (never touch a hooked/library work).
    also = db.scalars(select(CatalogWork).where(
        CatalogWork.work_url.like("%/ebooks/%/also"),
        CatalogWork.hooked_work_id.is_(None))).all()
    bogus_group_ids = db.scalars(select(CatalogGroup.id).where(CatalogGroup.norm_key.in_(_BOGUS_KEYS))).all()
    boiler = db.scalars(select(CatalogWork).where(
        CatalogWork.group_id.in_(bogus_group_ids),
        CatalogWork.author.is_(None),
        CatalogWork.hooked_work_id.is_(None))).all() if bogus_group_ids else []

    print(f"\nWOULD DELETE:\n  stale metadata_links: {len(stale_ids)}"
          f"\n  catalog '/also' rows: {len(also)}"
          f"\n  boilerplate catalog rows (null author, not hooked): {len(boiler)}"
          f"\n  candidate bogus groups: {len(bogus_group_ids)}")

    if not COMMIT:
        print("\nDRY RUN — re-run with --commit to apply.")
        return

    for lid in stale_ids:
        obj = db.get(MetadataLink, lid)
        if obj is not None:
            db.delete(obj)
    db.commit()

    seen: set[int] = set()
    for e in [*also, *boiler]:
        if e.id in seen:
            continue
        seen.add(e.id)
        _delete_catalog_entry(db, e)
    db.commit()

    # Drop bogus groups that are now empty (members deleted); leave any with surviving members.
    dropped = 0
    for gid in bogus_group_ids:
        n = db.scalar(select(func.count()).select_from(CatalogWork).where(CatalogWork.group_id == gid))
        if n == 0:
            g = db.get(CatalogGroup, gid)
            if g is not None:
                db.delete(g)
                dropped += 1
    db.commit()

    from app import cache
    cache.clear_catalog()
    print(f"\nCOMMITTED: deleted {len(stale_ids)} links, {len(seen)} catalog rows, {dropped} empty groups.")
    db.close()


if __name__ == "__main__":
    main()
