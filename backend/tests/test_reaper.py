"""The stalled-job reaper: revive crashed/parked/orphaned jobs, never finished works."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion.scheduler import _aware, _prune_superseded_jobs, reap_stalled_jobs
from app.models import Chapter, CrawlJob, Source, Work


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for model in (CrawlJob, Chapter, Work, Source):
        db.execute(delete(model))
    db.commit()
    db.close()
    yield


def _now():
    return datetime.now(UTC)


def _make(db, *, statuses, job=None, **work_kw):
    """Create a work with chapters of the given fetch_statuses and an optional job dict."""
    src = db.scalar(select(Source).where(Source.key == "generic_feed"))
    if src is None:
        src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                     tos_permitted=True)
        db.add(src)
        db.commit()
    work_kw.setdefault("status", "ongoing")
    w = Work(source_id=src.id, source_work_ref=f"https://s/{len(statuses)}-{id(statuses)}",
             title="W", hooked=True, **work_kw)
    db.add(w)
    db.commit()
    db.refresh(w)
    for i, st in enumerate(statuses, start=1):
        db.add(Chapter(work_id=w.id, index=i, source_chapter_ref=f"c{i}",
                       title=f"Ch {i}", fetch_status=st))
    if job is not None:
        db.add(CrawlJob(work_id=w.id, kind="backfill", cursor={}, **job))
    db.commit()
    db.refresh(w)
    return w


def test_reopens_orphaned_work_with_pending_chapters():
    db = SessionLocal()
    w = _make(db, statuses=["pending", "fetched"], job=None)  # no open job
    db.close()
    assert reap_stalled_jobs() == 1
    db = SessionLocal()
    jobs = db.scalars(select(CrawlJob).where(CrawlJob.work_id == w.id)).all()
    assert len(jobs) == 1 and jobs[0].status == "scheduled"
    db.close()


def test_requeues_failed_chapters_when_no_open_job():
    db = SessionLocal()
    w = _make(db, statuses=["fetched", "failed", "failed"], job=None)
    db.close()
    assert reap_stalled_jobs() == 1
    db = SessionLocal()
    states = [
        c.fetch_status
        for c in db.scalars(select(Chapter).where(Chapter.work_id == w.id)).all()
    ]
    assert states.count("pending") == 2 and states.count("failed") == 0  # revived
    db.close()


def test_failed_chapters_not_retried_too_often():
    # A work whose last job finished moments ago must NOT have its failed chapters
    # requeued (anti-thrash) — otherwise a permanently-broken chapter loops forever.
    db = SessionLocal()
    w = _make(db, statuses=["fetched", "failed"],
              job={"status": "done", "finished_at": _now() - timedelta(seconds=60)})
    db.close()
    assert reap_stalled_jobs() == 0
    db = SessionLocal()
    states = [
        c.fetch_status
        for c in db.scalars(select(Chapter).where(Chapter.work_id == w.id)).all()
    ]
    assert states.count("failed") == 1  # left failed, not requeued
    db.close()


def test_does_not_restart_a_finished_work():
    db = SessionLocal()
    _make(db, statuses=["fetched", "fetched"], status="complete", job=None)
    db.close()
    assert reap_stalled_jobs() == 0  # nothing pending/failed → leave it alone


def test_rearms_abandoned_running_job():
    db = SessionLocal()
    old = _now() - timedelta(hours=2)
    _make(db, statuses=["pending"], job={"status": "running", "started_at": old,
                                         "scheduled_for": old})
    db.close()
    assert reap_stalled_jobs() == 1
    db = SessionLocal()
    j = db.scalar(select(CrawlJob))
    assert j.status == "scheduled"
    db.close()


def test_live_lease_blocks_revival_and_bump_invalidates_stale_writer():
    """The two-writer race (F0.3): a long-running-but-ALIVE job (lease renewed) must NOT be
    revived even past the stuck threshold; once the lease lapses, revival bumps the token so
    the abandoned runner's _renew_lease fails and it stops committing."""
    from app.ingestion.scheduler import _lease_expired, _renew_lease, _stamp_lease
    db = SessionLocal()
    old = _now() - timedelta(hours=2)
    w = _make(db, statuses=["pending"], job={"status": "running", "started_at": old,
                                             "scheduled_for": old})
    j = db.scalar(select(CrawlJob).where(CrawlJob.work_id == w.id))
    token = _stamp_lease(db, j)               # the live runner claims + renews its lease
    db.commit()
    db.close()

    assert reap_stalled_jobs() == 0           # live lease → NOT revived despite old started_at
    db = SessionLocal()
    j = db.scalar(select(CrawlJob))
    assert j.status == "running" and j.lease_token == token

    # Lease lapses (runner crashed/hung) → reaper revives AND bumps the token.
    j.lease_expires_at = _now() - timedelta(seconds=5)
    db.commit()
    db.close()
    assert reap_stalled_jobs() == 1
    db = SessionLocal()
    j = db.scalar(select(CrawlJob))
    assert j.status == "scheduled" and j.lease_token != token   # new owner claim invalidated ours

    # The abandoned runner now tries to commit progress: renewal must fail → it abandons.
    assert _renew_lease(db, j, token) is False
    assert _lease_expired(j, _now()) is True  # NULL expiry counts as expired for pickup
    db.close()


def test_cas_rearm_no_ops_when_lease_renewed_after_observe():
    """CONC-1: a live runner renews its lease (future expiry, SAME token) after the reaper observed
    it as expired. The fresh-session CAS must see the current renewed lease and NOT re-arm — otherwise
    a healthy backfill is needlessly yanked."""
    from app.ingestion.scheduler import _cas_rearm_running
    db = SessionLocal()
    old = _now() - timedelta(hours=2)
    w = _make(db, statuses=["pending"], job={"status": "running", "started_at": old, "scheduled_for": old})
    j = db.scalar(select(CrawlJob).where(CrawlJob.work_id == w.id))
    j.lease_token = "tok"
    j.lease_expires_at = _now() - timedelta(seconds=5)   # expired, as the reaper observed it
    db.commit()
    # Simulate the renewal landing between observe and swap: lease pushed into the future, same token.
    j.lease_expires_at = _now() + timedelta(minutes=5)
    db.commit()
    jid = j.id
    db.close()
    assert _cas_rearm_running(jid, "tok", _now()) is False
    db = SessionLocal()
    j = db.scalar(select(CrawlJob))
    assert j.status == "running" and j.lease_token == "tok"   # untouched
    db.close()


def test_cas_rearm_swaps_when_still_expired_but_not_on_wrong_token():
    """CONC-1: the CAS re-arms a still-expired lease (new token, scheduled), but a wrong observed token
    (a fresh runner already claimed it) must no-op."""
    from app.ingestion.scheduler import _cas_rearm_running
    db = SessionLocal()
    old = _now() - timedelta(hours=2)
    w = _make(db, statuses=["pending"], job={"status": "running", "started_at": old, "scheduled_for": old})
    j = db.scalar(select(CrawlJob).where(CrawlJob.work_id == w.id))
    j.lease_token = "tok"
    j.lease_expires_at = _now() - timedelta(seconds=5)
    db.commit()
    jid = j.id
    db.close()
    assert _cas_rearm_running(jid, "WRONG", _now()) is False   # token mismatch → no swap
    assert _cas_rearm_running(jid, "tok", _now()) is True       # correct token → re-armed
    db = SessionLocal()
    j = db.scalar(select(CrawlJob))
    assert j.status == "scheduled" and j.lease_token != "tok" and j.lease_expires_at is None
    db.close()


def test_tick_skips_running_job_with_live_lease():
    """tick() must not start a second runner on a job whose lease shows it's executing."""
    import asyncio
    from app.ingestion import scheduler as sched
    db = SessionLocal()
    w = _make(db, statuses=["pending"],
              job={"status": "running", "started_at": _now(), "scheduled_for": _now()})
    j = db.scalar(select(CrawlJob).where(CrawlJob.work_id == w.id))
    sched._stamp_lease(db, j)                 # live runner
    db.commit()
    db.close()

    ran = []
    async def fake_run(job_id):
        ran.append(job_id)
    orig = sched._run_job
    sched._run_job = fake_run
    try:
        asyncio.run(sched.tick())
    finally:
        sched._run_job = orig
    assert ran == []                          # live-leased running job NOT re-picked


def test_prune_superseded_jobs():
    db = SessionLocal()
    src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                 tos_permitted=True)
    db.add(src)
    db.commit()

    counter = iter(range(1, 1000))
    def mkwork():
        # A STABLE unique ref per call: id(object()) reuses freed addresses → colliding refs that
        # the uq_work_source_ref index (correctly) rejects.
        w = Work(source_id=src.id, source_work_ref=f"r{next(counter)}", title="W", hooked=True)
        db.add(w)
        db.commit()
        db.refresh(w)
        return w

    # Work A: a done backfill superseded by a running refresh → done one is pruned.
    a = mkwork()
    db.add(CrawlJob(work_id=a.id, kind="backfill", status="done", last_error="budget"))
    db.add(CrawlJob(work_id=a.id, kind="refresh", status="running"))
    # Work B: three done jobs, no open → keep only the newest.
    b = mkwork()
    for i in range(3):
        db.add(CrawlJob(work_id=b.id, kind="refresh", status="done",
                        created_at=_now() - timedelta(hours=i)))
    # Work C: a single done job, no open → kept.
    c = mkwork()
    db.add(CrawlJob(work_id=c.id, kind="backfill", status="done"))
    db.commit()

    pruned = _prune_superseded_jobs(db)
    db.commit()
    assert pruned == 3  # A's done (1) + B's two older (2)
    from sqlalchemy import func

    def jobcount(wid):
        return db.scalar(select(func.count()).select_from(CrawlJob).where(CrawlJob.work_id == wid))

    assert jobcount(a.id) == 1 and jobcount(b.id) == 1 and jobcount(c.id) == 1
    db.close()


def test_pulls_forward_when_per_title_block_cleared():
    db = SessionLocal()
    future = _now() + timedelta(hours=5)
    # Parked in the future but no per-title window/limit set → block is clear → revive.
    _make(db, statuses=["pending"],
          job={"status": "scheduled", "scheduled_for": future,
               "last_error": "outside the title's allowed crawl hours; resuming later"})
    db.close()
    assert reap_stalled_jobs() == 1
    db = SessionLocal()
    j = db.scalar(select(CrawlJob))
    assert _aware(j.scheduled_for) <= _now()
    db.close()
