"""Whole-DB snapshot restore: the boot-time file swap (db.apply_pending_restore) + the store helpers
(backups_store.list_db_snapshots / request_db_restore / delete_db_snapshot)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import backups_store as store
from app import db as dbmod


def _make_sqlite(path: Path, marker_table: str) -> None:
    """A real (tiny) SQLite file so the header check passes; marker_table identifies which one."""
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE {marker_table} (x INTEGER)")
    con.commit()
    con.close()


@pytest.fixture
def dbdir(tmp_path, monkeypatch):
    live = tmp_path / "shelf.db"
    _make_sqlite(live, "live_db")
    # Point both modules at this throwaway dir.
    monkeypatch.setattr(store, "db_file", lambda: live)
    monkeypatch.setattr(dbmod, "settings", SimpleNamespace(database_url=f"sqlite:///{live}"))
    monkeypatch.setattr(dbmod, "_is_sqlite", True)
    monkeypatch.setattr(dbmod, "engine", SimpleNamespace(dispose=lambda: None))
    return tmp_path, live


def test_list_and_validate_snapshots(dbdir):
    tmp, live = dbdir
    _make_sqlite(tmp / "shelf.db.pre-x.bak", "snap_a")
    (tmp / "shelf.db.junk").write_text("not a database")          # listed but not restorable
    (tmp / "shelf.db-wal").write_bytes(b"\x00")                   # live wal — never listed
    (tmp / "unrelated.txt").write_text("x")                       # ignored entirely

    snaps = {s["name"]: s for s in store.list_db_snapshots()}
    assert "shelf.db.pre-x.bak" in snaps and snaps["shelf.db.pre-x.bak"]["restorable"] is True
    assert snaps["shelf.db.junk"]["restorable"] is False          # surfaced, but not a SQLite file
    assert "shelf.db" not in snaps and "shelf.db-wal" not in snaps and "unrelated.txt" not in snaps

    # Traversal / live-file names are rejected.
    for bad in ("../shelf.db", "shelf.db", "shelf.db-wal", "nope"):
        with pytest.raises(ValueError):
            store.snapshot_path(bad)


def test_request_restore_writes_marker_then_boot_swaps(dbdir):
    tmp, live = dbdir
    snap = tmp / "shelf.db.good-snapshot.bak"
    _make_sqlite(snap, "snapshot_db")

    # Staging refuses a non-SQLite file...
    (tmp / "shelf.db.bogus").write_text("nope")
    with pytest.raises(ValueError):
        store.request_db_restore("shelf.db.bogus")

    # ...and writes the boot marker for a valid one.
    marker = store.request_db_restore("shelf.db.good-snapshot.bak")
    assert marker.exists() and marker.read_text().strip() == str(snap)

    # Boot swap: live DB becomes the snapshot, the old DB is safety-copied, marker cleared.
    dbmod.apply_pending_restore()
    assert not marker.exists()
    con = sqlite3.connect(live)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "snapshot_db" in tables and "live_db" not in tables   # the swap happened
    assert list(tmp.glob("shelf.db.pre-restore-*.bak")), "previous DB was safety-copied"


def test_apply_pending_restore_noop_without_marker(dbdir):
    tmp, live = dbdir
    before = live.read_bytes()
    dbmod.apply_pending_restore()           # no marker → untouched
    assert live.read_bytes() == before


def test_delete_snapshot(dbdir):
    tmp, _ = dbdir
    _make_sqlite(tmp / "shelf.db.old.bak", "old")
    store.delete_db_snapshot("shelf.db.old.bak")
    assert not (tmp / "shelf.db.old.bak").exists()
