"""Unit tests for the acquisition-pipeline clients (Prowlarr search + SABnzbd downloader).

These exercise parsing / filtering / error-handling without hitting the network by
monkeypatching the low-level HTTP helpers.
"""
from __future__ import annotations

import pytest

from app.integrations.base import IntegrationError, client_for, is_pipeline_kind
from app.integrations.prowlarr import ProwlarrClient, _to_release
from app.integrations.sabnzbd import SABnzbdClient


class _Integ:
    def __init__(self, kind):
        self.kind = kind
        self.base_url = "http://x"
        self.api_key = "k"


def test_is_pipeline_kind_and_factory():
    assert is_pipeline_kind("prowlarr") and is_pipeline_kind("sabnzbd")
    assert not is_pipeline_kind("readarr")
    assert isinstance(client_for(_Integ("prowlarr")), ProwlarrClient)
    assert isinstance(client_for(_Integ("sabnzbd")), SABnzbdClient)


def test_to_release_maps_fields():
    r = _to_release({
        "title": "Some Book EPUB",
        "downloadUrl": "http://idx/nzb/1",
        "protocol": "usenet",
        "indexer": "NzbPlanet",
        "indexerId": 9,
        "size": 10_500_000,
        "categories": [{"id": 7020, "name": "Books/EBook"}, {"id": 7000, "name": "Books"}],
        "guid": "g1",
        "age": 12.0,
    })
    assert r.protocol == "usenet" and r.indexer_id == 9
    assert r.categories == [7020, 7000]
    assert r.size_mb == 10.5
    assert r.download_url == "http://idx/nzb/1"


@pytest.mark.asyncio
async def test_prowlarr_search_filters_protocol(monkeypatch):
    pc = ProwlarrClient("http://x", "k")

    async def fake_get(path, headers=None, params=None):
        return [
            {"title": "ebook", "protocol": "usenet", "downloadUrl": "u1",
             "categories": [{"id": 7020, "name": "Books/EBook"}]},
            {"title": "torrent", "protocol": "torrent", "magnetUrl": "m1",
             "categories": [{"id": 7020, "name": "Books/EBook"}]},
        ]

    monkeypatch.setattr(pc, "_get", fake_get)
    rels = await pc.search("anything", categories=[7020])
    assert [r.title for r in rels] == ["ebook"]  # torrent filtered out by default


@pytest.mark.asyncio
async def test_sabnzbd_call_unwraps_errors(monkeypatch):
    sc = SABnzbdClient("http://x", "k")

    async def fake_status_false(path, params=None):
        return {"status": False, "error": "API Key Incorrect"}

    monkeypatch.setattr(sc, "_get", fake_status_false)
    with pytest.raises(IntegrationError):
        await sc._call("queue")


@pytest.mark.asyncio
async def test_sabnzbd_add_url(monkeypatch):
    sc = SABnzbdClient("http://x", "k")

    async def ok(path, params=None):
        assert params["mode"] == "addurl"
        assert params["cat"] == "shelf"
        return {"status": True, "nzo_ids": ["abc"]}

    monkeypatch.setattr(sc, "_get", ok)
    out = await sc.add_url("http://nzb", category="shelf", nzbname="x")
    assert out["nzo_ids"] == ["abc"]

    async def empty(path, params=None):
        return {"status": True, "nzo_ids": []}

    monkeypatch.setattr(sc, "_get", empty)
    with pytest.raises(IntegrationError):
        await sc.add_url("http://nzb", category="shelf")


@pytest.mark.asyncio
async def test_sabnzbd_root_folders(monkeypatch):
    sc = SABnzbdClient("http://x", "k")

    async def cfg(path, params=None):
        return {"config": {
            "misc": {"complete_dir": "/dl/complete"},
            "categories": [{"name": "*", "dir": ""}, {"name": "shelf", "dir": "/media/Books"}],
        }}

    monkeypatch.setattr(sc, "_get", cfg)
    paths = [r.path for r in await sc.root_folders()]
    assert paths == ["/dl/complete", "/media/Books"]


# --- End-to-end router (admin-gated) -------------------------------------------
def test_pipeline_integration_router_flow():
    """Add Prowlarr + SABnzbd via the admin API; they're flagged is_pipeline and their
    config round-trips. Added disabled so no network connectivity check runs."""
    from fastapi.testclient import TestClient
    from sqlalchemy import delete

    from app.db import SessionLocal, init_db
    from app.main import app
    from app.models import Integration, User

    init_db()
    db = SessionLocal()
    db.execute(delete(Integration))
    db.execute(delete(User))
    db.commit()
    db.close()

    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})

        r = c.post("/api/integrations", json={
            "kind": "prowlarr", "base_url": "http://10.0.0.9:9696", "api_key": "k",
            "enabled": False,
            "config": {"protocols": ["usenet"], "categories": [7000, 7020],
                       "preferred_formats": ["epub", "azw3"]},
        })
        assert r.status_code == 200, r.text
        pj = r.json()
        assert pj["is_pipeline"] is True and pj["is_metadata"] is False
        assert pj["has_api_key"] is True and pj["config"]["categories"] == [7000, 7020]

        r = c.post("/api/integrations", json={
            "kind": "sabnzbd", "base_url": "http://10.0.0.9:8080", "api_key": "k",
            "enabled": False,
            "config": {"category": "shelf",
                       "path_mappings": [{"remote": "/media/NAS-Pool", "local": "/mnt/NAS-Pool"}]},
        })
        assert r.status_code == 200, r.text
        sj = r.json()
        assert sj["is_pipeline"] is True
        assert sj["config"]["path_mappings"][0]["local"] == "/mnt/NAS-Pool"

        kinds = {i["kind"]: i for i in c.get("/api/integrations").json()}
        assert "prowlarr" in kinds and "sabnzbd" in kinds
        assert kinds["prowlarr"]["is_pipeline"] and kinds["sabnzbd"]["is_pipeline"]
