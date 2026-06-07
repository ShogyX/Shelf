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
