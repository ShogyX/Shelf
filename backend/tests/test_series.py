"""Tests for series detection + selective acquisition (Open Library mocked)."""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.ingestion import series
from app.ingestion.extract import norm_title
from app.models import CatalogWork


def _reset(db):
    db.execute(delete(CatalogWork)); db.commit()


def _cw(db, title, author="Brandon Sanderson", extra=None, hooked=None):
    cw = CatalogWork(provider="openlibrary", provider_ref=title, domain="openlibrary.org",
                     work_url="x", title=title, author=author, media_kind="text",
                     norm_key=norm_title(title), extra=extra, hooked_work_id=hooked)
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


def test_parse_series_label():
    assert series.parse_series_label("Mistborn (1)") == ("Mistborn", 1)
    assert series.parse_series_label("Discworld #8") == ("Discworld", 8)
    assert series.parse_series_label("The Wheel of Time, 4") == ("The Wheel of Time", 4)
    assert series.parse_series_label("Dune") == ("Dune", None)
    assert series.parse_series_label(None) == (None, None)


def test_series_from_title():
    assert series._series_from_title("Mistborn: The Final Empire") == "Mistborn"
    assert series._series_from_title("The Final Empire (Mistborn #1)") == "Mistborn"
    assert series._series_from_title("A Standalone Novel") is None


@pytest.mark.asyncio
async def test_detect_series_filters_members(monkeypatch):
    init_db(); db = SessionLocal(); _reset(db)
    cw = _cw(db, "Mistborn: The Final Empire", extra={"series": "Mistborn (1)"})
    # A sibling already in the library (annotated as such).
    _cw(db, "The Hero of Ages", hooked=42)

    # The OL series: filter returns members server-side (+ realistic bundle noise).
    filter_docs = [
        {"title": "The Final Empire", "author_name": ["Brandon Sanderson"],
         "first_publish_year": 2006, "key": "/works/A"},
        {"title": "The Hero of Ages", "author_name": ["Brandon Sanderson"],
         "first_publish_year": 2008, "key": "/works/B"},
        {"title": "Mistborn Trilogy Omnibus", "author_name": ["Brandon Sanderson"],
         "key": "/works/D"},                                           # bundle → dropped
    ]
    # The author query is membership-checked (series-match or title-contains).
    author_docs = [
        {"title": "Mistborn: Secret History", "author_name": ["Brandon Sanderson"],
         "first_publish_year": 2016, "key": "/works/C"},               # title-contains → member
        {"title": "Some Other Book", "author_name": ["Brandon Sanderson"],
         "key": "/works/E", "series": ["Other"]},                      # not this series → dropped
        {"title": "Mistborn", "author_name": ["Imposter Author"], "key": "/works/F"},  # wrong author
    ]

    async def fake_ol(client, q, *, limit):
        return filter_docs if 'series:"Mistborn"' in q else author_docs

    # GB enumeration: a disjoint-title volume that names the series only in its SUBTITLE, plus a
    # wrong-author decoy that must be dropped.
    async def fake_gb(client, name, author, key):
        return [
            {"title": "Shadows of Self", "subtitle": "A Mistborn Novel",
             "author_name": ["Brandon Sanderson"], "first_publish_year": 2015,
             "position": 5, "key": "gb:x"},
            {"title": "Unrelated", "subtitle": "A Mistborn Novel",
             "author_name": ["Someone Else"], "key": "gb:y"},   # wrong author → dropped
        ]

    monkeypatch.setattr(series, "_ol_query", fake_ol)
    monkeypatch.setattr(series, "_gb_series", fake_gb)
    out = await series.detect_series(db, cw)
    assert out["series"] == "Mistborn"
    titles = [b["title"] for b in out["books"]]
    assert "The Final Empire" in titles and "The Hero of Ages" in titles
    assert "Shadows of Self" in titles                # GB subtitle-matched disjoint title
    assert "Unrelated" not in titles                  # GB wrong author dropped
    assert "Mistborn Trilogy Omnibus" not in titles  # bundle
    assert "Some Other Book" not in titles            # wrong series
    assert "Mistborn" not in titles                   # wrong author
    hero = next(b for b in out["books"] if b["title"] == "The Hero of Ages")
    assert hero["hooked_work_id"] == 42               # annotated as in-library
    db.close()


@pytest.mark.asyncio
async def test_detect_series_uses_hardcover_membership(monkeypatch):
    """Hardcover's authoritative book_series enumerates a series — incl. disjoint-title volumes —
    even when Open Library doesn't index it. Author-gated."""
    init_db(); db = SessionLocal(); _reset(db)
    series._SERIES_CACHE.clear()
    cw = _cw(db, "Spellmonger", author="Terry Mancour", extra=None)

    async def no_ol(client, q, *, limit):
        return []
    async def no_gb(client, name, author, key):
        return []
    async def hc(client, token, name, author):
        return ("Spellmonger", "hc:1", [
            {"title": "Spellmonger", "author_name": ["Terry Mancour"],
             "first_publish_year": 2011, "position": 1, "key": "hc:1"},
            {"title": "Warmage", "author_name": ["Terry Mancour"],
             "first_publish_year": 2012, "position": 2, "key": "hc:2"},
            {"title": "The Spellmonger's Yule", "author_name": ["Terry Mancour"],
             "first_publish_year": 2017, "position": 9.5, "key": "hc:9.5"},  # fractional (novella)
            {"title": "High Mage", "author_name": ["Terry Mancour"],
             "first_publish_year": 2014, "position": 4, "key": "hc:4"},
            {"title": "Some Other Author Book", "author_name": ["Imposter"],
             "position": 9, "key": "hc:9"},                       # wrong author → dropped
            {"title": "Spellmonger Omnibus", "author_name": ["Terry Mancour"],
             "position": 99, "key": "hc:99"},                     # bundle → dropped
        ])
    monkeypatch.setattr(series, "_ol_query", no_ol)
    monkeypatch.setattr(series, "_gb_series", no_gb)
    monkeypatch.setattr(series, "_hc_series_lookup", hc)
    monkeypatch.setattr(series, "_hc_token", lambda db: "tok")

    out = await series.detect_series(db, cw)
    assert out["series"] == "Spellmonger"
    titles = [b["title"] for b in out["books"]]
    assert {"Spellmonger", "Warmage", "High Mage"} <= set(titles)   # disjoint titles enumerated
    assert "Some Other Author Book" not in titles                   # author-gated
    assert "Spellmonger Omnibus" not in titles                      # bundle filtered
    # positions preserved + ordered (incl. the fractional novella at 9.5)
    assert [b["title"] for b in out["books"]] == [
        "Spellmonger", "Warmage", "High Mage", "The Spellmonger's Yule"]
    yule = next(b for b in out["books"] if b["title"] == "The Spellmonger's Yule")
    assert yule["position"] == 9.5
    # The result serializes through the API schema (fractional positions must not 500).
    from app.schemas import SeriesOut
    SeriesOut(series=out["series"], books=out["books"])
    db.close()


@pytest.mark.asyncio
async def test_hc_series_lookup_partial_name_needs_author(monkeypatch):
    """A merely-overlapping (non-subset) Hardcover series name must NOT match on title alone — it
    needs author corroboration, or a shared word would wrongly match a different series."""
    def _payload(*docs):
        return {"search": {"results": {"hits": [{"document": d} for d in docs]}}}

    async def search_only(name_authors):
        async def fake(client, token, query, variables):
            if query is series._HC_SERIES_SEARCH:
                return _payload({"id": 1, "name": "Red Dragon Falls",
                                 "author_names": name_authors, "primary_books_count": 4})
            return {"series": [{"name": "Red Dragon Falls", "book_series": [
                {"position": 1, "book": {"id": 9, "title": "Red Dragon Falls I", "contributions": []}}]}]}
        return fake

    # "Red Dragon Rising" vs "Red Dragon Falls": Jaccard 0.5, neither a subset of the other.
    # No author on our side → rejected.
    monkeypatch.setattr(series, "_hc_graphql", await search_only(["Someone Else"]))
    name, _sid, docs = await series._hc_series_lookup(None, "tok", "Red Dragon Rising", None)
    assert name is None and docs == []

    # Same partial name WITH a corroborating author → accepted.
    monkeypatch.setattr(series, "_hc_graphql", await search_only(["Jane Doe"]))
    name, _sid, docs = await series._hc_series_lookup(None, "tok", "Red Dragon Rising", "Jane Doe")
    assert name == "Red Dragon Falls" and len(docs) == 1


@pytest.mark.asyncio
async def test_detect_series_none_when_no_series(monkeypatch):
    init_db(); db = SessionLocal(); _reset(db)
    cw = _cw(db, "A Standalone Novel", extra=None)

    async def empty(client, q, *, limit):
        return []
    async def empty_gb(client, name, author, key):
        return []
    monkeypatch.setattr(series, "_ol_query", empty)
    monkeypatch.setattr(series, "_gb_series", empty_gb)
    out = await series.detect_series(db, cw)
    assert out == {"series": None, "series_id": None, "books": []}
    db.close()


@pytest.mark.asyncio
async def test_detect_series_uses_persisted_members_after_restart(monkeypatch):
    """14B: a persisted enumeration on the row (extra['series_members']) is reused after a restart
    (in-memory cache cleared) WITHOUT re-running the cross-API lookup."""
    import time

    init_db(); db = SessionLocal(); _reset(db)
    series._SERIES_CACHE.clear()
    cw = _cw(db, "Mistborn", extra={"series_members": {
        "ts": time.time(),  # fresh
        "name": "Mistborn",
        "books": [
            {"title": "The Final Empire", "author": "Brandon Sanderson", "year": 2006,
             "position": 1, "cover_url": None, "ref": "/works/1", "norm_key": "the final empire"},
            {"title": "The Well of Ascension", "author": "Brandon Sanderson", "year": 2007,
             "position": 2, "cover_url": None, "ref": "/works/2", "norm_key": "the well of ascension"},
        ],
    }})

    # Any network path is a hard failure — a persisted hit must not reach the APIs.
    async def _boom(*a, **k):
        raise AssertionError("network lookup must be skipped on a persisted hit")
    monkeypatch.setattr(series, "_hc_series_lookup", _boom)
    monkeypatch.setattr(series, "_series_name_for", _boom)

    out = await series.detect_series(db, cw)
    assert out["series"] == "Mistborn"
    assert [b["title"] for b in out["books"]] == ["The Final Empire", "The Well of Ascension"]
    db.close()


@pytest.mark.asyncio
async def test_detect_series_persists_members_on_fresh_enumeration(monkeypatch):
    """14B: a fresh enumeration stamps extra['series_members'] on the row so a later restart can
    reuse it."""
    init_db(); db = SessionLocal(); _reset(db)
    series._SERIES_CACHE.clear()
    cw = _cw(db, "Solo Series")

    async def hc(client, token, title, author):
        return ("Solo Series", "hc:2", [
            {"title": "Solo Series Vol 1", "author_name": ["Brandon Sanderson"],
             "first_publish_year": 2020, "key": "/works/a"},
        ])
    async def no_ol(client, q, *, limit):
        return []
    async def no_gb(client, name, author, key):
        return []
    monkeypatch.setattr(series, "_hc_series_lookup", hc)
    monkeypatch.setattr(series, "_ol_query", no_ol)
    monkeypatch.setattr(series, "_gb_series", no_gb)
    monkeypatch.setattr(series, "_hc_token", lambda db: "tok")

    await series.detect_series(db, cw)
    db.refresh(cw)
    rec = (cw.extra or {}).get("series_members")
    assert rec and rec.get("name") == "Solo Series" and rec.get("ts")
    assert any(b["title"] == "Solo Series Vol 1" for b in rec["books"])
    db.close()


@pytest.mark.asyncio
async def test_detect_series_does_not_persist_partial_roster_on_transient(monkeypatch):
    """S-DUP-4: a supplement that fails TRANSIENTLY (5xx/timeout) may have dropped volumes from the
    roster. That partial set must NOT be cached durably — otherwise the missing volumes resurface as
    'new' for 14 days and get re-fetched. The best-effort roster is still returned for display."""
    init_db(); db = SessionLocal(); _reset(db)
    series._SERIES_CACHE.clear()
    cw = _cw(db, "Blip Series")

    async def hc(client, token, title, author):
        return ("Blip Series", "hc:3", [
            {"title": "Blip Series Vol 1", "author_name": ["Brandon Sanderson"],
             "first_publish_year": 2020, "key": "/works/a"},
        ])
    async def ok_ol(client, q, *, limit):
        return []
    async def transient_gb(client, name, author, key):
        series._mark_transient()   # provider blip inside this gather task
        return []
    monkeypatch.setattr(series, "_hc_series_lookup", hc)
    monkeypatch.setattr(series, "_ol_query", ok_ol)
    monkeypatch.setattr(series, "_gb_series", transient_gb)
    monkeypatch.setattr(series, "_hc_token", lambda db: "tok")

    out = await series.detect_series(db, cw)
    assert out["series"] == "Blip Series"                       # display still gets the roster
    db.refresh(cw)
    assert (cw.extra or {}).get("series_members") is None        # but not durably persisted
    assert series._SERIES_CACHE.get(norm_title("Blip Series")) is None  # nor in the process cache
    db.close()


def test_pick_by_author_strict_content_type_for_crawled():
    """A crawled (web_index) source only serves its declared media kinds: a same-title entry must not
    match a request whose content type the source doesn't serve, while non-crawled rows stay ungated."""
    from app.models import IndexSite
    init_db(); db = SessionLocal(); _reset(db)
    db.execute(delete(IndexSite)); db.commit()
    site = IndexSite(root_url="https://manga.example/", domain="manga.example",
                     status="done", allowed_media_kinds=["comic"])   # comics-only crawl source
    db.add(site); db.commit(); db.refresh(site)
    cw = CatalogWork(provider="web_index", domain="manga.example", work_url="u1",
                     title="Overlord", author="Kugane Maruyama", media_kind="comic",
                     norm_key=norm_title("Overlord"), site_id=site.id)
    db.add(cw); db.commit(); db.refresh(cw)
    nk = norm_title("Overlord")
    # a TEXT (novel) request must NOT match the comics-only crawl source...
    assert series._pick_by_author(db, nk, "Kugane Maruyama", want_kind="text") is None
    # ...but a COMIC request does.
    got = series._pick_by_author(db, nk, "Kugane Maruyama", want_kind="comic")
    assert got is not None and got.id == cw.id

    # Scope check: a NON-crawled row is not content-type-gated here (download routes type-rank it).
    _reset(db)
    ol = _cw(db, "Overlord", author="Kugane Maruyama")   # provider=openlibrary, media_kind=text
    assert series._pick_by_author(db, nk, "Kugane Maruyama", want_kind="comic").id == ol.id
    db.close()


def test_resolve_book_row_prefers_author():
    """A same-title, wrong-author edition (study guide) must not be picked over the real author."""
    init_db(); db = SessionLocal(); _reset(db)
    _cw(db, "The Final Empire", author="Some Study Guide Author")   # decoy, same norm_key
    right = _cw(db, "The Final Empire", author="Brandon Sanderson")
    picked = series._pick_by_author(db, norm_title("The Final Empire"), "Brandon Sanderson")
    assert picked is not None and picked.id == right.id
    # no author match → None (caller will live-resolve), never the wrong-author decoy
    assert series._pick_by_author(db, norm_title("The Final Empire"), "Totally Different") is None
    db.close()


@pytest.mark.asyncio
async def test_acquire_series_selection(monkeypatch):
    init_db(); db = SessionLocal(); _reset(db)
    from app.models import User
    u = User(username="s", password_hash="x", role="user"); db.add(u); db.commit(); db.refresh(u)
    cw = _cw(db, "Mistborn: The Final Empire", extra={"series": "Mistborn (1)"})

    detected = {"series": "Mistborn", "books": [
        {"title": "The Final Empire", "author": "Brandon Sanderson", "ref": "/works/A",
         "hooked_work_id": None, "catalog_id": None},
        {"title": "The Hero of Ages", "author": "Brandon Sanderson", "ref": "/works/B",
         "hooked_work_id": 7, "catalog_id": 99},   # already in library → skipped
    ]}

    async def fake_detect(db_, c):
        return detected
    grabbed = []

    async def fake_resolve(db_, title, author, media_kind=None):
        return _cw(db, title + " row", author=author)

    async def fake_acquire(db_, row, *, user_id, priority, shelf_id=None, context=None):
        grabbed.append(row.title)
        return {"route": "pipeline", "status": "downloading", "job_id": 1}

    monkeypatch.setattr(series, "detect_series", fake_detect)
    monkeypatch.setattr(series, "_resolve_book_row", fake_resolve)
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)

    res = await series.acquire_series(db, cw, refs=["/works/A", "/works/B"], want_all=False, user_id=u.id)
    statuses = {r["ref"]: r["status"] for r in res}
    assert statuses["/works/A"] == "downloading"
    assert statuses["/works/B"] == "in_library"   # skipped, not grabbed
    assert grabbed == ["The Final Empire row"]
    db.close()
