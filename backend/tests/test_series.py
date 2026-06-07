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
    cw = _cw(db, "Spellmonger", author="Terry Mancour", extra=None)

    async def no_ol(client, q, *, limit):
        return []
    async def no_gb(client, name, author, key):
        return []
    async def hc(client, token, name, author):
        return ("Spellmonger", [
            {"title": "Spellmonger", "author_name": ["Terry Mancour"],
             "first_publish_year": 2011, "position": 1, "key": "hc:1"},
            {"title": "Warmage", "author_name": ["Terry Mancour"],
             "first_publish_year": 2012, "position": 2, "key": "hc:2"},
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
    # positions preserved + ordered
    assert [b["title"] for b in out["books"]] == ["Spellmonger", "Warmage", "High Mage"]
    db.close()


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
    assert out == {"series": None, "books": []}
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

    async def fake_resolve(db_, title, author):
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
