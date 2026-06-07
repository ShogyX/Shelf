"""Hardcover.app metadata/discovery provider: token handling, search parsing, catalog mapping."""
from __future__ import annotations

import pytest

from app.integrations import metadata as md


class FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._p


_DOC = {
    "id": 123, "slug": "the-way-of-kings", "title": "The Way of Kings",
    "author_names": ["Brandon Sanderson"], "isbns": ["9780765326355"],
    "series_names": ["The Stormlight Archive"], "release_year": 2010,
    "users_count": 42000, "image": {"url": "https://hc/img.jpg"},
    "description": "Epic fantasy.",
}


def test_norm_token_strips_bearer():
    assert md._hc_norm_token("Bearer abc123") == "abc123"
    assert md._hc_norm_token("  abc123 ") == "abc123"
    assert md._hc_norm_token(None) == ""


def test_hc_hits_handles_dict_and_json_string():
    import json
    as_dict = {"search": {"results": {"hits": [{"document": _DOC}]}}}
    as_str = {"search": {"results": json.dumps({"hits": [{"document": _DOC}]})}}
    assert md._hc_hits(as_dict)[0]["title"] == "The Way of Kings"
    assert md._hc_hits(as_str)[0]["title"] == "The Way of Kings"
    assert md._hc_hits({"search": {"results": "not json"}}) == []
    assert md._hc_hits({}) == []


@pytest.mark.asyncio
async def test_provider_search_maps_documents(monkeypatch):
    prov = md.HardcoverProvider(api_key="Bearer tok")

    async def fake_post(self, url, **kw):
        assert kw["headers"]["Authorization"] == "Bearer tok"   # normalized, single prefix
        assert "search" in kw["json"]["query"]
        return FakeResp({"data": {"search": {"results": {"hits": [{"document": _DOC}]}}}})

    monkeypatch.setattr(md.HardcoverProvider, "_post", fake_post)
    out = await prov.search("way of kings", "Sanderson", limit=5)
    assert len(out) == 1
    m = out[0]
    assert m.title == "The Way of Kings" and m.author == "Brandon Sanderson"
    assert m.year == 2010 and m.cover_url == "https://hc/img.jpg"
    assert m.url == "https://hardcover.app/books/the-way-of-kings"


@pytest.mark.asyncio
async def test_provider_requires_token():
    from app.integrations import IntegrationError
    with pytest.raises(IntegrationError):
        await md.HardcoverProvider(api_key="").search("dune")


@pytest.mark.asyncio
async def test_provider_raises_on_graphql_errors(monkeypatch):
    from app.integrations import IntegrationError
    prov = md.HardcoverProvider(api_key="tok")

    async def fake_post(self, url, **kw):
        return FakeResp({"errors": [{"message": "rate limited"}]})

    monkeypatch.setattr(md.HardcoverProvider, "_post", fake_post)
    with pytest.raises(IntegrationError):
        await prov.search("dune")


def test_registered_as_metadata_provider():
    assert md.is_metadata_kind("hardcover")
    from app.models import Integration
    integ = Integration(kind="hardcover", name="HC", api_key="tok")
    assert isinstance(md.provider_for(integ), md.HardcoverProvider)


def test_catalog_doc_to_hit_carries_popularity_and_series():
    from app.ingestion.book_catalog import _hc_doc_to_hit
    hit = _hc_doc_to_hit(_DOC)
    assert hit is not None
    assert hit.source == "hardcover" and hit.title == "The Way of Kings"
    assert hit.popularity == 42000.0 and hit.series == "The Stormlight Archive"
    assert hit.isbn == ["9780765326355"]
    # junk titles rejected
    assert _hc_doc_to_hit({"id": 1, "title": "Summary of The Way of Kings"}) is None