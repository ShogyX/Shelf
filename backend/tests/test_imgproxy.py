"""/cover image proxy: the remote-miss path (regression) + the SEC-M1 cover-host allowlist."""
from __future__ import annotations

import pytest
from fastapi.responses import FileResponse, RedirectResponse

from app.db import SessionLocal, init_db
from app.routers import imgproxy
from app.routers.imgproxy import cover_image


@pytest.mark.asyncio
async def test_cover_proxy_remote_miss_does_not_nameerror(monkeypatch):
    """A remote (non-local) cover URL on an ALLOWED host must fetch+serve without erroring (the old
    bug was `asyncio` undefined on this path)."""
    from app import imagecache
    from app.media import media_dir
    init_db()
    db = SessionLocal()
    imgproxy._cover_hosts_cache = (-1e9, frozenset())

    d = media_dir() / "imgcache"
    d.mkdir(parents=True, exist_ok=True)
    (d / "swept.jpg").write_bytes(b"\xff\xd8\xff\xe0jpg")
    monkeypatch.setattr(imagecache, "cache_image", lambda u, **k: "/media/imgcache/swept.jpg")

    resp = await cover_image(u="https://covers.openlibrary.org/b/id/1.jpg", db=db)
    assert isinstance(resp, FileResponse)  # allowed host → served the cached file
    db.close()


@pytest.mark.asyncio
async def test_cover_proxy_local_url_redirects():
    init_db()
    db = SessionLocal()
    resp = await cover_image(u="/media/comics/x/0001.jpg", db=db)
    assert isinstance(resp, RedirectResponse)  # local path → straight redirect, no fetch / no host check
    db.close()


@pytest.mark.asyncio
async def test_cover_proxy_rejects_arbitrary_host():
    """SEC-M1: a remote URL on a host that isn't a known cover source is refused (no open egress proxy)."""
    from fastapi import HTTPException
    init_db()
    db = SessionLocal()
    imgproxy._cover_hosts_cache = (-1e9, frozenset())
    with pytest.raises(HTTPException) as ei:
        await cover_image(u="https://evil.example.com/track?id=1", db=db)
    assert ei.value.status_code == 403
    db.close()


def test_rewrite_hotlinked_proxies_protocol_relative_src():
    # IMG-1: a protocol-relative //host/x.jpg on an allowlisted hotlink CDN must be routed through the
    # proxy (previously skipped because repl only matched http(s)).
    html = '<img src="//i.pstatic.net/img/a.jpg">'
    out = imgproxy.rewrite_hotlinked(html)
    assert "/api/img?u=" in out, out
    # a non-allowlisted protocol-relative src is left untouched.
    assert imgproxy.rewrite_hotlinked('<img src="//evil.example.com/x.jpg">') == \
        '<img src="//evil.example.com/x.jpg">'


def test_cover_host_allowlist():
    init_db()
    db = SessionLocal()
    imgproxy._cover_hosts_cache = (-1e9, frozenset())
    # Fixed metadata cover CDNs + hotlink CDNs allowed (suffix match covers subdomains).
    assert imgproxy._cover_host_allowed("https://covers.openlibrary.org/b/id/1.jpg", db)
    assert imgproxy._cover_host_allowed("https://books.google.com/books/content?id=x", db)
    assert imgproxy._cover_host_allowed("https://i.pstatic.net/img.jpg", db)
    assert imgproxy._cover_host_allowed("https://s4.anilist.co/file/cover.jpg", db)
    # Arbitrary / internal hosts refused.
    assert not imgproxy._cover_host_allowed("https://evil.example.com/track?id=1", db)
    assert not imgproxy._cover_host_allowed("https://169.254.169.254/latest/meta-data", db)
    db.close()


def test_cover_allowlist_picks_up_crawled_source_domains():
    """A crawled source's domain is added to the allowlist dynamically (so its covers can localize)."""
    from sqlalchemy import insert
    from app.models import Source
    init_db()
    db = SessionLocal()
    db.execute(insert(Source).values(key="rr-test", display_name="RR", adapter_key="royalroad",
                                     base_url="https://www.royalroad.com", license_basis="permitted"))
    db.commit()
    imgproxy._cover_hosts_cache = (-1e9, frozenset())
    assert imgproxy._cover_host_allowed("https://www.royalroad.com/cover.jpg", db)
    assert imgproxy._cover_host_allowed("https://cdn.royalroad.com/cover.jpg", db)  # subdomain via suffix
    db.close()


def test_cache_image_threads_host_ok_to_fetch(monkeypatch):
    """SEC-M1: cache_image passes host_ok down to _fetch_image (which re-checks every redirect hop)."""
    from app import imagecache
    captured = {}
    def fake_fetch(url, referer, *, _depth=0, host_ok=None):
        captured["host_ok"] = host_ok
        return imagecache.PERMANENT_FAIL
    monkeypatch.setattr(imagecache, "_fetch_image", fake_fetch)
    pred = lambda u: False
    imagecache.cache_image("https://covers.openlibrary.org/x.jpg", host_ok=pred)
    assert captured["host_ok"] is pred


def test_cover_host_allowed_cached_is_dbless(monkeypatch):
    """The redirect predicate reads ONLY the primed cache (no DB → safe in the download worker thread)."""
    init_db()
    db = SessionLocal()
    imgproxy._cover_hosts_cache = (-1e9, frozenset())
    imgproxy._allowed_cover_hosts(db)  # prime the cache in the request thread
    assert imgproxy.cover_host_allowed_cached("https://covers.openlibrary.org/x.jpg")
    assert not imgproxy.cover_host_allowed_cached("https://evil.example.com/x")
    db.close()
