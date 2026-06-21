"""Crawl must stop at the real end of a serial (no endless phantom chapters), reject placeholder
pages on refresh, and ad images must never be stored into chapter content."""
from __future__ import annotations

import asyncio

from sqlalchemy import delete, func, select

from app.db import SessionLocal, init_db
from app.ingestion import tracker
from app.ingestion.base import RawChapter
from app.ingestion.engine import DEAD_END, STORED, UNCHANGED, store_chapter_content
from app.models import Chapter, ChapterContent, Source, Work
from app.sanitize import sanitize_html


def _work(db, title="Serial", status="ongoing") -> Work:
    src = db.scalar(select(Source).where(Source.key == "generic_feed"))
    if src is None:
        src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                     tos_permitted=True)
        db.add(src)
        db.commit()
    # Unique ref per work — (source_id, source_work_ref) is now a unique index.
    ref = f"r{db.scalar(select(func.count()).select_from(Work)) or 0}"
    w = Work(source_id=src.id, source_work_ref=ref, title=title, hooked=True, status=status)
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def _chapter(db, work_id, index, ref, status="pending") -> Chapter:
    ch = Chapter(work_id=work_id, index=index, source_chapter_ref=ref,
                 title=f"Chapter {index}", fetch_status=status)
    db.add(ch)
    db.commit()
    db.refresh(ch)
    return ch


def _clean(db):
    for m in (ChapterContent, Chapter, Work):
        db.execute(delete(m))
    db.commit()


# ----------------------------------------------------------------- ad image filtering
def test_sanitize_drops_ad_images_keeps_real():
    html = (
        "<div><p>Real story paragraph.</p>"
        '<img src="https://cdn.example/series/ch1/page-1.jpg" alt="page"/>'
        '<img class="ad-banner" src="https://promo.example/x.jpg"/>'
        '<img src="https://pagead2.googlesyndication.com/x.gif"/>'
        '<img src="https://ads.example/ads/banner.png"/>'
        "</div>"
    )
    out = sanitize_html(html)
    assert "series/ch1/page-1.jpg" in out          # real illustration survives
    assert "promo.example" not in out              # class="ad-banner"
    assert "googlesyndication" not in out          # ad network
    assert "ads/banner.png" not in out             # /ads/ path
    assert out.count("<img") == 1


# ----------------------------------------------------------------- dead-end detection
def test_store_detects_duplicate_and_placeholder_dead_ends():
    init_db()
    db = SessionLocal()
    _clean(db)
    w = _work(db)
    body = "<p>" + ("word " * 200) + "</p>"

    ch1 = _chapter(db, w.id, 1, "c1")
    assert store_chapter_content(db, ch1, RawChapter(title="C1", body=body),
                                 detect_dead_end=True) == STORED

    # Identical content to ch1 → a 'next' link that loops back → dead-end (not stored).
    ch2 = _chapter(db, w.id, 2, "c2")
    assert store_chapter_content(db, ch2, RawChapter(title="C2", body=body),
                                 detect_dead_end=True) == DEAD_END
    db.refresh(ch2)
    assert ch2.fetch_status == "skipped" and ch2.content_id is None

    # Near-empty image-less page, with real content already present → placeholder dead-end.
    ch3 = _chapter(db, w.id, 3, "c3")
    assert store_chapter_content(db, ch3, RawChapter(title="C3", body="<p>soon</p>"),
                                 detect_dead_end=True) == DEAD_END
    db.refresh(ch3)
    assert ch3.fetch_status == "skipped"
    db.close()


def test_short_first_chapter_is_not_a_dead_end():
    """A genuinely short first page on a brand-new work must not be mistaken for the end."""
    init_db()
    db = SessionLocal()
    _clean(db)
    w = _work(db)
    ch = _chapter(db, w.id, 1, "c1")
    assert store_chapter_content(db, ch, RawChapter(title="C1", body="<p>short start</p>"),
                                 detect_dead_end=True) == STORED
    db.close()


def test_detect_dead_end_off_stores_normally():
    """Hooking a single indexed page (no dead-end detection) stores even short content."""
    init_db()
    db = SessionLocal()
    _clean(db)
    w = _work(db)
    _chapter(db, w.id, 1, "c1", status="fetched")  # pre-existing real content sibling
    db.add(ChapterContent(chapter_id=w.chapters[0].id, format="html", body="<p>x</p>",
                          word_count=1, checksum="seed"))
    db.commit()
    ch = _chapter(db, w.id, 2, "c2")
    assert store_chapter_content(db, ch, RawChapter(title="C2", body="<p>tiny</p>")) == STORED
    db.close()


# ----------------------------------------------------------------- reseed bounding
def test_reseed_bounded_at_frontier():
    init_db()
    db = SessionLocal()
    _clean(db)

    # Completed serials never speculate.
    done = _work(db, title="Done", status="complete")
    _chapter(db, done.id, 1, "d1", status="fetched")
    assert asyncio.run(tracker._reseed_sequential(db, done, None, set())) == 0

    # A skipped frontier is RE-PROBED (reset to pending), not extended to a new phantom index.
    w = _work(db, title="Ongoing")
    skipped = _chapter(db, w.id, 5, "c5", status="skipped")
    assert asyncio.run(tracker._reseed_sequential(db, w, None, set())) == 1
    db.commit()  # the real caller (discover_updates) commits the reseed
    db.refresh(skipped)
    assert skipped.fetch_status == "pending"
    # Now the frontier is pending → don't pile on another speculative chapter.
    assert asyncio.run(tracker._reseed_sequential(db, w, None, {"c5"})) == 0
    db.close()


def test_speculative_reprobe_does_not_inflate_total():
    """I7: a refresh that only RE-PROBES a skipped frontier must not ratchet total_chapters_known
    up (it oscillated +1 every refresh under the old count)."""
    init_db(); db = SessionLocal(); _clean(db)

    from app.ingestion.base import WorkMeta

    class _Adapter:
        async def discover_work(self, ref):
            return WorkMeta(source_work_ref=ref, title="Ongoing", status="ongoing")
        async def list_chapters(self, meta):
            return []                                  # TOC reveals nothing new → triggers re-probe

    w = _work(db, title="Ongoing")
    _chapter(db, w.id, 1, "c1", status="fetched")
    _chapter(db, w.id, 2, "c2", status="skipped")     # dead-end frontier
    w.total_chapters_known = 1
    db.commit()
    # First refresh re-probes c2 (skipped→pending) but must NOT bump the known total.
    added, _changed = asyncio.run(tracker.discover_updates(db, w, _Adapter()))
    db.commit(); db.refresh(w)
    assert added >= 1 and w.total_chapters_known == 1   # re-probe happened, total unchanged
    db.close()


def test_backfill_skips_dead_end_when_more_chapters_already_listed(monkeypatch):
    """A PRE-LISTED source (e.g. Gutenberg) scatters placeholder/divider sections among real chapters.
    A dead-end must NOT finalize the backfill when more chapters are already listed beyond it — the run
    advances to the next pending chapter instead of marking 'done' (which forced a ~2-min reaper cycle
    per placeholder, crawling such books at ~1 chapter / 2 min)."""
    from app.ingestion import scheduler
    from app.models import CrawlJob
    init_db(); db = SessionLocal(); _clean(db)
    db.execute(delete(CrawlJob)); db.commit()
    w = _work(db, title="Gutenberg Book")
    w.crawl_interval_s = 1.0            # per_request → batch of 1
    w.total_chapters_expected = 3
    db.commit()
    _chapter(db, w.id, 1, "g#1")        # front-matter / divider → dead-end, but NOT the end
    _chapter(db, w.id, 2, "g#2")        # real chapters, still listed beyond the dead-end
    _chapter(db, w.id, 3, "g#3")
    job = CrawlJob(work_id=w.id, kind="backfill", status="scheduled", cursor={"next_index": 1})
    db.add(job); db.commit(); db.refresh(job)

    class _A:
        async def fetch_chapter(self, ref):
            return RawChapter(title="x", body="<p>x</p>")
    monkeypatch.setattr(scheduler, "adapter_for", lambda source: _A())

    def _store(_db, ch, _raw, detect_dead_end=False):   # ch 1 reads as a placeholder
        ch.fetch_status = "skipped"
        return DEAD_END
    monkeypatch.setattr(scheduler, "store_chapter_content", _store)

    asyncio.run(scheduler._process_job(db, job))

    db.refresh(job)
    assert job.status != "done", f"backfill wrongly finalized on a mid-list dead-end ({job.status})"
    c1 = db.scalar(select(Chapter).where(Chapter.work_id == w.id, Chapter.index == 1))
    assert c1.fetch_status == "skipped"                  # dead-end retired
    pend = db.scalar(select(func.count(Chapter.id)).where(
        Chapter.work_id == w.id, Chapter.fetch_status == "pending"))
    assert pend == 2, pend                               # ch 2 & 3 still queued, not abandoned
    db.close()
