"""SSRF egress guard — only public http(s) hosts may be fetched."""
from __future__ import annotations

import pytest

from app.ingestion.netguard import BlockedAddress, assert_public_url, is_public_url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://localhost/admin",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/",
    "http://[::1]/", "http://0.0.0.0/",
    "file:///etc/passwd", "ftp://example.com/", "gopher://x/",
])
def test_blocks_internal_and_nonhttp(url):
    assert is_public_url(url) is False
    with pytest.raises(BlockedAddress):
        assert_public_url(url)


def test_allows_public_host():
    # A well-known public host resolves to a global address.
    assert is_public_url("https://www.gutenberg.org/ebooks/1342") is True


# ---- safe_get: the SSRF-guarded sync fetch chokepoint ----------------------------------------

def test_safe_get_blocks_internal_target():
    from app.ingestion.netguard import safe_get
    with pytest.raises(BlockedAddress):
        safe_get("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(BlockedAddress):
        safe_get("http://127.0.0.1/")


def test_safe_get_blocks_public_to_internal_redirect(monkeypatch):
    """A PUBLIC host 302ing to an internal address must be blocked at the hop — the classic
    SSRF-via-redirect that follow_redirects=True would have followed blindly."""
    import httpx
    from app.ingestion import netguard

    # First URL passes the public check; the response redirects to the metadata service.
    monkeypatch.setattr(netguard, "assert_public_url", _public_then_real(netguard))

    def fake_get(self, url):
        if "public.example" in url:
            return httpx.Response(302, headers={"location": "http://169.254.169.254/secrets"},
                                  request=httpx.Request("GET", url))
        raise AssertionError("must not fetch the internal hop")
    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with pytest.raises(BlockedAddress):
        netguard.safe_get("http://public.example/img.jpg")


def _public_then_real(netguard):
    """assert_public_url stub: treat public.example as public; validate IP-literal hosts for
    real (so the internal redirect target still raises)."""
    def check(url):
        if "public.example" in url:
            return
        import ipaddress
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return                                  # non-literal host: out of scope for this stub
        if not netguard._ip_is_public(host):        # 169.254.169.254 is link-local → blocked
            raise BlockedAddress(f"blocked internal address {host}")
    return check


def test_safe_get_follows_legit_public_redirect(monkeypatch):
    """Legitimate image CDNs 302 between public hosts — safe_get must follow (re-validated),
    not refuse outright."""
    import httpx
    from app.ingestion import netguard
    monkeypatch.setattr(netguard, "assert_public_url", lambda url: None)  # all hops "public"

    def fake_get(self, url):
        if url.endswith("/start"):
            return httpx.Response(302, headers={"location": "https://cdn.example/final.jpg"},
                                  request=httpx.Request("GET", url))
        return httpx.Response(200, content=b"IMAGEBYTES",
                              headers={"content-type": "image/jpeg"},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.Client, "get", fake_get)
    r = netguard.safe_get("https://img.example/start")
    assert r.status_code == 200 and r.content == b"IMAGEBYTES"


def test_epub_export_image_fetch_is_guarded(monkeypatch):
    """resolve_image_bytes / _load_cover must refuse internal URLs (and not crash the export)."""
    from app import epub_export
    called = []

    def boom(url, **kw):
        called.append(url)
        raise BlockedAddress("blocked")
    monkeypatch.setattr(epub_export, "safe_get", boom)
    assert epub_export.resolve_image_bytes("http://169.254.169.254/x.jpg", {}) is None
    assert epub_export._load_cover("http://10.0.0.5/cover.jpg") is None
    assert len(called) == 2   # both paths actually routed through the guard
