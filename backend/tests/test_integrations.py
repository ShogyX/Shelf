"""Readarr/Kapowarr clients (mocked), strong matching, catalog merge, and sync."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import catalog
from app.ingestion.extract import titles_match
from app.integrations.base import ExternalWork
from app.integrations.kapowarr import KapowarrClient
from app.integrations.readarr import ReadarrClient
from app.models import CatalogWork, IndexSite, Integration, WatchedFolder


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for model in (CatalogWork, Integration, WatchedFolder, IndexSite):
        db.execute(delete(model))
    db.commit()
    db.close()
    yield


class _Canned:
    """Stand-in for BaseClient._get: returns canned JSON keyed by path suffix."""

    def __init__(self, responses: dict):
        self.responses = responses

    async def __call__(self, path, *, headers=None, params=None):
        for suffix, val in self.responses.items():
            if path.endswith(suffix):
                return val
        raise AssertionError(f"no canned response for {path}")


READARR_BOOK = {
    "id": 5, "title": "Mistborn", "authorTitle": "brandon sanderson",
    "author": {"authorName": "Brandon Sanderson"},
    "overview": "<p>The Final Empire</p>", "releaseDate": "2006-07-17T00:00:00Z",
    "titleSlug": "mistborn-5",
    "images": [{"coverType": "cover", "url": "/MediaCover/5/cover.jpg",
                "remoteUrl": "https://img.example/cover.jpg"}],
    "statistics": {"bookFileCount": 1},
    "foreignBookId": "768890",
}

KAPOWARR_VOL = {
    "id": 3, "comicvine_id": 12345, "title": "Saga", "year": "2012",
    "publisher": "Image", "description": "<p>Epic space opera</p>",
    "cover_link": "https://cv.example/img.jpg", "issues_downloaded": 5,
}


@pytest.mark.asyncio
async def test_readarr_maps_book_to_external_work():
    client = ReadarrClient("http://readarr:8787", "key")
    client._get = _Canned({"/api/v1/book": [READARR_BOOK]})
    [ext] = await client.list_library()
    assert ext.provider == "readarr" and ext.ref == "768890"
    assert ext.title == "Mistborn" and ext.author == "Brandon Sanderson"
    assert ext.overview == "The Final Empire"            # HTML stripped
    assert ext.cover_url == "https://img.example/cover.jpg"  # absolute remoteUrl preferred
    assert ext.year == 2006 and ext.downloaded is True and ext.media_kind == "text"


@pytest.mark.asyncio
async def test_kapowarr_unwraps_envelope_and_maps_volume():
    client = KapowarrClient("http://kapowarr:5656", "key")
    client._get = _Canned({"/api/volumes": {"error": None, "result": [KAPOWARR_VOL]}})
    [ext] = await client.list_library()
    assert ext.provider == "kapowarr" and ext.ref == "12345"
    assert ext.title == "Saga" and ext.author == "Image" and ext.year == 2012
    assert ext.overview == "Epic space opera" and ext.media_kind == "comic"
    assert ext.cover_url == "https://cv.example/img.jpg" and ext.downloaded is True


def test_titles_match_strong_and_author_aware():
    # Same title, compatible/unknown authors → match.
    assert titles_match("library of heavens path", None, "library of heavens path", None)
    # High token containment → match.
    assert titles_match("library of heavens path", None,
                        "library of heavens path complete", None)
    # Same normalized title but disjoint known authors → NOT a match.
    assert not titles_match("dawn", "Octavia Butler", "dawn", "Someone Else")
    # Merely sharing a word → NOT a match.
    assert not titles_match("martial peak", None, "martial god asura", None)


def _integration(db, kind="readarr") -> Integration:
    integ = Integration(kind=kind, name=f"My {kind}", base_url="http://x", api_key="k",
                        enabled=True, root_folder="/data/books")
    db.add(integ)
    db.commit()
    db.refresh(integ)
    return integ


def test_upsert_external_copies_metadata_and_dedupes():
    db = SessionLocal()
    integ = _integration(db)
    ext = ExternalWork(provider="readarr", ref="768890", title="Mistborn",
                       author="Brandon Sanderson", overview="The Final Empire",
                       cover_url="https://img/c.jpg", media_kind="text", in_library=True)
    a = catalog.upsert_external(db, integ, ext)
    b = catalog.upsert_external(db, integ, ext)  # same ref → dedupe
    assert a.id == b.id
    assert a.provider == "readarr" and a.author == "Brandon Sanderson"
    assert a.synopsis == "The Final Empire" and a.cover_url == "https://img/c.jpg"
    assert a.extra["in_library"] is True and a.extra["root_folder"] == "/data/books"
    assert db.scalar(select(CatalogWork)) is a
    db.close()


def test_group_rows_merges_web_and_integration_sources():
    db = SessionLocal()
    site = IndexSite(root_url="https://s/", domain="s.com", status="done",
                     max_pages=10, max_depth=2)
    db.add(site)
    db.commit()
    db.refresh(site)
    # A web-crawl catalog entry…
    web = CatalogWork(provider="web_index", site_id=site.id, domain="s.com",
                      work_url="https://s/novel/mistborn", title="Mistborn",
                      norm_key="mistborn")
    db.add(web)
    # …and the same title from Readarr.
    integ = _integration(db)
    catalog.upsert_external(db, integ, ExternalWork(
        provider="readarr", ref="768890", title="Mistborn", author="Brandon Sanderson"))
    db.commit()

    groups = catalog.group_rows(catalog.find_rows(db))
    assert len(groups) == 1, [g["title"] for g in groups]
    kinds = {s["kind"] for s in groups[0]["sources"]}
    assert kinds == {"online", "readarr"}
    db.close()


class _CannedReq:
    """Stand-in for BaseClient._request: canned JSON keyed by (method, path suffix)."""

    def __init__(self, responses: dict):
        self.responses = responses

    async def __call__(self, method, path, *, headers=None, params=None, json=None):
        for (m, suffix), val in self.responses.items():
            if m == method and path.endswith(suffix):
                return val
        raise AssertionError(f"no canned {method} {path}")


@pytest.mark.asyncio
async def test_readarr_grab_adds_book_and_searches():
    client = ReadarrClient("http://readarr:8787", "key")
    client._request = _CannedReq({
        ("GET", "/api/v1/book/lookup"): [
            {"foreignBookId": "768890", "title": "Mistborn",
             "author": {"authorName": "Brandon Sanderson"}}
        ],
        ("GET", "/api/v1/qualityprofile"): [{"id": 1}],
        ("GET", "/api/v1/metadataprofile"): [{"id": 2}],
        ("POST", "/api/v1/book"): {"id": 99},
    })
    res = await client.grab({"foreignBookId": "768890"}, root_folder="/books")
    assert res["id"] == 99 and res["status"] == "added" and res["searching"] is True


@pytest.mark.asyncio
async def test_kapowarr_grab_adds_volume():
    client = KapowarrClient("http://kapowarr:5656", "key")
    client._request = _CannedReq({
        ("GET", "/api/rootfolder"): {"error": None, "result": [{"id": 2, "folder": "/comics"}]},
        ("POST", "/api/volumes"): {"error": None, "result": {"id": 55}},
        ("POST", "/api/volumes/55/search"): {"error": None, "result": {}},
    })
    res = await client.grab({"comicvine_id": 12345}, root_folder="/comics")
    assert res["id"] == 55 and res["status"] == "added"


@pytest.mark.asyncio
async def test_grab_external_records_status(monkeypatch):
    db = SessionLocal()
    integ = _integration(db, kind="readarr")
    entry = catalog.upsert_external(db, integ, ExternalWork(
        provider="readarr", ref="768890", title="Mistborn", author="Brandon Sanderson"))

    class FakeClient:
        async def grab(self, extra, **kw):
            return {"id": 99, "status": "added"}

    monkeypatch.setattr("app.integrations.sync.client_for", lambda i: FakeClient())
    from app.integrations import sync as isync
    info = await isync.grab_external(db, entry)
    assert info["integration"] == integ.name
    db.refresh(entry)
    assert entry.extra["grab_status"] == "requested"
    db.close()


@pytest.mark.asyncio
async def test_sync_integration_pulls_library_into_catalog(monkeypatch):
    db = SessionLocal()
    integ = _integration(db, kind="kapowarr")

    class FakeClient:
        async def list_library(self):
            return [ExternalWork(provider="kapowarr", ref="1", title="Saga",
                                 author="Image", media_kind="comic")]
        async def root_folders(self):
            return []  # nothing mappable in the sandbox

    monkeypatch.setattr("app.integrations.sync.client_for", lambda i: FakeClient())
    from app.integrations import sync as isync
    summary = await isync.sync_integration(db, integ)
    assert summary["library"] == 1
    row = db.scalar(select(CatalogWork).where(CatalogWork.provider == "kapowarr"))
    assert row is not None and row.title == "Saga" and row.media_kind == "comic"
    db.refresh(integ)
    assert integ.last_sync_at is not None
    db.close()
