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
