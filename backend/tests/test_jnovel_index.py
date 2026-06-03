"""j-novel.club index efficiency: reader pages (/read/<slug>) are content-less dead-ends; the
crawler must collapse them to their /series/<slug> work landing (or skip), not fetch each one —
the fix for hitting 20-30 stale reader requests before a single title is found."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import adapters  # noqa: F401 — registers built-in adapters
from app.ingestion import indexer
from app.ingestion.extract import is_chapter_url, is_work_url, work_url_for
from app.models import AppSetting, IndexedPage, IndexSite


# ----------------------------------------------------------------- URL classification
@pytest.mark.parametrize("read,series", [
    ("https://j-novel.club/read/the-misfit-of-demon-king-academy-volume-12-act-1-part-1",
     "https://j-novel.club/series/the-misfit-of-demon-king-academy"),
    ("https://j-novel.club/read/reborn-as-a-vending-machine-volume-1-prologue",
     "https://j-novel.club/series/reborn-as-a-vending-machine"),
    ("https://j-novel.club/read/some-title-volume-3-part-2",
     "https://j-novel.club/series/some-title"),
    # Series name itself ends in a chaptery '-manga-part-2' — still a distinct work, collapsed
    # at the first -volume- marker (not the name's '-part-2').
    ("https://j-novel.club/read/ascendance-of-a-bookworm-manga-part-2-volume-1-chapter-1",
     "https://j-novel.club/series/ascendance-of-a-bookworm-manga-part-2"),
])
def test_jnovel_read_collapses_to_clean_series(read, series):
    assert is_chapter_url(read) is True      # always a reader page, never a work
    assert is_work_url(read) is False
    assert work_url_for(read) == series      # clean series slug (act/prologue/part all handled)


def test_jnovel_series_is_a_work():
    u = "https://j-novel.club/series/the-misfit-of-demon-king-academy"
    assert is_work_url(u) is True and is_chapter_url(u) is False


def test_jnovel_series_with_chaptery_name_is_still_a_work():
    # '-manga-part-2' is part of the SERIES name; the /series/ path makes it a work regardless.
    u = "https://j-novel.club/series/ascendance-of-a-bookworm-manga-part-2"
    assert is_chapter_url(u) is False and is_work_url(u) is True


# ----------------------------------------------------------------- discovery (_smart_targets)
def test_smart_targets_collapses_reads_and_skips_deadends():
    html = """
      <a href="/read/cool-novel-volume-1-part-1">p1</a>
      <a href="/read/cool-novel-volume-1-part-2">p2</a>
      <a href="/read/cool-novel-volume-2-act-1-part-1">p3</a>
      <a href="/series/another-novel">series</a>
      <a href="/read/no-volume-marker-here">weird</a>
    """
    targets = indexer._smart_targets(html, "https://j-novel.club/series/cool-novel",
                                     "j-novel.club", True)
    # All three /read/ parts collapse to the ONE series page (deduped), not three reader fetches.
    assert "https://j-novel.club/series/cool-novel" in targets
    assert "https://j-novel.club/series/another-novel" in targets
    # No raw /read/ URLs are queued, and the un-collapsible reader page is dropped, not crawled.
    assert not any("/read/" in u for u in targets)
    assert "https://j-novel.club/read/no-volume-marker-here" not in targets


# ----------------------------------------------------------------- backlog reclaim
@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    s.execute(delete(IndexedPage))
    s.execute(delete(IndexSite))
    s.execute(delete(AppSetting).where(AppSetting.key == indexer._DEADEND_RECLAIM_KEY))
    s.commit()
    yield s
    s.close()


def test_reclaim_collapses_pending_reader_deadends(db):
    site = IndexSite(root_url="https://j-novel.club", domain="j-novel.club", status="active")
    db.add(site)
    db.commit()
    db.refresh(site)
    db.add_all([
        IndexedPage(site_id=site.id, url="https://j-novel.club/read/aaa-volume-1-part-1",
                    status="pending"),
        IndexedPage(site_id=site.id, url="https://j-novel.club/read/aaa-volume-1-part-2",
                    status="pending"),
        IndexedPage(site_id=site.id, url="https://j-novel.club/read/bbb-volume-2-act-1-part-3",
                    status="fetched", word_count=0),
        IndexedPage(site_id=site.id, url="https://j-novel.club/series/ccc", status="fetched"),
    ])
    db.commit()

    res = indexer.reclaim_reader_deadends(db)
    assert res["ran"] is True

    by_url = {p.url: p for p in db.scalars(select(IndexedPage)).all()}
    # Pending reader dead-ends are marked skipped (no longer fetched).
    assert by_url["https://j-novel.club/read/aaa-volume-1-part-1"].status == "skipped"
    assert by_url["https://j-novel.club/read/aaa-volume-1-part-2"].status == "skipped"
    # Their series landings are enqueued as pending so the titles get discovered.
    assert by_url["https://j-novel.club/series/aaa"].status == "pending"
    assert by_url["https://j-novel.club/series/bbb"].status == "pending"  # from the fetched read
    # A second run is a no-op (sentinel gate).
    assert indexer.reclaim_reader_deadends(db)["ran"] is False
