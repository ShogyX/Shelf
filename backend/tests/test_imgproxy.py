"""Sweep regression: the /cover proxy must not NameError on a remote-cover cache miss.

The endpoint used `await asyncio.to_thread(...)` but `asyncio` was only imported inside a DIFFERENT
function in the module, so a remote (non-local) cover URL raised NameError at runtime."""
from __future__ import annotations

import pytest
from fastapi.responses import FileResponse, RedirectResponse


@pytest.mark.asyncio
async def test_cover_proxy_remote_miss_does_not_nameerror(monkeypatch):
    from app import imagecache
    from app.media import media_dir
    from app.routers.imgproxy import cover_image

    d = media_dir() / "imgcache"
    d.mkdir(parents=True, exist_ok=True)
    (d / "swept.jpg").write_bytes(b"\xff\xd8\xff\xe0jpg")
    # cache_image runs in a thread via asyncio.to_thread — the bug was asyncio being undefined here.
    monkeypatch.setattr(imagecache, "cache_image", lambda u, **k: "/media/imgcache/swept.jpg")

    resp = await cover_image(u="https://example.com/remote-cover.jpg")
    assert isinstance(resp, FileResponse)  # served the cached file (no NameError on the remote path)


@pytest.mark.asyncio
async def test_cover_proxy_local_url_redirects():
    from app.routers.imgproxy import cover_image
    resp = await cover_image(u="/media/comics/x/0001.jpg")
    assert isinstance(resp, RedirectResponse)  # local path → straight redirect, no fetch
