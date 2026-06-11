"""Tiered backup/restore: a fresh install can import a backup and resume without re-gathering."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlalchemy import delete

from app import backup as B
from app.db import SessionLocal, init_db
from app.models import (
    AppSetting, Bookshelf, BookshelfItem, Chapter, ChapterContent, CrawlJob, Integration,
    LibraryItem, MetadataLink, ReadingState, Source, User, UserSession, Work,
)


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    for m in (ReadingState, BookshelfItem, Bookshelf, LibraryItem, MetadataLink, CrawlJob,
              Chapter, ChapterContent, Integration, AppSetting, Work, UserSession, Source, User):
        s.execute(delete(m))
    s.commit()
    yield s
    s.close()


def _seed(db) -> dict:
    user = User(username="admin", password_hash="hash", role="admin")
    db.add(user); db.commit(); db.refresh(user)
    db.add(AppSetting(key="crawl_tuning", value={"refresh_hours": 6, "parallel_fetches": 3}))
    db.add(Integration(kind="googlebooks", name="Google Books", base_url="https://gb", enabled=True,
                       api_key="SECRET_KEY", config={"x": 1}))
    src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                 tos_permitted=True, config={"token": "abc"})
    db.add(src); db.commit(); db.refresh(src)
    w = Work(source_id=src.id, source_work_ref="https://s/n", title="Test Novel", hooked=True,
             status="ongoing", total_chapters_known=3, total_chapters_expected=3, start_chapter=1)
    db.add(w); db.commit(); db.refresh(w)
    # two fetched chapters WITH content + one pending (the live frontier). Mirror the engine's
    # creation order: chapter first (for its id), then its content, then link content_id.
    ch1 = Chapter(work_id=w.id, index=1, source_chapter_ref="r1", title="Ch1",
                  fetch_status="fetched")
    ch2 = Chapter(work_id=w.id, index=2, source_chapter_ref="r2", title="Ch2",
                  fetch_status="fetched")
    ch3 = Chapter(work_id=w.id, index=3, source_chapter_ref="r3", title="Ch3",
                  fetch_status="pending")
    db.add_all([ch1, ch2, ch3]); db.commit(); db.refresh(ch1); db.refresh(ch2)
    c1 = ChapterContent(chapter_id=ch1.id, format="html", body="<p>chapter one body</p>",
                        word_count=3, checksum="a")
    c2 = ChapterContent(chapter_id=ch2.id, format="html", body="<p>chapter two body</p>",
                        word_count=3, checksum="b")
    db.add_all([c1, c2]); db.commit()
    ch1.content_id = c1.id; ch2.content_id = c2.id; db.commit(); db.refresh(ch1)
    db.add(LibraryItem(user_id=user.id, work_id=w.id))
    db.add(CrawlJob(work_id=w.id, kind="backfill", status="scheduled", cursor={"next_index": 3}))
    db.add(MetadataLink(work_id=w.id, provider="googlebooks", ref="gb1", confidence=1.0,
                        status="auto", total_units=320, unit_kind="pages"))
    db.add(ReadingState(user_id=user.id, work_id=w.id, last_chapter_id=ch1.id,
                        scroll_fraction=0.5))
    db.commit()
    return {"work_id": w.id, "ch1": ch1.id}


def _counts(db) -> dict:
    from sqlalchemy import func, select
    out = {}
    for m in (User, Source, Integration, AppSetting, Work, Chapter, ChapterContent,
              LibraryItem, CrawlJob, MetadataLink, ReadingState):
        out[m.__tablename__] = db.scalar(select(func.count()).select_from(m)) or 0
    return out


def _roundtrip(db, level: str) -> dict:
    """Export at ``level``, wipe, import — returns post-import counts."""
    tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
    try:
        B.export_archive(db, level, tmp)
        B.wipe_database(db)
        assert B.database_is_empty(db)
        B.import_archive(db, tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return _counts(db)


def test_data_level_roundtrip_preserves_everything(db):
    _seed(db)
    before = _counts(db)
    after = _roundtrip(db, "data")
    assert after == before  # whole DB restored exactly, including chapter content
    # Credentials/config survive so the instance works without reconfiguration.
    integ = db.query(Integration).filter_by(kind="googlebooks").first()
    assert integ.api_key == "SECRET_KEY"
    src = db.query(Source).first()
    assert src.config == {"token": "abc"}
    # Fetched chapters keep their content (no needless re-fetch) and the frontier is intact.
    fetched = db.query(Chapter).filter_by(fetch_status="fetched").count()
    assert fetched == 2
    assert db.query(CrawlJob).first().cursor == {"next_index": 3}


def test_settings_level_roundtrip_drops_content_and_resets_chapters(db):
    ids = _seed(db)
    after = _roundtrip(db, "settings")
    # Config + library structure + progress + frontier survive...
    assert after["users"] == 1 and after["works"] == 1 and after["library_items"] == 1
    assert after["chapters"] == 3 and after["crawl_jobs"] == 1 and after["metadata_links"] == 1
    # ...but text content is NOT carried (re-downloaded on the target).
    assert after["chapter_contents"] == 0
    # Every chapter with now-missing content was reset to pending so the backfill re-fetches it.
    assert db.query(Chapter).filter_by(fetch_status="fetched").count() == 0
    assert db.query(Chapter).filter_by(fetch_status="pending").count() == 3
    for ch in db.query(Chapter).all():
        assert ch.content_id is None
    # Reading progress still points at a real (resurrected) chapter row.
    rs = db.query(ReadingState).first()
    assert rs.last_chapter_id == ids["ch1"]


def test_login_sessions_are_not_exported(db):
    user = User(username="admin", password_hash="h", role="admin")
    db.add(user); db.commit(); db.refresh(user)
    from datetime import UTC, datetime, timedelta
    db.add(UserSession(user_id=user.id, token="secret-token",
                       expires_at=datetime.now(UTC) + timedelta(days=1)))
    db.commit()
    tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
    try:
        import zipfile
        B.export_archive(db, "settings", tmp)
        with zipfile.ZipFile(tmp) as zf:
            assert "data/user_sessions.jsonl" not in zf.namelist()
    finally:
        tmp.unlink(missing_ok=True)


def test_backup_order_covers_every_table():
    """Every persistent table must be in _ORDER (or be the intentionally-excluded session table),
    so a newly-added model can't silently vanish from backups."""
    from app.db import Base
    import app.models  # noqa: F401 — ensure all models are registered
    all_tables = set(Base.metadata.tables.keys())
    covered = {m.__tablename__ for m in B._ORDER} | {"user_sessions"}
    assert all_tables <= covered, f"tables missing from backup _ORDER: {sorted(all_tables - covered)}"


def test_streamed_archive_is_valid_and_roundtrips(db):
    """The on-the-fly streaming path (non-seekable zip → data-descriptor entries) must produce a
    zip that imports identically to the build-to-disk path — that's what the download endpoint now
    serves, so a restore from a streamed backup has to work."""
    import io
    import zipfile
    _seed(db)
    before = _counts(db)
    data = b"".join(B.stream_archive("data"))
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.testzip() is None                       # every entry decompresses (incl. data descriptors)
    assert "manifest.json" in zf.namelist()
    tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
    try:
        tmp.write_bytes(data)
        B.wipe_database(db)
        assert B.database_is_empty(db)
        B.import_archive(db, tmp)
    finally:
        tmp.unlink(missing_ok=True)
    assert _counts(db) == before


def test_backup_endpoint_streams_zip_and_is_single_flight(db):
    """The download endpoint streams a valid zip, and while one backup is building a concurrent
    request is refused with 409 (the single-flight guard that stops retry storms)."""
    import io
    import zipfile

    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import backup as br

    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "hunter2pw"})
        r = c.get("/api/admin/backup", params={"level": "settings"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/zip")
        assert "attachment" in r.headers.get("content-disposition", "")
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert zf.testzip() is None and "manifest.json" in zf.namelist()
        # A download already in progress → the next request is cleanly refused, not piled on.
        assert br._STREAM_LOCK.acquire(blocking=False)
        try:
            assert c.get("/api/admin/backup", params={"level": "settings"}).status_code == 409
        finally:
            br._STREAM_LOCK.release()
        # Lock freed → downloads work again (no leaked lock).
        assert c.get("/api/admin/backup", params={"level": "settings"}).status_code == 200


def test_restore_sections_cover_every_table():
    """Every exportable table must belong to exactly one restore section, so the interactive
    restore can never silently omit (or double-count) a table."""
    section_tables = [t for sec in B.SECTIONS for t in sec["tables"]]
    assert len(section_tables) == len(set(section_tables)), "a table is in two sections"
    covered = set(section_tables) | {"user_sessions"}  # sessions are never exported
    all_tables = {m.__tablename__ for m in B._ORDER}
    assert all_tables <= covered, f"tables missing from a restore section: {sorted(all_tables - covered)}"


def _stage(db, level: str) -> Path:
    tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
    B.export_archive(db, level, tmp)
    return tmp


def test_selective_restore_skip_preserves_target_config(db):
    """The migration case: restore the library from a backup WITHOUT clobbering the target's own
    integrations / settings. Skipped sections are left exactly as they are."""
    _seed(db)
    backup = _stage(db, "data")
    try:
        # Target now diverges: a different integration + a changed setting + an extra work.
        B.wipe_database(db)
        db.add(User(username="admin", password_hash="h", role="admin")); db.commit()
        db.add(Integration(kind="readarr", name="My Readarr", base_url="http://r", enabled=True,
                           api_key="KEEP_ME"))
        db.add(AppSetting(key="crawl_tuning", value={"refresh_hours": 99}))
        src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                     tos_permitted=True)
        db.add(src); db.commit(); db.refresh(src)
        db.commit()
        # Restore ONLY the library; skip accounts/settings/integrations/sources/catalog/acquisition.
        B.import_selective(db, backup, {"library": "merge", "sources": "skip",
                                        "integrations": "skip", "settings": "skip",
                                        "accounts": "skip", "catalog": "skip", "acquisition": "skip"})
    finally:
        backup.unlink(missing_ok=True)
    # The target's integration + setting are untouched (not overwritten by the backup's).
    assert db.query(Integration).filter_by(kind="readarr").first().api_key == "KEEP_ME"
    assert db.query(Integration).filter_by(kind="googlebooks").first() is None  # backup's not imported
    assert db.get(AppSetting, "crawl_tuning").value == {"refresh_hours": 99}
    # ...but the library DID come in.
    assert db.query(Work).filter_by(title="Test Novel").first() is not None
    assert db.query(Chapter).count() == 3


def test_selective_restore_merge_vs_replace(db):
    """merge keeps existing rows on a PK clash; replace clears the section first."""
    _seed(db)
    backup = _stage(db, "data")
    try:
        # Rename the existing work in place (same PK as in the backup).
        w = db.query(Work).first()
        w.title = "Local Edit"; db.commit()
        # merge: PK already present → backup row ignored, local edit kept.
        B.import_selective(db, backup, {"library": "merge"})
        assert db.query(Work).first().title == "Local Edit"
        # replace: section cleared then reloaded → backup's title wins.
        B.import_selective(db, backup, {"library": "replace"})
        assert db.query(Work).first().title == "Test Novel"
    finally:
        backup.unlink(missing_ok=True)


def test_backups_store_upload_list_plan_restore_delete(db, tmp_path, monkeypatch):
    """End-to-end over HTTP via the backups store: upload an external backup so it's a selectable
    object, see it listed, get its plan, restore chosen sections by name, then delete it."""
    import io

    from fastapi.testclient import TestClient

    from app import backups_store as store
    from app.main import app

    monkeypatch.setattr(store, "backups_dir", lambda: tmp_path)  # isolate the store to a temp dir
    _seed(db)
    blob = b"".join(B.stream_archive("data"))
    B.wipe_database(db)
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "hunter2pw"})
        # Upload an externally-made backup → it joins the store as a selectable object.
        r = c.post("/api/admin/backups/upload",
                   files={"file": ("from-other-vm.zip", io.BytesIO(blob), "application/zip")})
        assert r.status_code == 200, r.text
        name = r.json()["name"]
        assert r.json()["origin"] == "uploaded" and r.json()["restorable"]
        # It shows up in the listing.
        listing = c.get("/api/admin/backups").json()
        assert any(b["name"] == name for b in listing["backups"]) and "free_bytes" in listing
        # Plan by name (reads the manifest; changes nothing).
        plan = c.get(f"/api/admin/backups/{name}/plan").json()
        keys = {s["key"] for s in plan["sections"]}
        assert {"library", "integrations", "settings"} <= keys
        # Restore the library only, by name.
        modes = {k: ("merge" if k == "library" else "skip") for k in keys}
        r2 = c.post("/api/admin/restore/commit", json={"name": name, "sections": modes})
        assert r2.status_code == 200, r2.text
        assert r2.json()["restored"] is True
        # Delete it from the store.
        assert c.delete(f"/api/admin/backups/{name}").status_code == 200
        assert not any(b["name"] == name for b in c.get("/api/admin/backups").json()["backups"])
        # A traversal-y name is rejected, and an unknown one 404s.
        assert c.get("/api/admin/backups/..%2f..%2fetc/plan").status_code in (400, 404)
        assert c.post("/api/admin/restore/commit",
                      json={"name": "nope.zip", "sections": {}}).status_code == 404
    assert db.query(Work).filter_by(title="Test Novel").first() is not None


def test_restore_tolerates_column_drift(db):
    """A backup from a different app version restores cleanly: an unknown column it carries is
    dropped, and a column this version added that the backup lacks falls back to its default."""
    import io
    import json
    import zipfile
    _seed(db)
    src = zipfile.ZipFile(io.BytesIO(b"".join(B.stream_archive("settings"))))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for info in src.infolist():
            raw = src.read(info.filename)
            if info.filename == "data/users.jsonl":
                rewritten = []
                for line in raw.decode().splitlines():
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    r.pop("is_active", None)               # older backup: column didn't exist yet
                    r["bogus_future_col"] = "ignore me"    # newer backup: column we don't know
                    rewritten.append(json.dumps(r))
                raw = ("\n".join(rewritten) + "\n").encode()
            zf.writestr(info, raw)
    tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
    tmp.write_bytes(out.getvalue())
    try:
        B.wipe_database(db)
        B.import_selective(db, tmp, {sec["key"]: "replace" for sec in B.SECTIONS})
    finally:
        tmp.unlink(missing_ok=True)
    u = db.query(User).filter_by(username="admin").first()
    assert u is not None
    assert u.is_active is True  # the omitted column took its model default


def test_restore_rolls_back_on_error(db):
    """If the restore fails partway, the database is left exactly as it was — no half-applied
    replace (the delete is rolled back too)."""
    import io
    import json
    import zipfile
    _seed(db)
    good = b"".join(B.stream_archive("data"))           # backup made BEFORE the sentinel exists
    db.add(Integration(kind="sentinel", name="keep-me", base_url="http://x", api_key="k",
                       enabled=True))
    db.commit()
    users_before = db.query(User).count()
    # Corrupt a late child table so the load throws after earlier tables were staged in the txn.
    src = zipfile.ZipFile(io.BytesIO(good))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for info in src.infolist():
            raw = src.read(info.filename)
            if info.filename == "data/reading_states.jsonl":
                raw = b"{ not valid json at all }\n"
            zf.writestr(info, raw)
    tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
    tmp.write_bytes(out.getvalue())
    try:
        with pytest.raises(Exception):
            B.import_selective(db, tmp, {sec["key"]: "replace" for sec in B.SECTIONS})
    finally:
        tmp.unlink(missing_ok=True)
    # Fully rolled back: the sentinel (which a "replace" deleted) is back, counts unchanged.
    assert db.query(Integration).filter_by(kind="sentinel").first() is not None
    assert db.query(User).count() == users_before


def test_import_rejects_newer_schema(db):
    import json
    import zipfile
    tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
    try:
        with zipfile.ZipFile(tmp, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"schema_version": B.SCHEMA_VERSION + 1,
                                                     "level": "data"}))
        with pytest.raises(ValueError, match="newer"):
            B.import_archive(db, tmp)
    finally:
        tmp.unlink(missing_ok=True)
