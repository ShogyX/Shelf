"""B2: a refresh must NOT re-localize (re-fetch) a chapter whose content is unchanged.

``store_chapter_content`` localizes every remote <img> on each ingest, which re-downloads them.
On the ~12h refresh that re-fetches every image even when nothing changed (and comic CDN URLs
rotate, so the content-addressed cache misses). Verify: identical re-ingest skips
``localize_html_images`` and leaves the stored body untouched; changed content re-localizes.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app import imagecache
from app.db import SessionLocal, init_db
from app.ingestion import engine
from app.ingestion.base import RawChapter
from app.models import Chapter, ChapterContent, Work


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    s.execute(delete(ChapterContent))
    s.execute(delete(Chapter))
    s.execute(delete(Work))
    s.commit()
    yield s
    s.close()


def _make_chapter(db) -> Chapter:
    w = Work(title="Comic", media_kind="comic")
    db.add(w)
    db.flush()
    ch = Chapter(work_id=w.id, index=1, fetch_status="pending")
    db.add(ch)
    db.commit()
    db.refresh(ch)
    return ch


def test_unchanged_reingest_skips_localize_and_keeps_body(db, monkeypatch):
    ch = _make_chapter(db)
    # Localize rewrites the remote src to a local cache path (what a real fetch would produce).
    calls = {"n": 0}

    def fake_localize(html, base_url=""):
        calls["n"] += 1
        return html.replace("https://cdn.example/p1.jpg", "/media/imgcache/abc123.jpg")

    monkeypatch.setattr(imagecache, "localize_html_images", fake_localize)

    raw = RawChapter(title="Ch1", body='<p>x</p><img src="https://cdn.example/p1.jpg">')
    assert engine.store_chapter_content(db, ch, raw) == engine.STORED
    assert calls["n"] == 1
    stored_body = ch.content.body
    assert "/media/imgcache/abc123.jpg" in stored_body
    assert ch.content.raw_checksum is not None

    # Re-ingest the SAME raw content (e.g. the 12h refresh): must NOT localize again, and the
    # already-localized body + checksum must be untouched.
    raw2 = RawChapter(title="Ch1", body='<p>x</p><img src="https://cdn.example/p1.jpg">')
    assert engine.store_chapter_content(db, ch, raw2) == engine.UNCHANGED
    assert calls["n"] == 1  # localize was NOT called a second time
    assert ch.content.body == stored_body


def test_changed_content_relocalizes(db, monkeypatch):
    ch = _make_chapter(db)
    calls = {"n": 0}

    def fake_localize(html, base_url=""):
        calls["n"] += 1
        return html.replace("https://cdn.example/", "/media/imgcache/")

    monkeypatch.setattr(imagecache, "localize_html_images", fake_localize)

    raw = RawChapter(title="Ch1", body='<img src="https://cdn.example/p1.jpg">')
    assert engine.store_chapter_content(db, ch, raw) == engine.STORED
    assert calls["n"] == 1
    first_body = ch.content.body

    # Genuinely different page → must re-localize and re-store.
    raw2 = RawChapter(title="Ch1", body='<img src="https://cdn.example/p2.jpg">')
    assert engine.store_chapter_content(db, ch, raw2) == engine.STORED
    assert calls["n"] == 2
    assert ch.content.body != first_body
