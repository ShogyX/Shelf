"""Audiobookshelf + Storyteller client parsing + wiring (Phase 1 scaffolding)."""
from __future__ import annotations

import pytest

from app.db import SessionLocal, init_db
from app.integrations.audiobookshelf import AudiobookshelfClient
from app.integrations.base import client_for, is_companion_kind
from app.integrations.provider_catalog import catalog_entry
from app.integrations.storyteller import StorytellerClient
from app.models import Integration


def test_companion_kinds_registered():
    assert is_companion_kind("audiobookshelf") and is_companion_kind("storyteller")
    assert not is_companion_kind("readarr")
    assert catalog_entry("audiobookshelf")["category"] == "companion"
    assert catalog_entry("storyteller")["category"] == "companion"


def test_client_for_dispatch():
    abs_i = Integration(kind="audiobookshelf", base_url="http://h:13378", api_key="k", config={})
    st_i = Integration(kind="storyteller", base_url="http://h:8001", api_key="pw",
                       config={"username": "u"})
    assert isinstance(client_for(abs_i), AudiobookshelfClient)
    assert isinstance(client_for(st_i), StorytellerClient)


@pytest.mark.asyncio
async def test_abs_iter_items_missing_format_detection(monkeypatch):
    c = AudiobookshelfClient("http://h:13378", "k", kind="audiobookshelf", config={})
    page = {"results": [
        {"id": "li_1", "media": {"ebookFormat": "epub", "numAudioFiles": 0,
                                 "metadata": {"title": "Ebook Only", "isbn": "111"}}},
        {"id": "li_2", "media": {"ebookFormat": None, "numAudioFiles": 5,
                                 "metadata": {"title": "Audio Only", "authorName": "A"}}},
        {"id": "li_3", "media": {"ebookFormat": "epub", "numAudioFiles": 3,
                                 "metadata": {"title": "Both"}}},
    ]}

    async def fake_get(path, **kw):
        return page if kw.get("params", {}).get("page", 0) == 0 else {"results": []}
    monkeypatch.setattr(c, "_get", fake_get)

    items = await c.iter_items("lib")
    by = {i["title"]: i for i in items}
    assert by["Ebook Only"]["has_ebook"] and not by["Ebook Only"]["has_audio"]
    assert by["Audio Only"]["has_audio"] and not by["Audio Only"]["has_ebook"]
    assert by["Both"]["has_ebook"] and by["Both"]["has_audio"]
    assert by["Ebook Only"]["isbn"] == "111"


@pytest.mark.asyncio
async def test_abs_book_libraries(monkeypatch):
    c = AudiobookshelfClient("http://h:13378", "k", kind="audiobookshelf", config={})

    async def fake_get(path, **kw):
        return {"libraries": [
            {"id": "l1", "name": "Books", "mediaType": "book",
             "folders": [{"fullPath": "/mnt/stock"}]},
            {"id": "l2", "name": "Pods", "mediaType": "podcast", "folders": [{"fullPath": "/x"}]},
        ]}
    monkeypatch.setattr(c, "_get", fake_get)
    libs = await c.book_libraries()
    assert len(libs) == 1 and libs[0]["folders"] == ["/mnt/stock"]
    roots = await c.root_folders()
    assert [r.path for r in roots] == ["/mnt/stock"]


@pytest.mark.asyncio
async def test_storyteller_list_books_wanted_signal(monkeypatch):
    c = StorytellerClient("http://h:8001", "pw", kind="storyteller", config={"username": "u"})
    monkeypatch.setattr(c, "_auth", lambda: _async({"Authorization": "Bearer t"}))

    async def fake_get(path, **kw):
        return [
            {"uuid": "b1", "title": "Needs Audio", "authors": [{"name": "A"}],
             "ebook": {"uuid": "e1"}, "audiobook": None, "readaloud": None},
            {"uuid": "b2", "title": "Needs Ebook",
             "ebook": None, "audiobook": {"uuid": "a1"}, "readaloud": None},
            {"uuid": "b3", "title": "Done",
             "ebook": {"uuid": "e"}, "audiobook": {"uuid": "a"},
             "readaloud": {"status": "ALIGNED"}},
        ]
    monkeypatch.setattr(c, "_get", fake_get)
    books = {b["title"]: b for b in await c.list_books()}
    assert books["Needs Audio"]["has_ebook"] and not books["Needs Audio"]["has_audio"]
    assert books["Needs Ebook"]["has_audio"] and not books["Needs Ebook"]["has_ebook"]
    assert books["Done"]["readaloud_status"] == "ALIGNED"


async def _async(v):
    return v


def test_sync_button_on_companion_does_not_500(monkeypatch):
    # Regression: Sync on an ABS/Storyteller card must re-test connectivity, not 500 on the
    # NotImplementedError from the (absent) library-sync path.
    from fastapi.testclient import TestClient
    from sqlalchemy import delete

    from app.integrations import sync as isync
    from app.main import app

    from app.models import User, UserSession
    init_db()
    db = SessionLocal()
    db.execute(delete(Integration))
    db.execute(delete(UserSession))
    db.execute(delete(User))  # fresh DB so /auth/setup creates an admin + authenticates this client
    db.commit()
    db.close()

    async def fake_status(db, integ):
        integ.last_error = None
        return {"ok": True, "app": integ.kind}
    monkeypatch.setattr(isync, "pipeline_status", fake_status)

    c = TestClient(app)
    c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
    r = c.post("/api/integrations", json={
        "kind": "audiobookshelf", "base_url": "http://10.0.0.9:13378", "api_key": "k"})
    assert r.status_code == 200, r.text
    iid = r.json()["id"]
    s = c.post(f"/api/integrations/{iid}/sync")
    assert s.status_code == 200 and s.json().get("ok") is True
