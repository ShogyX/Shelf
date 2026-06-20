"""Per-title crawl policy enforcement + the daily-cap count-collapse bug fix."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import scheduler
from app.ingestion.base import RateLimited, RawChapter
from app.models import Chapter, ChapterContent, CrawlJob, Source, Work


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for model in (CrawlJob, Chapter, Work, Source):
        db.execute(delete(model))
    db.commit()
    db.close()
    yield


@pytest.mark.asyncio
async def test_scheduled_task_owns_session_and_isolates_errors():
    """F0.6: @scheduled_task hands the body a live Session, swallows+logs any error (so a failing
    tick can't kill the scheduler), and supports both async and sync (off-loop) bodies."""
    seen = {}

    @scheduler.scheduled_task()
    async def _async_tick(db):
        seen["async_db"] = db is not None
        raise RuntimeError("boom")  # must be swallowed, not propagated

    @scheduler.scheduled_task(to_thread=True)
    def _sync_tick(db):
        import threading
        seen["sync_db"] = db is not None
        seen["thread"] = threading.current_thread().name

    await _async_tick()                 # does NOT raise despite the body raising
    await _sync_tick()
    assert seen["async_db"] and seen["sync_db"]
    assert seen["thread"] != "MainThread"   # sync body ran off the event loop


class FakeAdapter:
    key = "generic_feed"

    def __init__(self):
        self.calls = 0

    async def fetch_chapter(self, ref):
        self.calls += 1
        return RawChapter(title=ref.title, body="<p>body text here, long enough.</p>", fmt="html")


def _setup(db, *, expected=None, chapters=2, **work_kw) -> tuple[Work, CrawlJob]:
    src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                 tos_permitted=True)
    db.add(src)
    db.commit()
    w = Work(source_id=src.id, source_work_ref="https://s/n", title="X", hooked=True,
             status="ongoing", total_chapters_known=chapters,
             total_chapters_expected=expected, **work_kw)
    db.add(w)
    db.commit()
    db.refresh(w)
    for i in range(1, chapters + 1):
        db.add(Chapter(work_id=w.id, index=i, source_chapter_ref=f"https://s/n/c/{i}",
                       title=f"Chapter {i}", fetch_status="pending"))
    job = CrawlJob(work_id=w.id, kind="backfill", status="scheduled", cursor={})
    db.add(job)
    db.commit()
    db.refresh(job)
    return w, job


def test_seconds_until_window_is_minute_precise():
    """I8b: the wait is computed from the exact start:00 instant — no whole-hour over-wait."""
    base = datetime(2026, 6, 12, 10, 45, 0, tzinfo=UTC)
    w = Work(title="X", crawl_window_start=11, crawl_window_end=12)   # opens 11:00, currently closed
    secs = scheduler._seconds_until_window(w, base)
    assert 14 * 60 <= secs <= 15 * 60                  # ~15 min, NOT ~a day
    # start hour already passed today → next opening is tomorrow, never a tiny/negative wait
    late = datetime(2026, 6, 12, 23, 30, 0, tzinfo=UTC)
    w2 = Work(title="Y", crawl_window_start=9, crawl_window_end=10)
    assert scheduler._seconds_until_window(w2, late) > 9 * 3600
    # open now → 0
    assert scheduler._seconds_until_window(
        Work(title="Z", crawl_window_start=10, crawl_window_end=12), base) == 0.0


class AlwaysFailAdapter:
    key = "generic_feed"

    def __init__(self):
        self.calls = 0

    async def fetch_chapter(self, ref):
        self.calls += 1
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_failure_tracks_attempts_per_chapter_and_stops_batch(monkeypatch):
    """I3: a fetch failure counts per CHAPTER (via the cursor), not a job-global counter, and stops
    the batch instead of churning the rest — so the 5-strike guard isn't defeated by many chapters."""
    db = SessionLocal()
    w, job = _setup(db, chapters=3)
    adapter = AlwaysFailAdapter()
    monkeypatch.setattr("app.ingestion.scheduler.adapter_for", lambda src: adapter)

    # Five runs: each attempts only the frontier chapter (index 1), incrementing its per-chapter count.
    for n in range(1, 6):
        await scheduler._process_job(db, job)
        db.refresh(job)
        if n < 5:
            assert adapter.calls == n                  # ONE chapter attempted per run (batch stopped)
            assert (job.cursor or {}).get("fail_count") == n and job.attempts == n
    # On the 5th strike, chapter 1 is failed and its per-chapter counter cleared.
    ch1 = db.scalar(select(Chapter).where(Chapter.work_id == w.id, Chapter.index == 1))
    assert ch1.fetch_status == "failed"
    assert (job.cursor or {}).get("fail_count") is None and job.attempts == 0
    # The other chapters were never churned past the fault.
    others = db.scalars(select(Chapter).where(Chapter.work_id == w.id, Chapter.index > 1)).all()
    assert all(c.fetch_status == "pending" for c in others)
    db.close()


@pytest.mark.asyncio
async def test_outside_window_reschedules_without_fetching(monkeypatch):
    db = SessionLocal()
    now = datetime.now(UTC)
    # A 1-hour window two hours from now → current hour is outside it.
    start = (now.hour + 2) % 24
    w, job = _setup(db, crawl_window_start=start, crawl_window_end=(start + 1) % 24)
    adapter = FakeAdapter()
    monkeypatch.setattr("app.ingestion.scheduler.adapter_for", lambda src: adapter)

    await scheduler._process_job(db, job)

    assert adapter.calls == 0
    pend = db.scalars(select(Chapter).where(Chapter.work_id == w.id)).all()
    assert all(c.fetch_status == "pending" for c in pend)
    db.refresh(job)
    assert job.status == "scheduled" and "hours" in (job.last_error or "")
    db.close()


class BlockedAdapter:
    """Simulates a source rate-limiting/Cloudflare-blocking us (comix)."""
    key = "comix"

    def __init__(self, *, blocked=True):
        self.calls = 0
        self.blocked = blocked

    async def fetch_chapter(self, ref):
        self.calls += 1
        if self.blocked:
            raise RateLimited("comix.to is rate-limiting / Cloudflare-challenging the reader")
        return RawChapter(title=ref.title, body="<p>real content, long enough to store.</p>", fmt="html")


@pytest.mark.asyncio
async def test_rate_limit_cools_the_job_down_instead_of_failing_chapters(monkeypatch):
    db = SessionLocal()
    w, job = _setup(db, chapters=4)
    adapter = BlockedAdapter()
    monkeypatch.setattr("app.ingestion.scheduler.adapter_for", lambda src: adapter)
    started = datetime.now(UTC)

    await scheduler._process_job(db, job)

    # Stopped at the FIRST block — didn't hammer the rest of the batch.
    assert adapter.calls == 1
    db.refresh(job)
    assert job.status == "scheduled"
    assert (job.cursor or {}).get("rl_cooldowns") == 1
    # Cooled down ~10 min (not the normal tick interval), and chapters left PENDING (not failed).
    assert scheduler._aware(job.scheduled_for) >= started + timedelta(
        seconds=scheduler._RL_COOLDOWN_BASE_S - 5)
    assert all(c.fetch_status == "pending"
               for c in db.scalars(select(Chapter).where(Chapter.work_id == w.id)).all())

    # A second block escalates the backoff (exponential).
    await scheduler._process_job(db, job)
    db.refresh(job)
    assert (job.cursor or {}).get("rl_cooldowns") == 2
    assert scheduler._aware(job.scheduled_for) >= started + timedelta(
        seconds=2 * scheduler._RL_COOLDOWN_BASE_S - 5)
    db.close()


@pytest.mark.asyncio
async def test_successful_fetch_resets_the_cooldown(monkeypatch):
    db = SessionLocal()
    w, job = _setup(db, chapters=2)
    job.cursor = {"next_index": 1, "rl_cooldowns": 3}  # was cooling down
    db.commit()
    monkeypatch.setattr("app.ingestion.scheduler.adapter_for", lambda src: BlockedAdapter(blocked=False))
    await scheduler._process_job(db, job)
    db.refresh(job)
    assert "rl_cooldowns" not in (job.cursor or {})  # escalation reset on a clean fetch
    db.close()


def test_reaper_does_not_pull_a_rate_limit_cooldown_forward():
    db = SessionLocal()
    w, job = _setup(db, chapters=2)
    future = datetime.now(UTC) + timedelta(seconds=scheduler._RL_COOLDOWN_BASE_S)
    job.cursor = {"next_index": 1, "rl_cooldowns": 1}
    job.scheduled_for = future
    db.commit()
    db.close()

    scheduler.reap_stalled_jobs()

    db = SessionLocal()
    j = db.scalar(select(CrawlJob).where(CrawlJob.id == job.id))
    # Still cooling down — NOT yanked forward to now (which would hammer the block).
    assert scheduler._aware(j.scheduled_for) >= datetime.now(UTC) + timedelta(
        seconds=scheduler._RL_COOLDOWN_BASE_S - 30)
    db.close()


@pytest.mark.asyncio
async def test_interval_fetches_one_per_run(monkeypatch):
    db = SessionLocal()
    w, job = _setup(db, chapters=3, crawl_interval_s=30)
    adapter = FakeAdapter()
    monkeypatch.setattr("app.ingestion.scheduler.adapter_for", lambda src: adapter)

    await scheduler._process_job(db, job)

    assert adapter.calls == 1   # one request this run (a per-title interval is set)
    db.close()


class GapAdapter:
    """Sequential numeric source whose real chapters are scattered among empty 'gap' pages
    (e.g. novellunar: only some …/chapter/N slots have content). Real indices store + chain;
    the rest return a near-empty body the engine classifies as a dead-end."""
    key = "generic_feed"

    def __init__(self, real: set[int]):
        self.real = real

    async def fetch_chapter(self, ref):
        from app.ingestion.extract import synthesize_next_chapter_url
        n = int(ref.source_chapter_ref.rsplit("/", 1)[1])
        nxt = synthesize_next_chapter_url(ref.source_chapter_ref)
        if n in self.real:  # >50 distinct words so it's real prose, not the short-page dead-end
            words = " ".join(f"w{n}t{i}" for i in range(120))
            return RawChapter(title=f"Ch{n}", body=f"<p>chapter {n}: {words}</p>",
                              fmt="html", next_ref=nxt, next_title=None)
        return RawChapter(title="", body="<p>x</p>", fmt="html")  # gap → dead-end


@pytest.mark.asyncio
async def test_backfill_bridges_gaps_instead_of_ending_early(monkeypatch):
    """Root-cause fix: a single empty …/chapter/N page is a HOLE in the numbering, not the end.
    The crawl must step past gaps (bounded by the advertised total) and gather the real chapters
    that follow — previously it stopped dead at the first gap."""
    db = SessionLocal()
    # Seed only chapter 1; real content at 1,2,4 with a gap at 3; advertised total = 4.
    w, job = _setup(db, chapters=1, expected=4)
    ch1 = db.scalar(select(Chapter).where(Chapter.work_id == w.id, Chapter.index == 1))
    ch1.source_chapter_ref = "https://s/n/chapter/1"
    db.commit()
    monkeypatch.setattr("app.ingestion.scheduler.adapter_for",
                        lambda src: GapAdapter(real={1, 2, 4}))

    for _ in range(30):  # drive ticks until the backfill finalizes
        db.refresh(job)
        if job.status == "done":
            break
        await scheduler._process_job(db, job)

    by_index = {c.index: c.fetch_status
                for c in db.scalars(select(Chapter).where(Chapter.work_id == w.id)).all()}
    # The gap at 3 was bridged — chapter 4 (past it) got fetched, not abandoned.
    assert by_index.get(1) == "fetched"
    assert by_index.get(2) == "fetched"
    assert by_index.get(3) == "skipped"
    assert by_index.get(4) == "fetched"
    db.close()


def test_finalize_done_keeps_outstanding_visible():
    db = SessionLocal()
    src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                 tos_permitted=True)
    db.add(src)
    db.commit()
    w = Work(source_id=src.id, title="X", hooked=True, status="ongoing")
    db.add(w)
    db.commit()
    db.refresh(w)
    db.add_all([
        Chapter(work_id=w.id, index=1, fetch_status="fetched"),
        Chapter(work_id=w.id, index=2, fetch_status="fetched"),
        Chapter(work_id=w.id, index=3, fetch_status="failed"),
    ])
    job = CrawlJob(work_id=w.id, kind="backfill", status="running")
    db.add(job)
    db.commit()

    scheduler._finalize_done(db, job, w)

    db.refresh(w)
    # Total must NOT collapse to the 2 fetched — the failed chapter stays visible.
    assert w.total_chapters_expected == 3
    assert w.health == "incomplete"
    db.close()


def test_finalize_done_excludes_dead_end_placeholder():
    """A dead-end frontier probe (status 'skipped') is a placeholder for an unpublished chapter,
    not a real one — it must not peg the totals one above the real count ('N/N+1' forever)."""
    db = SessionLocal()
    src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                 tos_permitted=True)
    db.add(src)
    db.commit()
    # known/expected were optimistically bumped to the placeholder index (4) when it was enqueued.
    w = Work(source_id=src.id, title="X", hooked=True, status="ongoing",
             total_chapters_known=4, total_chapters_expected=4)
    db.add(w)
    db.commit()
    db.refresh(w)
    db.add_all([
        Chapter(work_id=w.id, index=1, fetch_status="fetched"),
        Chapter(work_id=w.id, index=2, fetch_status="fetched"),
        Chapter(work_id=w.id, index=3, fetch_status="fetched"),
        Chapter(work_id=w.id, index=4, fetch_status="skipped"),  # dead-end frontier placeholder
    ])
    job = CrawlJob(work_id=w.id, kind="backfill", status="running")
    db.add(job)
    db.commit()

    scheduler._finalize_done(db, job, w)

    db.refresh(w)
    # 3 real chapters — the placeholder must not inflate either total to 4.
    assert w.total_chapters_known == 3
    assert w.total_chapters_expected == 3
    assert w.health == "ok"
    db.close()


def test_finalize_done_keeps_advertised_total_above_real_rows():
    """When the source advertises MORE chapters than we have real rows (a still-releasing serial),
    keep that higher ceiling — only the placeholder-inflated case is retracted."""
    db = SessionLocal()
    src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                 tos_permitted=True)
    db.add(src)
    db.commit()
    # 2 real chapters + 1 dead-end placeholder, but metadata advertises 5 total.
    w = Work(source_id=src.id, title="X", hooked=True, status="ongoing",
             total_chapters_known=3, total_chapters_expected=5)
    db.add(w)
    db.commit()
    db.refresh(w)
    db.add_all([
        Chapter(work_id=w.id, index=1, fetch_status="fetched"),
        Chapter(work_id=w.id, index=2, fetch_status="fetched"),
        Chapter(work_id=w.id, index=3, fetch_status="skipped"),
    ])
    job = CrawlJob(work_id=w.id, kind="backfill", status="running")
    db.add(job)
    db.commit()

    scheduler._finalize_done(db, job, w)

    db.refresh(w)
    assert w.total_chapters_known == 2          # real rows only
    assert w.total_chapters_expected == 5       # advertised ceiling preserved
    db.close()


def test_set_crawl_policy_endpoint():
    from app.routers.works import set_crawl_policy
    from app.schemas import CrawlPolicyIn

    db = SessionLocal()
    w = Work(title="X", hooked=True)
    db.add(w)
    db.commit()
    db.refresh(w)
    out = set_crawl_policy(
        w.id,
        CrawlPolicyIn(crawl_interval_s=20, crawl_window_start=1, crawl_window_end=6),
        db,
    )
    assert out.crawl_interval_s == 20
    assert out.crawl_window_start == 1 and out.crawl_window_end == 6
    db.close()


@pytest.mark.asyncio
async def test_permanent_failure_marks_unavailable_not_retried(monkeypatch):
    """A members-only/paywalled chapter (PermanentFetchError) is marked 'unavailable' — a
    terminal state the reaper never revives — so it can't thrash the source every hour."""
    from app.ingestion.base import PermanentFetchError

    class LockedAdapter:
        key = "generic_feed"
        async def fetch_chapter(self, ref):
            raise PermanentFetchError("members-only")

    db = SessionLocal()
    w, job = _setup(db, chapters=1)  # one chapter per tick
    monkeypatch.setattr("app.ingestion.scheduler.adapter_for", lambda src: LockedAdapter())

    await scheduler._process_job(db, job)

    ch = db.scalar(select(Chapter).where(Chapter.work_id == w.id))
    assert ch.fetch_status == "unavailable"  # not "failed" → the reaper won't requeue it
    # _outstanding counts only pending/failed, so nothing is outstanding for the reaper.
    pending, failed = scheduler._outstanding(db, w.id)
    assert pending == 0 and failed == 0
    db.close()


@pytest.mark.asyncio
async def test_imgcache_sweep_pins_in_library_comic_pages(monkeypatch, tmp_path):
    """B3: a sweep must never evict an imgcache page referenced by an in-library work's chapter
    content (its remote src was overwritten on localize, so an evicted page can't be re-fetched).
    An unreferenced file is still freely evictable."""
    from app import config_store, imagecache
    from sqlalchemy import delete

    db = SessionLocal()
    db.execute(delete(ChapterContent))
    # In-library (hooked) comic whose chapter body points at a localized imgcache page.
    w = Work(title="Comic", hooked=True, media_kind="comic")
    db.add(w)
    db.flush()
    body = '<img src="/media/imgcache/pinnedpage.jpg">'
    content = ChapterContent(chapter_id=0, format="html", body=body, checksum="c",
                             raw_checksum="r")
    db.add(content)
    db.flush()
    ch = Chapter(work_id=w.id, index=1, fetch_status="fetched", content_id=content.id)
    db.add(ch)
    db.commit()
    db.close()

    # Real files on disk: the pinned page + an unreferenced one. Big enough that the cache exceeds
    # a 0-byte cap, forcing the sweep to evict.
    cache = tmp_path / "imgcache"
    cache.mkdir()
    pinned_file = cache / "pinnedpage.jpg"
    orphan_file = cache / "orphanpage.jpg"
    pinned_file.write_bytes(b"x" * 1024)
    orphan_file.write_bytes(b"y" * 1024)

    monkeypatch.setattr(imagecache, "media_dir", lambda: tmp_path)
    monkeypatch.setattr(config_store, "effective", lambda key: 0.0001 if key == "imgcache_max_mb" else None)

    await scheduler.imgcache_sweep_tick()

    assert pinned_file.exists()        # in-library comic page survived
    assert not orphan_file.exists()    # unreferenced file was evicted under the cap
