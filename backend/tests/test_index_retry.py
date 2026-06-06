"""Resilient index crawling: transient failures retry, blocks throttle the site, only a
genuinely dead URL / exhausted attempts marks a page permanently failed."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from app.db import SessionLocal, init_db
from app.ingestion import adapters  # noqa: F401 — registers built-in adapters (web_index)
from app.ingestion import indexer
from app.ingestion.engine import ensure_source
from app.ingestion.base import registry
from app.ingestion.fetcher import DailyBudgetExceeded, RobotsDisallowed
from app.models import CatalogWork, IndexedPage, IndexSite


class _Resp:
    def __init__(self, status=200, text="<html><body><p>hi</p></body></html>", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class _Fetcher:
    """Fake fetcher whose get_html applies `outcome(url)` — a _Resp to return or an Exception
    instance to raise. Records calls so tests can assert no fetch happened when skipped."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[str] = []

    async def get_html(self, source_key, url, **kw):
        self.calls.append(url)
        r = self.outcome(url)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    for m in (CatalogWork, IndexedPage, IndexSite):
        s.execute(delete(m))
    s.commit()
    yield s
    s.close()


def _seed(db, *, status="pending", next_attempt_at=None, attempts=0):
    site = IndexSite(root_url="https://x.com", domain="x.com", status="active", max_depth=3)
    db.add(site)
    db.commit()
    db.refresh(site)
    page = IndexedPage(site_id=site.id, url="https://x.com/p", depth=0, status=status,
                       attempts=attempts, next_attempt_at=next_attempt_at)
    db.add(page)
    db.commit()
    db.refresh(page)
    return site, page


async def test_transient_block_retries_then_cools_site(db, monkeypatch):
    """A 403 (anti-bot block) keeps the page in the frontier and, once errors repeat, cools the
    whole site down — it is never marked permanently failed for a transient block."""
    site, page = _seed(db)
    fetcher = _Fetcher(lambda url: _Resp(status=403))
    monkeypatch.setattr(indexer, "get_fetcher", lambda: fetcher)

    await indexer._fetch_one(db, None, page, site)
    db.refresh(page)
    db.refresh(site)
    assert page.status == "pending"          # still retryable
    assert page.attempts == 1
    assert page.next_attempt_at is not None   # deferred
    assert site.consecutive_errors == 1
    assert site.cooldown_until is None        # first error: below the site-block threshold

    # Second consecutive block trips the site-wide cooldown.
    page.next_attempt_at = None
    db.commit()
    await indexer._fetch_one(db, None, page, site)
    db.refresh(page)
    db.refresh(site)
    assert page.status == "pending"
    assert page.attempts == 2
    assert site.consecutive_errors == 2
    assert site.cooldown_until is not None and indexer._as_utc(site.cooldown_until) > datetime.now(UTC)


async def test_dead_url_fails_without_blaming_site(db, monkeypatch):
    """A 404 is permanent (fail the page) but the site is responding fine — no cooldown."""
    site, page = _seed(db)
    fetcher = _Fetcher(lambda url: _Resp(status=404))
    monkeypatch.setattr(indexer, "get_fetcher", lambda: fetcher)

    await indexer._fetch_one(db, None, page, site)
    db.refresh(page)
    db.refresh(site)
    assert page.status == "failed"
    assert site.consecutive_errors == 0
    assert site.cooldown_until is None


async def test_robots_disallow_skips_not_fails(db, monkeypatch):
    site, page = _seed(db)
    fetcher = _Fetcher(lambda url: RobotsDisallowed("nope"))
    monkeypatch.setattr(indexer, "get_fetcher", lambda: fetcher)

    await indexer._fetch_one(db, None, page, site)
    db.refresh(page)
    assert page.status == "skipped"
    assert page.attempts == 0


async def test_download_navigation_skips_without_cooling_site(db, monkeypatch):
    """Navigating to a download URL makes Playwright throw 'Download is starting'. That means the
    URL is a FILE, not the site blocking us — skip just that page, never cool the whole site down
    (a book's handful of download links would otherwise stall the entire crawl)."""
    site, page = _seed(db)
    fetcher = _Fetcher(lambda url: Exception(
        'Page.goto: Download is starting\nCall log:\n  - navigating to "https://x.com/35.epub.images"'
    ))
    monkeypatch.setattr(indexer, "get_fetcher", lambda: fetcher)

    await indexer._fetch_one(db, None, page, site)
    db.refresh(page)
    db.refresh(site)
    assert page.status == "skipped"            # the file is skipped, not retried
    assert page.attempts == 0                  # not counted against the page's retries
    assert site.consecutive_errors == 0        # NOT blamed on the site
    assert site.cooldown_until is None         # so the site is never cooled down


def test_asset_urls_are_not_enqueued(db):
    """Download/asset links (Gutenberg's '.epub.images'/'.kindle.images', sheet music, plain text)
    must never enter the frontier — only real crawlable pages do."""
    site = _site(db, root_url="https://www.gutenberg.org/", domain="www.gutenberg.org")
    page = _page(db, site, url="https://www.gutenberg.org/ebooks/35")
    html = (
        '<a href="/ebooks/35.epub.images">EPUB</a>'
        '<a href="/ebooks/35.kindle.images">Kindle</a>'
        '<a href="/ebooks/35.kf8.images">KF8</a>'
        '<a href="/ebooks/35.txt.utf-8">Plain Text</a>'
        '<a href="/files/78803/78803-h/music/i_359b.mxl">music</a>'
        '<a href="/files/20973/20973-readme.txt">readme</a>'
        '<a href="/ebooks/36">A Real Book</a>'   # the only crawlable page here
    )
    added, _new = indexer._enqueue_links(db, site, page, html)
    urls = set(db.scalars(select(IndexedPage.url).where(IndexedPage.status == "pending")).all())
    assert any(u.endswith("/ebooks/36") for u in urls)
    assert not any(".epub" in u or ".kindle" in u or ".kf8" in u or ".mxl" in u
                   or u.endswith(".txt") or ".txt.utf-8" in u for u in urls)


async def test_daily_budget_pauses_without_failing(db, monkeypatch):
    """Hitting the daily request budget pauses the site (cooldown) and leaves the page pending
    — it is pacing, not a failure, and must not consume the page's retry attempts."""
    site, page = _seed(db)
    fetcher = _Fetcher(lambda url: DailyBudgetExceeded("spent"))
    monkeypatch.setattr(indexer, "get_fetcher", lambda: fetcher)

    await indexer._fetch_one(db, None, page, site)
    db.refresh(page)
    db.refresh(site)
    assert page.status == "pending"
    assert page.attempts == 0                  # budget pause never counts against the page
    assert site.cooldown_until is not None


async def test_attempts_exhausted_marks_failed(db, monkeypatch):
    """After the max attempts a perpetually-blocked page is finally marked failed so it stops
    holding up the frontier."""
    site, page = _seed(db, attempts=indexer._MAX_PAGE_ATTEMPTS - 1)
    fetcher = _Fetcher(lambda url: _Resp(status=503))
    monkeypatch.setattr(indexer, "get_fetcher", lambda: fetcher)

    await indexer._fetch_one(db, None, page, site)
    db.refresh(page)
    assert page.status == "failed"
    assert page.attempts == indexer._MAX_PAGE_ATTEMPTS


def test_note_fetch_success_clears_backoff(db):
    site, _ = _seed(db)
    site.consecutive_errors = 4
    site.cooldown_until = datetime.now(UTC) + timedelta(minutes=5)
    db.commit()
    indexer._note_fetch_success(db, site)
    db.refresh(site)
    assert site.consecutive_errors == 0
    assert site.cooldown_until is None


async def test_tick_skips_cooled_down_site(db, monkeypatch):
    """index_tick must not fetch from a site whose cooldown hasn't elapsed."""
    ensure_source(db, registry.get("web_index"))  # enable the source so the tick runs
    site, page = _seed(db)
    site.cooldown_until = datetime.now(UTC) + timedelta(minutes=10)
    db.commit()
    fetcher = _Fetcher(lambda url: _Resp())
    monkeypatch.setattr(indexer, "get_fetcher", lambda: fetcher)

    await indexer.index_tick()
    await indexer._drain_crawl_tasks()
    db.refresh(page)
    db.refresh(site)
    assert fetcher.calls == []                  # never fetched
    assert page.status == "pending"
    assert site.status == "active"              # paused, not finished


async def test_tick_stays_active_while_pages_await_retry(db, monkeypatch):
    """A site with only not-yet-due pending pages must stay active (it ends on idle discovery,
    not because a retry backoff is in flight)."""
    ensure_source(db, registry.get("web_index"))
    site, page = _seed(db, next_attempt_at=datetime.now(UTC) + timedelta(minutes=10))
    fetcher = _Fetcher(lambda url: _Resp())
    monkeypatch.setattr(indexer, "get_fetcher", lambda: fetcher)

    await indexer.index_tick()
    await indexer._drain_crawl_tasks()
    db.refresh(page)
    db.refresh(site)
    assert fetcher.calls == []
    assert page.status == "pending"
    assert site.status == "active"              # NOT marked done despite no due page


async def test_tick_marks_done_when_frontier_empty(db, monkeypatch):
    ensure_source(db, registry.get("web_index"))
    site, page = _seed(db)
    page.status = "fetched"                     # no pending pages remain
    db.commit()
    fetcher = _Fetcher(lambda url: _Resp())
    monkeypatch.setattr(indexer, "get_fetcher", lambda: fetcher)

    await indexer.index_tick()
    await indexer._drain_crawl_tasks()
    db.refresh(site)
    assert site.status == "done"


# ---- Crawl continuation: index all content, finish only when the frontier is empty ---------
def _site(db, **kw):
    defaults = dict(root_url="https://x.com", domain="x.com", status="active",
                    max_depth=8, max_pages=0, same_host_only=True)
    defaults.update(kw)
    site = IndexSite(**defaults)
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def _page(db, site, url="https://x.com/", depth=0, status="fetched"):
    p = IndexedPage(site_id=site.id, url=url, depth=depth, status=status)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_enqueue_links_discovers_then_dedupes(db):
    site = _site(db)
    page = _page(db, site)
    html = '<a href="/title/a">A</a><a href="/title/b">B</a>'
    added, new = indexer._enqueue_links(db, site, page, html)
    assert new is True and added == 2
    # Same links a second time → already seen, so no new discovery and nothing enqueued.
    added2, new2 = indexer._enqueue_links(db, site, page, html)
    assert added2 == 0 and new2 is False


def test_enqueue_links_reports_discovery_without_enqueuing(db):
    """In a dry spell (discover=False) the page's new links are still REPORTED as discovery —
    so a productive crawl never trips the idle backstop just because the frontier paused."""
    site = _site(db)
    page = _page(db, site)
    html = '<a href="/title/a">A</a>'
    added, new = indexer._enqueue_links(db, site, page, html, discover=False)
    assert added == 0 and new is True
    assert db.scalar(select(func.count(IndexedPage.id)).where(
        IndexedPage.status == "pending")) == 0  # nothing was queued


def test_enqueue_links_depth_floor_for_unlimited(db):
    # Unlimited crawl with a low per-site max_depth still reaches deep pages: depth is floored
    # to the generous global default so paginated/nested content isn't cut off.
    site = _site(db, max_depth=2, max_pages=0)
    deep = _page(db, site, url="https://x.com/deep", depth=5)
    added, new = indexer._enqueue_links(db, site, deep, '<a href="/title/z">Z</a>')
    assert new is True and added == 1
    # A *bounded* crawl honours its per-site depth: depth 5 >= max_depth 2 → no discovery.
    bounded = _site(db, root_url="https://y.com", domain="y.com", max_depth=2, max_pages=100)
    bp = _page(db, bounded, url="https://y.com/deep", depth=5)
    added2, new2 = indexer._enqueue_links(db, bounded, bp, '<a href="/title/z">Z</a>')
    assert added2 == 0 and new2 is False


async def test_tick_revives_done_site_with_queued_pages(db, monkeypatch):
    """A site marked 'done' but still holding queued pages (e.g. stopped early by the old
    idle-stop) is re-activated so its remaining content gets crawled."""
    ensure_source(db, registry.get("web_index"))
    site = _site(db, status="done")
    # Pending but not yet due, so the tick revives the site without needing to fetch.
    p = _page(db, site, url="https://x.com/queued", status="pending")
    p.next_attempt_at = datetime.now(UTC) + timedelta(minutes=10)
    db.commit()
    fetcher = _Fetcher(lambda url: _Resp())
    monkeypatch.setattr(indexer, "get_fetcher", lambda: fetcher)

    await indexer.index_tick()
    await indexer._drain_crawl_tasks()
    db.refresh(site)
    assert site.status == "active"   # revived
    assert fetcher.calls == []       # page wasn't due, so nothing fetched


async def test_tick_leaves_truly_finished_site_done(db, monkeypatch):
    """A site whose frontier is genuinely empty stays done (no queued pages → not revived)."""
    ensure_source(db, registry.get("web_index"))
    site = _site(db, status="done")
    _page(db, site, url="https://x.com/done", status="fetched")  # no pending pages
    fetcher = _Fetcher(lambda url: _Resp())
    monkeypatch.setattr(indexer, "get_fetcher", lambda: fetcher)

    await indexer.index_tick()
    await indexer._drain_crawl_tasks()
    db.refresh(site)
    assert site.status == "done"
