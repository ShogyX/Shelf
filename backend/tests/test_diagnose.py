"""Completeness diagnosis + self-repair for hooked works."""
from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.ingestion import diagnose
from app.models import Chapter, CrawlJob, Work

BASE = "https://s.test/novel/x/chapter/"


def _work(db, *, expected=None) -> Work:
    w = Work(title="X", total_chapters_expected=expected, hooked=True, status="ongoing")
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def _add(db, work, index, status="fetched"):
    db.add(Chapter(work_id=work.id, index=index, source_chapter_ref=f"{BASE}{index}",
                   title=f"Chapter {index}", fetch_status=status))
    db.commit()


def test_no_chapters_is_no_chapters():
    init_db()
    db = SessionLocal()
    w = _work(db)
    rep = diagnose.completeness(db, w)
    assert rep["health"] == "no_chapters"
    db.close()


def test_all_fetched_no_advertised_is_ok():
    init_db()
    db = SessionLocal()
    w = _work(db)
    for i in (1, 2, 3):
        _add(db, w, i)
    rep = diagnose.completeness(db, w)
    assert rep["health"] == "ok"
    assert rep["fetched"] == 3 and rep["gaps"] == []
    db.close()


def test_detects_missing_chapter_gap():
    init_db()
    db = SessionLocal()
    w = _work(db)
    for i in (1, 2, 4):  # 3 is missing
        _add(db, w, i)
    rep = diagnose.completeness(db, w)
    assert rep["health"] == "incomplete"
    assert rep["gaps"] == [3]
    db.close()


def test_detects_fetched_below_advertised():
    init_db()
    db = SessionLocal()
    w = _work(db, expected=5)
    for i in (1, 2, 3):
        _add(db, w, i)
    rep = diagnose.completeness(db, w)
    assert rep["health"] == "incomplete"
    assert rep["advertised"] == 5 and rep["fetched"] == 3
    db.close()


def test_repair_retries_failed_and_fills_gaps():
    init_db()
    db = SessionLocal()
    w = _work(db)
    _add(db, w, 1, "fetched")
    _add(db, w, 2, "failed")   # should be retried
    _add(db, w, 4, "fetched")  # leaves a hole at 3
    rep = diagnose.repair(db, w)
    # Chapter 2 reset to pending; chapter 3 synthesized + enqueued.
    by_index = {c.index: c for c in db.scalars(
        select(Chapter).where(Chapter.work_id == w.id)).all()}
    assert by_index[2].fetch_status == "pending"
    assert 3 in by_index and by_index[3].fetch_status == "pending"
    assert by_index[3].source_chapter_ref == f"{BASE}3"
    # An open backfill job now exists so the scheduler will fetch them.
    assert db.scalar(select(CrawlJob).where(
        CrawlJob.work_id == w.id, CrawlJob.status == "scheduled"))
    assert any("retry" in a for a in rep["actions"])
    db.close()


def test_repair_reseeds_stalled_sequential_crawl():
    init_db()
    db = SessionLocal()
    w = _work(db, expected=10)
    for i in (1, 2, 3):
        _add(db, w, i)  # head stalled at 3, advertised 10, nothing pending
    rep = diagnose.repair(db, w)
    # The next chapter after the highest fetched is synthesized + enqueued.
    nxt = db.scalar(select(Chapter).where(Chapter.work_id == w.id, Chapter.index == 4))
    assert nxt is not None and nxt.source_chapter_ref == f"{BASE}4"
    assert any("re-seed" in a for a in rep["actions"])
    db.close()


def test_apply_health_raises_stale_ceiling():
    """A serial that gathered past its old advertised total must not report 'fetched > total':
    apply_health lifts the ceiling (and total_chapters_known) to the real listed count."""
    init_db()
    db = SessionLocal()
    w = _work(db, expected=3)
    w.total_chapters_known = 3
    db.commit()
    for i in (1, 2, 3, 4, 5):  # source continued; 5 chapters now listed + fetched
        _add(db, w, i)
    rep = diagnose.completeness(db, w)
    diagnose.apply_health(db, w, rep)
    db.refresh(w)
    assert w.total_chapters_expected == 5   # raised from 3 to reflect the latest ceiling
    assert w.total_chapters_known == 5
    assert w.health == "ok"                 # no longer "incomplete (fetched vs advertised)"
    db.close()


def test_apply_health_keeps_unset_ceiling_none():
    """When no total was ever advertised, apply_health leaves expected as None (the UI falls
    back to total_chapters_known) — it only raises an existing ceiling, never invents one."""
    init_db()
    db = SessionLocal()
    w = _work(db)  # expected=None
    for i in (1, 2, 3):
        _add(db, w, i)
    rep = diagnose.completeness(db, w)
    diagnose.apply_health(db, w, rep)
    db.refresh(w)
    assert w.total_chapters_expected is None
    assert w.total_chapters_known == 3
    db.close()
