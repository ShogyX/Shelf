"""Permanent local image cache: localize remote covers/chapter images, skip local ones."""
from __future__ import annotations

import app.imagecache as ic


class _Resp:
    def __init__(self, status=200, ctype="image/jpeg", content=b"\xff\xd8\xff\xe0jpegdata"):
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.content = content


class _Client:
    def __init__(self, resp): self.resp = resp; self.is_closed = False; self.calls = 0
    def get(self, url, headers=None): self.calls += 1; return self.resp


def _patch(monkeypatch, resp):
    client = _Client(resp)
    monkeypatch.setattr(ic, "_get_client", lambda: client)
    # don't let the SSRF guard do real DNS in the unit test
    monkeypatch.setattr(ic, "assert_public_url", lambda u: None)
    return client


def test_is_remote_and_local_passthrough():
    assert ic.is_remote("https://x/a.jpg") and not ic.is_remote("/media/x.jpg")
    assert ic.cache_image("/media/local.jpg") == "/media/local.jpg"  # local → unchanged


def test_caches_remote_image_once(monkeypatch, tmp_path):
    monkeypatch.setattr(ic, "media_dir", lambda: tmp_path)
    client = _patch(monkeypatch, _Resp())
    url = "https://cdn.example.com/cover123.jpg"
    local = ic.cache_image(url)
    assert local.startswith("/media/imgcache/") and local.endswith(".jpg")
    assert (tmp_path / "imgcache").glob("*.jpg")
    # Second call is served from disk — no second download.
    assert ic.cache_image(url) == local
    assert client.calls == 1


def test_permanent_fail_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(ic, "media_dir", lambda: tmp_path)
    client = _patch(monkeypatch, _Resp(status=404))
    url = "https://cdn.example.com/missing.jpg"
    assert ic.cache_image(url) == ic.PERMANENT_FAIL
    # The .fail marker means we never hit the network again for this URL.
    assert ic.cache_image(url) == ic.PERMANENT_FAIL
    assert client.calls == 1


def test_localize_html_rewrites_remote_only(monkeypatch, tmp_path):
    monkeypatch.setattr(ic, "media_dir", lambda: tmp_path)
    _patch(monkeypatch, _Resp())
    html = (
        '<div class="comic"><img src="https://cdn/p1.jpg"/>'
        '<img src="/media/comics/x/0001.jpg"/><img src="/api/img?u=z"/></div>'
    )
    out = ic.localize_html_images(html)
    assert "/media/imgcache/" in out                 # remote one localized
    assert '/media/comics/x/0001.jpg' in out          # already-local left alone
    assert '/api/img?u=z' in out                      # proxied left alone
    assert "https://cdn/p1.jpg" not in out
