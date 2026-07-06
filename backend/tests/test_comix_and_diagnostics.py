"""comix.to manga adapter (metadata API + DOM scrape + page enumeration, mocked), J-Novel
per-source auth, and crawl status-reason diagnostics."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
import app.ingestion.adapters.comix as comix_mod
from app.ingestion.adapters.comix import ComixAdapter, _hid, _series_ref
from app.ingestion.adapters.jnovel import JNovelClubAdapter
from app.ingestion.extract import detect_media_kind
from app.main import app
from app.models import Source, User, UserSession
from app.ingestion.index_admin import _status_reason

SLUG = "nxy5-jujutsu-kaisen-modulo"


class _Resp:
    def __init__(self, *, body_text="", text=""):
        self.body_text = body_text
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        pass


# ----------------------------------------------------------------- comix.to adapter
class _FakeComixFetcher:
    """Mimics the render path: metadata URL → JSON body_text; chapter-list pages + reader → HTML."""
    async def get_html(self, source_key, url, *, force_render=False, scroll=0, headers=None, **kw):
        assert force_render, "comix must force the headless browser"
        if "/api/v1/manga/nxy5" in url:
            return _Resp(body_text=json.dumps({"result": {
                "id": 1, "hid": "nxy5", "title": "Jujutsu Kaisen Modulo", "status": "completed",
                "synopsis": "Curses, modulo.", "poster": {"large": "https://static.comix.to/c.jpg"},
                "url": f"/title/{SLUG}",
            }}))
        if url.endswith("?page=1"):
            return _Resp(text=(f'<a href="/title/{SLUG}/100-chapter-1">Ch1</a>'
                               f'<a href="/title/{SLUG}/220-chapter-2">Ch2</a>'
                               f'<a href="/title/{SLUG}/220-chapter-2">dup group</a>'))
        if url.endswith("?page=2"):
            return _Resp(text="<div>no chapter links here</div>")  # nothing new → stop
        if f"/title/{SLUG}/100-chapter-1" in url:  # the reader page
            return _Resp(text='<img src="https://jloo.wowpic.store/i3/TOK/01.webp"/>')
        raise AssertionError(f"unexpected {url}")


class _FakeHead:
    """Fake httpx.AsyncClient: pages 01..03 exist, then 404 (chapter has 3 pages)."""
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def head(self, u):
        import re
        m = re.search(r"/(\d+)\.webp", u)
        n = int(m.group(1)) if m else 999
        return SimpleNamespace(status_code=200 if n <= 3 else 404)


@pytest.fixture
def comix():
    return ComixAdapter(_FakeComixFetcher())


def test_series_ref_and_hid_parsing():
    assert _series_ref(f"https://comix.to/title/{SLUG}") == SLUG
    assert _series_ref(f"https://comix.to/title/{SLUG}/10-chapter-5") == SLUG
    assert _hid(f"https://comix.to/title/{SLUG}") == "nxy5"
    assert _hid("nxy5") == "nxy5"


def test_comix_catalog_browser_ingest(monkeypatch):
    """comix.to is crawled through the browser (Cloudflare Turnstile + signed-token API). Verifies the
    browse cards upsert as comics, dedup by work_url, cover capture, cursor advance + completion."""
    import asyncio
    from app.ingestion import comix_catalog as cc
    from app.models import CatalogWork, IndexSite

    init_db()
    db = SessionLocal()
    site = db.scalar(select(IndexSite).where(IndexSite.domain == "comix.to"))
    if site is None:
        site = IndexSite(root_url="https://comix.to/", domain="comix.to", status="active")
        db.add(site); db.commit()
    db.execute(delete(CatalogWork).where(CatalogWork.site_id == site.id))
    site.api_cursor = None; site.api_synced_at = None; db.commit()

    async def _fake_crawl(start, count):
        return {"cards": [
            {"url": "https://comix.to/title/pvry-one-piece", "hid": "pvry", "slug": "one-piece",
             "title": "One Piece", "cover": "https://static.comix.to/op.jpg"},
            {"url": "https://comix.to/title/z9-solo-leveling", "hid": "z9", "slug": "solo-leveling",
             "title": "Solo Leveling", "cover": None},
            # duplicate of One Piece → must dedup by work_url (no second row)
            {"url": "https://comix.to/title/pvry-one-piece", "hid": "pvry", "slug": "one-piece",
             "title": "One Piece", "cover": "https://static.comix.to/op.jpg"},
        ], "pages": 1, "ended": True}
    monkeypatch.setattr(cc, "_browser_crawl", _fake_crawl)

    out = asyncio.run(cc.ingest_tick(db, site, max_pages=5))
    assert out["done"] is True and site.api_cursor == 0  # empty/ended crawl → parked
    rows = db.scalars(select(CatalogWork).where(CatalogWork.site_id == site.id)).all()
    titles = {w.title: w for w in rows}
    assert set(titles) == {"One Piece", "Solo Leveling"}     # duplicate deduped
    assert len(rows) == 2
    op = titles["One Piece"]
    assert op.media_kind == "comic" and op.work_url == "https://comix.to/title/pvry-one-piece"
    assert op.cover_url == "https://static.comix.to/op.jpg" and (op.extra or {}).get("hid") == "pvry"

    # A completed pass stamps the sync time; a re-run is a no-op until due.
    assert site.api_synced_at is not None and cc.is_due(site) is False
    out2 = asyncio.run(cc.ingest_tick(db, site, max_pages=5))
    assert out2["done"] is True and out2["created"] == 0
    db.execute(delete(CatalogWork).where(CatalogWork.site_id == site.id)); db.commit(); db.close()


def test_comix_browser_ingest_captures_popularity(monkeypatch):
    """The browser crawl now reads the SPA's own /api/v1/manga payload (full metadata). Verifies that
    rich API ``items`` upsert with popularity/rating/year (so titles RANK instead of stranding at
    popularity 0 and getting buried), prefer the localized durable cover, and dedup by work_url
    against a DOM-only fallback card for the same title."""
    import asyncio
    from app.ingestion import comix_catalog as cc
    from app.models import CatalogWork, IndexSite

    init_db()
    db = SessionLocal()
    site = db.scalar(select(IndexSite).where(IndexSite.domain == "comix.to"))
    if site is None:
        site = IndexSite(root_url="https://comix.to/", domain="comix.to", status="active")
        db.add(site); db.commit()
    db.execute(delete(CatalogWork).where(CatalogWork.site_id == site.id))
    site.api_cursor = None; site.api_synced_at = None; db.commit()

    async def _fake_crawl(start, count):
        return {
            "items": [
                {"hid": "pvry", "url": "/title/pvry-one-piece", "title": "One Piece", "type": "manga",
                 "year": 1997, "followsTotal": 123456, "ratedAvg": 9.3, "ratedCount": 4242,
                 "poster": {"large": "https://static.comix.to/op.jpg"}, "_cover": "/covers/op.jpg"},
            ],
            # DOM-only fallback card for the SAME title → must dedup, not create a 2nd row.
            "cards": [
                {"url": "https://comix.to/title/pvry-one-piece", "hid": "pvry", "slug": "one-piece",
                 "title": "One Piece", "cover": "https://static.comix.to/op.jpg"},
            ],
            "pages": 1, "ended": True,
        }
    monkeypatch.setattr(cc, "_browser_crawl", _fake_crawl)

    out = asyncio.run(cc.ingest_tick(db, site, max_pages=5))
    rows = db.scalars(select(CatalogWork).where(CatalogWork.site_id == site.id)).all()
    assert len(rows) == 1 and out["created"] == 1          # API item + dup card → one row
    op = rows[0]
    assert op.title == "One Piece" and op.work_url == "https://comix.to/title/pvry-one-piece"
    assert op.popularity == 123456.0 and op.rating == 9.3 and op.rating_count == 4242
    assert op.year == 1997
    assert op.cover_url == "/covers/op.jpg"                # localized durable cover preferred
    db.execute(delete(CatalogWork).where(CatalogWork.site_id == site.id)); db.commit(); db.close()


def test_comix_crawl_skips_html_frontier(monkeypatch):
    """REGRESSION: an API/browser-catalog site (comix.to) must NOT HTML-crawl its frontier. Its
    per-title/user/group pages are Cloudflare-gated; draining them only churns 403s that pause the
    whole site and starve the browser catalog crawl. ``_crawl_site`` must ingest the catalog and
    STOP — never call the page fetcher — even with pending HTML pages queued."""
    import asyncio
    from app.ingestion import comix_catalog as cc
    from app.ingestion import indexer
    from app.models import IndexedPage, IndexSite, Source

    init_db()
    db = SessionLocal()
    src = db.scalar(select(Source).where(Source.key == indexer.SOURCE_KEY))
    if src is None:
        indexer.ensure_source(db, indexer._web_index_adapter_cls())
        src = db.scalar(select(Source).where(Source.key == indexer.SOURCE_KEY))
    site = db.scalar(select(IndexSite).where(IndexSite.domain == "comix.to"))
    if site is None:
        site = IndexSite(root_url="https://comix.to/", domain="comix.to", status="active")
        db.add(site); db.commit()
    site.status = "active"; site.api_cursor = None; site.api_synced_at = None
    db.execute(delete(IndexedPage).where(IndexedPage.site_id == site.id))
    db.add(IndexedPage(site_id=site.id, url="https://comix.to/title/abc-x", status="pending",
                       depth=1, priority=2))
    db.commit()
    sid = site.id
    db.close()

    ingested = {"n": 0}
    async def _fake_ingest(db, site, **kw):
        ingested["n"] += 1
        return {"created": 0, "scanned": 0, "done": True}
    def _boom(*a, **k):
        raise AssertionError("HTML frontier must NOT be fetched for an api-catalog site")
    monkeypatch.setattr(cc, "ingest_tick", _fake_ingest)
    monkeypatch.setattr(indexer, "_fetch_one", _boom)

    asyncio.run(indexer._crawl_site(sid, batch=5))
    assert ingested["n"] == 1                       # catalog ingest ran exactly once
    db = SessionLocal()
    pend = db.scalar(select(IndexedPage).where(IndexedPage.site_id == sid,
                                               IndexedPage.status == "pending"))
    assert pend is not None                          # the pending HTML page was left untouched, not fetched
    db.execute(delete(IndexedPage).where(IndexedPage.site_id == sid)); db.commit(); db.close()


def test_comix_catalog_crawl_failure_backs_off(monkeypatch):
    """A browser-crawl failure must NOT mark the catalog synced (which would drop the rest) — it
    backs off and keeps the cursor; a non-ended batch advances the cursor without completing."""
    import asyncio
    from app.ingestion import comix_catalog as cc
    from app.models import CatalogWork, IndexSite

    init_db()
    db = SessionLocal()
    site = db.scalar(select(IndexSite).where(IndexSite.domain == "comix.to"))
    if site is None:
        site = IndexSite(root_url="https://comix.to/", domain="comix.to", status="active")
        db.add(site); db.commit()
    db.execute(delete(CatalogWork).where(CatalogWork.site_id == site.id))
    site.api_cursor = None; site.api_synced_at = None; site.cooldown_until = None; db.commit()

    # Case A: crawl failure (None) → no completion, cursor stays 1, site cooled down.
    async def _fail(start, count):
        return None
    monkeypatch.setattr(cc, "_browser_crawl", _fail)
    out = asyncio.run(cc.ingest_tick(db, site, max_pages=5))
    assert out["done"] is False and site.api_cursor == 1 and site.api_synced_at is None
    assert site.cooldown_until is not None  # backoff set — won't spin every tick

    # Case B: a productive but NOT-ended batch advances the cursor past the crawled pages.
    site.api_cursor = None; site.cooldown_until = None; db.commit()
    async def _partial(start, count):
        return {"cards": [{"url": "https://comix.to/title/k-kingdom", "hid": "k", "slug": "kingdom",
                           "title": "Kingdom", "cover": None}], "pages": count, "ended": False}
    monkeypatch.setattr(cc, "_browser_crawl", _partial)
    out = asyncio.run(cc.ingest_tick(db, site, max_pages=5))
    assert out["done"] is False and site.api_synced_at is None and site.api_cursor == 6  # 1 + 5
    db.execute(delete(CatalogWork).where(CatalogWork.site_id == site.id)); db.commit(); db.close()


def test_comix_browser_crawl_tolerates_stdout_pollution(monkeypatch):
    """zendriver/Chrome can print a benign diagnostic (e.g. "no Cloudflare challenge appeared") to the
    subprocess's stdout AHEAD of the JSON payload. _browser_crawl must still recover the payload — a
    whole-stdout json.loads used to choke here and discard a SUCCESSFUL crawl ("produced no JSON"),
    which silently stalled the entire comix.to catalog sync."""
    import asyncio
    import json as _json
    from app.ingestion import comix_catalog as cc

    payload = {"items": [{"hid": "gdk7", "url": "/title/gdk7-vagabond", "title": "Vagabond",
                          "type": "manga"}], "cards": [], "pages": 1, "ended": True}
    polluted = (b"Timeout: Cloudflare challenge elements not found or not visible within 15 seconds.\n"
                + _json.dumps(payload).encode() + b"\n")

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return polluted, b""

    async def _fake_exec(*a, **k):
        return _FakeProc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    out = asyncio.run(cc._browser_crawl(1, 1))
    assert out is not None and out["ended"] is True
    assert out["items"][0]["title"] == "Vagabond"


def test_comix_chapter_urls_collapse_to_series_not_crawled():
    """The index crawler must treat comix.to /title/<slug>/<chapter-id> reader URLs as chapters
    (they 404 for a plain fetch) and collapse them to the series page, instead of enqueueing
    thousands of dead reader URLs (the cause of ~865 'permanent: HTTP 404' index failures)."""
    from app.ingestion.extract import is_chapter_url, is_work_url, work_url_for
    series = f"https://comix.to/title/{SLUG}"
    for chap in (f"{series}/8617006", f"{series}/9661540", f"{series}/10-chapter-5"):
        assert is_chapter_url(chap) and not is_work_url(chap)
        assert work_url_for(chap) == series  # collapses to the already-indexed series page
    # The series landing itself stays a crawlable work, not a chapter.
    assert is_work_url(series) and not is_chapter_url(series)
    assert work_url_for(series) == series


async def test_comix_discover_work(comix):
    meta = await comix.discover_work(f"https://comix.to/title/{SLUG}")
    assert meta.media_kind == "comic"
    assert meta.title == "Jujutsu Kaisen Modulo"
    assert meta.status == "complete"
    assert meta.source_work_ref == SLUG
    assert meta.cover_url == "https://static.comix.to/c.jpg"
    assert "modulo" in (meta.description or "").lower()


async def test_comix_list_chapters_dedup_and_order(comix):
    meta = await comix.discover_work(f"https://comix.to/title/{SLUG}")
    refs = await comix.list_chapters(meta)
    # Two distinct chapter numbers (the duplicate scanlation group is de-duped), oldest-first.
    assert [r.title for r in refs] == ["Chapter 1", "Chapter 2"]
    assert refs[0].source_chapter_ref == f"/title/{SLUG}/100-chapter-1"
    assert [r.index for r in refs] == [1, 2]


class _PhantomFetcher:
    """A chapter list whose page chrome links, on EVERY page, both a far-outlier phantom (chapter
    999, with NO neighbours in the real run) AND the real latest chapter (3, as a 'read latest'
    button). Real chapters 3,2,1 paginate one per page."""
    _REAL = {1: "10", 2: "20", 3: "30"}  # chapter number -> id

    async def get_html(self, source_key, url, *, force_render=False, scroll=0, headers=None, **kw):
        if f"/api/v1/manga/nxy5" in url:
            return _Resp(body_text=json.dumps({"result": {
                "hid": "nxy5", "title": "Phantom Test", "status": "ongoing",
                "url": f"/title/{SLUG}"}}))
        chrome = (f'<a href="/title/{SLUG}/9000-chapter-999">phantom</a>'
                  f'<a href="/title/{SLUG}/30-chapter-3">read latest</a>')  # latest recurs every page
        for page, num in ((1, 3), (2, 2), (3, 1)):
            if url.endswith(f"?page={page}"):
                cid = self._REAL[num]
                return _Resp(text=chrome + f'<a href="/title/{SLUG}/{cid}-chapter-{num}">Ch{num}</a>')
        return _Resp(text=chrome)  # page 4: only chrome, no new real chapter → stop


async def test_comix_list_chapters_drops_phantom_but_keeps_real_latest():
    """A 'read latest' link the site repeats on every page is UI, not a paginated chapter — but it
    usually targets the REAL latest chapter, so recurrence alone can't condemn it. Only a recurring
    number that is also a far OUTLIER is a phantom (the One Piece (Official Colored) bug: real run
    topped 1076 but a phantom 1181 sat on every page). The real latest (here ch 3, which recurs as a
    'read latest' button) must survive because it sits right above the rest of the run."""
    adapter = ComixAdapter(_PhantomFetcher())
    meta = await adapter.discover_work(f"https://comix.to/title/{SLUG}")
    refs = await adapter.list_chapters(meta)
    nums = [r.title for r in refs]
    assert nums == ["Chapter 1", "Chapter 2", "Chapter 3"]  # real run intact, incl. the latest
    assert "Chapter 999" not in nums  # far-outlier phantom dropped


async def test_comix_fetch_chapter_enumerates_pages(comix, monkeypatch):
    monkeypatch.setattr(comix_mod.httpx, "AsyncClient", _FakeHead)
    raw = await comix.fetch_chapter(
        SimpleNamespace(source_chapter_ref=f"/title/{SLUG}/100-chapter-1", title="Chapter 1")
    )
    assert raw.body.count("<figure") == 3  # enumerated 01,02,03 then stopped at 404
    assert "https://jloo.wowpic.store/i3/TOK/01.webp" in raw.body
    assert "https://jloo.wowpic.store/i3/TOK/03.webp" in raw.body


def test_detect_media_kind_comix_domain():
    assert detect_media_kind("https://comix.to/title/nxy5-jjk") == "comic"
    assert detect_media_kind("https://example.com/title/foo") == "text"


def test_comix_cover_fallback_from_poster():
    """comix.to sets no og:image; the cover is pulled from the static poster img (full-res)."""
    from app.ingestion.catalog import _comix_cover
    html = ('<head></head><body>'
            '<img src="https://static.comix.to/784a/i/b/07/68e1198690485@280.jpg"/>'
            '<img src="https://static.comix.to/0ab5/i/5/3d/recommended@280.jpg"/></body>')
    assert _comix_cover(html) == "https://static.comix.to/784a/i/b/07/68e1198690485.jpg"
    assert _comix_cover('<img src="https://example.com/x.jpg">') is None


def test_indexer_auto_renders_comix_only():
    """The index crawler JS-renders comix.to (an SPA) but leaves other sites on plain HTTP."""
    from app.ingestion.indexer import _needs_render
    assert _needs_render("https://comix.to/browse?types=manga") is True
    assert _needs_render("https://comix.to/title/nxy5-jjk") is True
    assert _needs_render("https://www.comix.to/title/x") is True
    assert _needs_render("https://example.com/manga/x") is False
    assert _needs_render("https://standardebooks.org/ebooks") is False


# ----------------------------------------------------------------- J-Novel auth
def test_jnovel_auth_header_from_config():
    a = JNovelClubAdapter(_FakeComixFetcher(), config={"auth_token": "TOK123"})
    assert a._auth_headers() == {"Authorization": "Bearer TOK123"}
    # No config + no env → no auth header.
    b = JNovelClubAdapter(_FakeComixFetcher(), config={})
    assert b._auth_headers() == {} or "Authorization" not in b._auth_headers()


# ----------------------------------------------------------------- status-reason diagnostics
def test_status_reason_explains_states():
    from datetime import UTC, datetime, timedelta
    now = datetime(2026, 6, 3, tzinfo=UTC)

    cooling = SimpleNamespace(status="active", last_error="blocked: 403", stop_after_idle_pages=0,
                              pages_since_new_title=0)
    assert "Cooling down" in _status_reason(cooling, {"pending": 5}, now + timedelta(minutes=10), now)

    allfail = SimpleNamespace(status="done", last_error=None, stop_after_idle_pages=0,
                              pages_since_new_title=0)
    assert "every request failed" in _status_reason(allfail, {"failed": 7}, None, now)

    idle = SimpleNamespace(status="done", last_error=None, stop_after_idle_pages=50,
                           pages_since_new_title=60)
    assert "no new titles" in _status_reason(idle, {"fetched": 100}, None, now)


# ----------------------------------------------------------------- source auth API (masked)
@pytest.fixture
def admin():
    init_db()
    db = SessionLocal()
    for m in (UserSession, User):
        db.execute(delete(m))
    db.commit()
    db.close()
    c = TestClient(app)
    c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
    return c


def test_source_auth_token_stored_but_never_returned(admin):
    sources = admin.get("/api/sources").json()
    jnovel = next((s for s in sources if s["key"] == "jnovel"), None)
    assert jnovel is not None
    assert jnovel["supports_auth"] is True and jnovel["has_auth"] is False
    assert "config" not in jnovel and "auth_token" not in jnovel  # secret never serialized

    out = admin.patch(f"/api/sources/{jnovel['id']}", json={"auth_token": "secret-tok"}).json()
    assert out["has_auth"] is True
    assert "auth_token" not in out and "config" not in out

    # Verify it actually landed in Source.config (server-side) and the adapter would use it.
    db = SessionLocal()
    src = db.scalar(select(Source).where(Source.key == "jnovel"))
    assert (src.config or {}).get("auth_token") == "secret-tok"
    db.close()

    # Clearing it works.
    cleared = admin.patch(f"/api/sources/{jnovel['id']}", json={"auth_token": ""}).json()
    assert cleared["has_auth"] is False
