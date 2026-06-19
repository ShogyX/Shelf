"""Two files with IDENTICAL bytes in a watched folder must not churn.

Regression: content-hash dedupe (13C) re-homed the single shared Work's ref/path to whichever
duplicate was scanned last, so every scan re-"added" both copies (perpetual writes → connection-pool
exhaustion on background ticks). The fix keeps the existing copy's Work and gives the duplicate its
own Work, so the second scan finds both by ref and skips them.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.ingestion.local_folder import (
    _local_folder_adapter_cls,
    ensure_source,
    sync_folder,
)
from app.models import WatchedFolder, Work


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    s.execute(delete(Work))
    s.execute(delete(WatchedFolder))
    s.commit()
    yield s
    s.close()


def test_duplicate_files_do_not_churn(db, tmp_path):
    # Two distinct files, identical bytes → same content_hash.
    (tmp_path / "copy-a.txt").write_text("Chapter one.\nIdentical content.\n")
    (tmp_path / "copy-b.txt").write_text("Chapter one.\nIdentical content.\n")
    src = ensure_source(db, _local_folder_adapter_cls())
    folder = WatchedFolder(path=str(tmp_path), display_name="t", recursive=False, enabled=True)
    db.add(folder)
    db.commit()
    db.refresh(folder)

    first = sync_folder(db, folder)
    assert first["added"] == 2 and first["errors"] == 0
    # Both copies persist as their OWN Work (no re-home stealing one ref).
    assert db.query(Work).filter(Work.source_id == src.id).count() == 2

    # The churn bug made this re-"add" both every scan; the fix elides them via the (mtime,size) skip.
    second = sync_folder(db, folder)
    assert second["added"] == 0, f"duplicate files re-imported: {second}"
    assert second["updated"] == 0 and second["removed"] == 0


def test_rename_still_dedupes_to_one_work(db, tmp_path):
    # A genuine rename (old file gone) must still adopt the SAME Work (13C), not create a duplicate.
    (tmp_path / "old-name.txt").write_text("Some unique book text.\n")
    src = ensure_source(db, _local_folder_adapter_cls())
    folder = WatchedFolder(path=str(tmp_path), display_name="t", recursive=False, enabled=True)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    sync_folder(db, folder)
    assert db.query(Work).filter(Work.source_id == src.id).count() == 1

    (tmp_path / "old-name.txt").rename(tmp_path / "new-name.txt")  # rename: old path now gone
    out = sync_folder(db, folder)
    # Re-homed onto the new file, removed the stale row → still exactly one Work, no churn build-up.
    assert db.query(Work).filter(Work.source_id == src.id).count() == 1, out
