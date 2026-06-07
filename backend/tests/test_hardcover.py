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


def test_hc_popular_book_to_hit_and_genres():
    from app.ingestion.book_catalog import _hc_book_to_hit, _hc_genres
    b = {
        "id": 7, "slug": "1984", "title": "1984", "release_year": 1949, "users_count": 15688,
        "image": {"url": "https://hc/1984.jpg"}, "description": "Dystopia.",
        "contributions": [{"author": {"name": "George Orwell"}}, {"author": {"name": "Editor"}}],
        "cached_tags": {"Genre": [{"tag": "Dystopian"}, {"tag": "Fiction"}],
                        "Mood": [{"tag": "dark"}]},
    }
    h = _hc_book_to_hit(b)
    assert h.source == "hardcover" and h.title == "1984" and h.popularity == 15688.0
    assert h.cover_url == "https://hc/1984.jpg" and "George Orwell" in h.author
    assert h.year == 1949 and h.weak_signal is False
    assert h.subjects == ["Dystopian", "Fiction"]
    assert _hc_genres(b["cached_tags"]) == ["Dystopian", "Fiction"]
    assert _hc_genres(None) == []
    assert _hc_book_to_hit({"id": 1, "title": "Summary of 1984"}) is None   # junk filtered


@pytest.mark.asyncio
async def test_backfill_metadata_fills_cover_and_series(monkeypatch):
    from sqlalchemy import delete, select

    from app.db import SessionLocal, init_db
    from app.ingestion import book_catalog as bc
    from app.models import CatalogWork
    init_db(); db = SessionLocal()
    db.execute(delete(CatalogWork)); db.commit()
    r = CatalogWork(provider="openlibrary", provider_ref="/works/X", domain="openlibrary.org",
                    work_url="u", title="Warmage", author="Terry Mancour", media_kind="text",
                    norm_key=bc.norm_title("Warmage"), popularity=10.0, extra={})
    # a row with an ISBN but no Hardcover match → gets the ISBN cover fallback
    r2 = CatalogWork(provider="openlibrary", provider_ref="/works/Y", domain="openlibrary.org",
                     work_url="u2", title="Obscure Standalone", author="Nobody", media_kind="text",
                     norm_key=bc.norm_title("Obscure Standalone"), popularity=5.0,
                     extra={"isbn": ["9780765326355"]})
    db.add_all([r, r2]); db.commit(); db.refresh(r); db.refresh(r2)

    async def fake_hc(client, *, q, limit, token):
        if "Warmage" in q:
            return [bc.BookHit(source="hardcover", ref="1", title="Warmage", author="Terry Mancour",
                               cover_url="https://hc/warmage.jpg", series="The Spellmonger")]
        return []
    monkeypatch.setattr(bc, "_hc_query", fake_hc)
    monkeypatch.setattr(bc, "_hc_token", lambda db: "tok")

    out = await bc.backfill_metadata(db, max_lookups=10)
    db.refresh(r); db.refresh(r2)
    assert out["updated"] == 2
    assert r.cover_url == "https://hc/warmage.jpg" and (r.extra or {})["series"] == "The Spellmonger"
    assert r2.cover_url.endswith("/9780765326355-M.jpg")     # ISBN cover fallback
    assert (r.extra or {}).get("meta_checked") and (r2.extra or {}).get("meta_checked")
    # idempotent: completed/checked rows aren't re-selected
    assert (await bc.backfill_metadata(db, max_lookups=10))["checked"] == 0
    db.close()


def test_backfill_work_series_copies_from_catalog():
    from sqlalchemy import delete

    from app.db import SessionLocal, init_db
    from app.ingestion import book_catalog as bc
    from app.models import CatalogWork, Work
    init_db(); db = SessionLocal()
    db.execute(delete(Work)); db.execute(delete(CatalogWork)); db.commit()
    # A catalog row tagged with a series + a library Work of the same title missing series.
    db.add(CatalogWork(provider="hardcover", provider_ref="hc:2", domain="hardcover.app",
                       work_url="u", title="Warmage", author="Terry Mancour", media_kind="text",
                       norm_key=bc.norm_title("Warmage"),
                       extra={"series": "The Spellmonger", "series_position": 2.0}))
    w = Work(title="Warmage", author="Terry Mancour", media_kind="text")
    db.add(w); db.commit(); db.refresh(w)
    assert w.series is None
    n = bc._backfill_work_series(db)
    db.refresh(w)
    assert n == 1 and w.series == "The Spellmonger" and w.series_position == 2.0
    # idempotent-ish: a work with no matching series catalog row is left alone
    w2 = Work(title="A Standalone", author="X", media_kind="text")
    db.add(w2); db.commit()
    bc._backfill_work_series(db); db.refresh(w2)
    assert w2.series is None
    db.close()


def test_hc_series_name_only_for_multi_volume():
    from app.ingestion.book_catalog import _hc_series_name
    in_series = {"book_series": [{"position": 2, "series": {"name": "The Spellmonger", "books_count": 30}}]}
    standalone = {"book_series": [{"position": 1, "series": {"name": "X", "books_count": 1}}]}
    assert _hc_series_name(in_series) == "The Spellmonger"
    assert _hc_series_name(standalone) is None        # single-book "series" → not a series
    assert _hc_series_name({"book_series": []}) is None


def test_isbn_cover_fallback():
    from app.ingestion.book_catalog import _isbn_cover
    assert _isbn_cover(["9780765326355"]) == "https://covers.openlibrary.org/b/isbn/9780765326355-M.jpg"
    assert _isbn_cover(["junk", "0-7653-2635-5"]).endswith("/0765326355-M.jpg")
    assert _isbn_cover([]) is None and _isbn_cover(["nope"]) is None


def test_listing_only_and_series_in_group():
    from app.ingestion import catalog
    from app.models import CatalogWork as CW
    # A metadata (listing) source is flagged so the UI hides hook/grab.
    hc = CW(id=1, provider="hardcover", domain="hardcover.app", work_url="u", title="Dune",
            author="Frank Herbert", media_kind="text", norm_key="dune",
            extra={"series": "Dune"}, popularity=100.0)
    web = CW(id=2, provider="web_index", domain="novelsite.com", work_url="u2", title="Dune",
             author="Frank Herbert", media_kind="text", norm_key="dune")
    groups = catalog.group_rows([hc, web])
    g = groups[0]
    assert g["series"] == "Dune"                       # series surfaced → UI shows View Series
    by_provider = {s["provider"]: s for s in g["sources"]}
    assert by_provider["hardcover"]["listing_only"] is True
    assert by_provider["web_index"]["listing_only"] is False
    # A standalone (no series on any member) → no series affordance.
    solo = CW(id=3, provider="hardcover", domain="hardcover.app", work_url="u3", title="Solo Book",
              author="A", media_kind="text", norm_key="solo book")
    assert catalog.group_rows([solo])[0]["series"] is None


def test_catalog_doc_to_hit_carries_popularity_and_series():
    from app.ingestion.book_catalog import _hc_doc_to_hit
    hit = _hc_doc_to_hit(_DOC)
    assert hit is not None
    assert hit.source == "hardcover" and hit.title == "The Way of Kings"
    assert hit.popularity == 42000.0 and hit.series == "The Stormlight Archive"
    assert hit.isbn == ["9780765326355"]
    # junk titles rejected
    assert _hc_doc_to_hit({"id": 1, "title": "Summary of The Way of Kings"}) is None