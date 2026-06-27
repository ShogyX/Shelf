"""SSRF guard on the reading-list importer: user-supplied list URLs are fetched server-side, so they
must never reach an internal/metadata address, and the full-URL Amazon provider must stay on amazon.*."""
import httpx
import pytest

from app.ingestion import list_import as li, netguard


@pytest.mark.asyncio
async def test_ssrf_guard_blocks_internal_addresses():
    # The per-request hook (run on every hop, incl. redirects) rejects link-local / loopback / RFC-1918.
    for url in ("http://169.254.169.254/latest/meta-data/",   # cloud metadata
                "http://127.0.0.1/admin", "http://10.0.0.5/", "http://[::1]/"):
        with pytest.raises(netguard.BlockedAddress):
            await li._ssrf_guard(httpx.Request("GET", url))
    # A non-http(s) scheme is refused too.
    with pytest.raises(netguard.BlockedAddress):
        await li._ssrf_guard(httpx.Request("GET", "file:///etc/passwd"))


@pytest.mark.asyncio
async def test_amazon_wishlist_allowlists_amazon_host():
    # The full-URL provider must refuse a non-amazon host BEFORE any fetch (so it can't be a generic
    # SSRF/URL-fetcher) — including an internal address and an arbitrary external one.
    for ref in ("http://169.254.169.254/latest/meta-data/",
                "http://evil.example.com/hz/wishlist/ls/X",
                "https://amazon.evil.com/hz/wishlist/ls/X"):   # not a real amazon.* registrable domain
        with pytest.raises(li.ListImportError):
            await li.fetch_list("amazon_wishlist", ref)


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_ssrf_guard_blocks_internal_addresses())
    asyncio.run(test_amazon_wishlist_allowlists_amazon_host())
    print("ok")
