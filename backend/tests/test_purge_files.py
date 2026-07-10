"""Admin purge with delete_files removes the on-disk file, but never a path a live Work still uses."""
from __future__ import annotations

from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.library import purge_work
from app.models import LibraryItem, User, UserSession, Work


def _fresh():
    init_db()
    db = SessionLocal()
    for m in (LibraryItem, UserSession, Work, User):
        db.execute(delete(m))
    db.commit()
    return db


def test_purge_deletes_file(tmp_path):
    db = _fresh()
    f = tmp_path / "book.epub"
    f.write_bytes(b"data")
    w = Work(title="Del", media_kind="text", local_path=str(f))
    db.add(w); db.commit(); db.refresh(w)
    assert f.exists()
    purge_work(db, w, delete_files=True)
    assert not f.exists()          # file removed with the work
    db.close()


def test_purge_keeps_file_shared_with_live_work(tmp_path):
    db = _fresh()
    f = tmp_path / "shared.m4b"
    f.write_bytes(b"data")
    w1 = Work(title="A", media_kind="audio", local_path=str(f))
    w2 = Work(title="B", media_kind="audio", local_path=str(f))  # same underlying file
    db.add_all([w1, w2]); db.commit(); db.refresh(w1)
    purge_work(db, w1, delete_files=True)
    assert f.exists()              # w2 still points at it → protected
    db.close()


def test_purge_without_flag_leaves_file(tmp_path):
    db = _fresh()
    f = tmp_path / "keep.epub"
    f.write_bytes(b"data")
    w = Work(title="Keep", media_kind="text", local_path=str(f))
    db.add(w); db.commit(); db.refresh(w)
    purge_work(db, w)              # default: DB-only, file stays
    assert f.exists()
    db.close()
