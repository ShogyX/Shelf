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
from app.routers.index import _status_reason

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
