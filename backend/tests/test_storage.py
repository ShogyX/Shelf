"""Admin storage-path overrides + the optional data migration (Settings → Storage)."""
from __future__ import annotations

from app import storage
from app.db import SessionLocal, init_db
from app.media import media_dir
from app.routers.settings import _migrate_dir, set_storage_ep


def test_override_and_revert(tmp_path):
    init_db()
    db = SessionLocal()
    storage.load(db)
    try:
        default = media_dir()
        target = tmp_path / "imgpool"
        storage.update(db, {"media_dir": str(target)})
        assert media_dir() == target            # override wins
        storage.update(db, {"media_dir": ""})    # blank reverts
        assert media_dir() == default
    finally:
        storage.update(db, {"media_dir": ""})
        db.close()


def test_migrate_moves_contents(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    (old / "comics" / "abc").mkdir(parents=True)
    (old / "comics" / "abc" / "01.webp").write_bytes(b"x" * 8)
    (old / "marker.txt").write_text("hi")
    # same filesystem → fast rename; verify files land under new and leave old.
    moved = _migrate_dir(str(old), str(new))
    assert moved == 2                                  # comics/ + marker.txt
    assert (new / "comics" / "abc" / "01.webp").is_file()
    assert (new / "marker.txt").read_text() == "hi"
    assert not (old / "marker.txt").exists()
    # skip-existing + no-op on same path
    assert _migrate_dir(str(new), str(new)) == 0


def test_put_storage_override_plus_migrate(tmp_path):
    init_db()
    db = SessionLocal()
    storage.load(db)
    try:
        old = tmp_path / "media_old"
        (old / "books").mkdir(parents=True)
        (old / "books" / "b.json").write_text("{}")
        storage.update(db, {"media_dir": str(old)})
        assert media_dir() == old
        new = tmp_path / "media_new"
        res = set_storage_ep({"media_dir": str(new), "migrate": True}, db)
        assert res["migrated"].get("media_dir")        # reported a move
        assert media_dir() == new                      # re-pointed
        assert (new / "books" / "b.json").is_file()     # data followed
    finally:
        storage.update(db, {"media_dir": ""})
        db.close()
