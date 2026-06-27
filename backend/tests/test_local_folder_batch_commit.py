"""A folder scan larger than the commit batch must import every file correctly.

Regression for the watcher memory balloon: the scan now commits + ``expunge_all()`` every
``_SYNC_COMMIT_EVERY`` imports instead of holding the whole pass in one session/transaction. That
expunge detaches ``src`` and the ``folder`` arg, so the loop re-loads ``src`` and the tail re-attaches
``folder`` — this test guards that nothing is dropped, double-counted, or left uncommitted across the
batch boundary, and that a re-scan still skips everything (the unchanged gate survived).
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.ingestion import local_folder as LF
from app.ingestion.local_folder import _local_folder_adapter_cls, ensure_source, sync_folder
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


def test_scan_larger_than_batch_imports_all(db, tmp_path):
    n = LF._SYNC_COMMIT_EVERY * 2 + 7  # spans 3 batches incl. a partial tail
    for i in range(n):
        (tmp_path / f"book-{i:03d}.txt").write_text(f"Chapter one of book {i}.\nUnique body {i}.\n")
    src = ensure_source(db, _local_folder_adapter_cls())
    folder = WatchedFolder(path=str(tmp_path), display_name="t", recursive=False, enabled=True)
    db.add(folder)
    db.commit()
    fid = folder.id

    first = sync_folder(db, folder)
    assert first["added"] == n, first
    assert first["errors"] == 0
    # Every file is committed (a fresh session sees them — proves the batches flushed, not just buffered).
    other = SessionLocal()
    try:
        assert other.query(Work).filter(Work.source_id == src.id).count() == n
    finally:
        other.close()

    # The folder's tail bookkeeping still landed despite the mid-scan expunge (re-fetch: expunge_all
    # detached the original handle).
    folder = db.get(WatchedFolder, fid)
    assert folder.last_scan_at is not None
    assert folder.file_count == n

    # Re-scan: the unchanged (mtime,size) gate skips everything — no re-import, no churn.
    second = sync_folder(db, folder)
    assert second["added"] == 0 and second["updated"] == 0 and second["removed"] == 0, second


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
