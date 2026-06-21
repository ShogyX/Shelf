"""External reading-list import: each provider parses its canned response into ListItems."""
from __future__ import annotations

import json as _json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.ingestion import list_import as li
from app.main import app


class _Resp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._p = payload

    def json(self):
        return self._p if self._p is not None else _json.loads(self.text or "{}")


class _FakeClient:
    def __init__(self, handler):
        self._h = handler

    async def get(self, url, **kw):
        return self._h("GET", url, kw)

    async def post(self, url, **kw):
        return self._h("POST", url, kw)


def _patch(monkeypatch, handler):
    @asynccontextmanager
    async def _inst(category, **kw):
        yield _FakeClient(handler)
    monkeypatch.setattr("app.telemetry.instrument", _inst)


@pytest.mark.asyncio
async def test_anilist(monkeypatch):
    payload = {"data": {"MediaListCollection": {"lists": [{"entries": [
        {"status": "PLANNING", "media": {"id": 1, "format": "NOVEL", "title": {"english": "Mushoku Tensei"},
                                          "staff": {"nodes": [{"name": {"full": "Rifujin na Magonote"}}]}}},
        {"status": "CURRENT", "media": {"id": 2, "format": "MANGA", "title": {"romaji": "Berserk"},
                                        "staff": {"nodes": [{"name": {"full": "Kentaro Miura"}}]}}},
    ]}]}}}
    _patch(monkeypatch, lambda m, u, kw: _Resp(payload=payload))
    items = await li.fetch_list("anilist", "someuser", list_name="PLANNING")
    assert len(items) == 1                                     # status filter kept only PLANNING
    assert items[0].title == "Mushoku Tensei" and items[0].author == "Rifujin na Magonote"
    assert items[0].media_kind == "text"                       # NOVEL → text
    allk = await li.fetch_list("anilist", "someuser")          # no filter → both
    assert {i.title for i in allk} == {"Mushoku Tensei", "Berserk"}
    assert next(i for i in allk if i.title == "Berserk").media_kind == "comic"


@pytest.mark.asyncio
async def test_goodreads(monkeypatch):
    rss = ('<?xml version="1.0"?><rss version="2.0"><channel><item>'
           '<title>Dune (Dune #1)</title><book_id>234</book_id>'
           '<author_name>Frank Herbert</author_name>'
           '<book_image_url>http://img/dune.jpg</book_image_url></item></channel></rss>')
    _patch(monkeypatch, lambda m, u, kw: _Resp(text=rss))
    items = await li.fetch_list("goodreads", "12345-name", list_name="to-read")
    assert len(items) == 1
    assert items[0].title == "Dune"                            # series suffix stripped
    assert items[0].author == "Frank Herbert" and items[0].ext_id == "234"


@pytest.mark.asyncio
async def test_openlibrary(monkeypatch):
    def h(m, u, kw):
        if kw.get("params", {}).get("page", 1) == 1:
            return _Resp(payload={"reading_log_entries": [
                {"work": {"title": "The Left Hand of Darkness", "author_names": ["Ursula K. Le Guin"],
                          "key": "/works/OL1W"}}]})
        return _Resp(payload={"reading_log_entries": []})
    _patch(monkeypatch, h)
    items = await li.fetch_list("openlibrary", "mek", list_name="want-to-read")
    assert len(items) == 1 and items[0].title == "The Left Hand of Darkness"
    assert items[0].author == "Ursula K. Le Guin"


@pytest.mark.asyncio
async def test_hardcover(monkeypatch):
    payload = {"data": {"users": [{"user_books": [
        {"book": {"title": "Project Hail Mary", "contributions": [{"author": {"name": "Andy Weir"}}]}}]}]}}
    _patch(monkeypatch, lambda m, u, kw: _Resp(payload=payload))
    items = await li.fetch_list("hardcover", "weiruser", list_name="want", config={"hc_token": "tok"})
    assert len(items) == 1 and items[0].title == "Project Hail Mary" and items[0].author == "Andy Weir"
    with pytest.raises(li.ListImportError):                    # no token → clear error
        await li.fetch_list("hardcover", "weiruser")


@pytest.mark.asyncio
async def test_mal(monkeypatch):
    def h(m, u, kw):
        if kw.get("params", {}).get("page", 1) == 1:
            return _Resp(payload={"data": [{"entry": {"title": "Vinland Saga", "mal_id": 2}}],
                                  "pagination": {"has_next_page": False}})
        return _Resp(payload={"data": []})
    _patch(monkeypatch, h)
    items = await li.fetch_list("mal", "someuser", list_name="plan_to_read")
    assert len(items) == 1 and items[0].title == "Vinland Saga" and items[0].media_kind == "comic"


@pytest.mark.asyncio
async def test_amazon_wishlist(monkeypatch):
    html = ('<html><body><span>wishlist</span>'
            '<a id="itemName_I1" href="/dp/1">The Hobbit (Paperback)</a>'
            '<span id="item-byline_I1">by J.R.R. Tolkien (Author)</span></body></html>')
    _patch(monkeypatch, lambda m, u, kw: _Resp(text=html))
    items = await li.fetch_list("amazon_wishlist", "https://www.amazon.com/hz/wishlist/ls/ABC")
    assert len(items) == 1 and items[0].title == "The Hobbit" and items[0].author == "J.R.R. Tolkien"


@pytest.mark.asyncio
async def test_dedup_and_unknown_provider(monkeypatch):
    payload = {"data": {"MediaListCollection": {"lists": [{"entries": [
        {"status": "PLANNING", "media": {"id": 1, "format": "NOVEL", "title": {"english": "Dup"}, "staff": {"nodes": []}}},
        {"status": "PLANNING", "media": {"id": 2, "format": "NOVEL", "title": {"english": "dup"}, "staff": {"nodes": []}}},
    ]}]}}}
    _patch(monkeypatch, lambda m, u, kw: _Resp(payload=payload))
    items = await li.fetch_list("anilist", "u")
    assert len(items) == 1                                     # "Dup"/"dup" de-duplicated
    with pytest.raises(li.ListImportError):
        await li.fetch_list("nope", "x")


def _anilist_payload(*titles):
    return {"data": {"MediaListCollection": {"lists": [{"entries": [
        {"status": "PLANNING", "media": {"id": i, "format": "NOVEL", "title": {"english": t},
                                         "staff": {"nodes": []}}}
        for i, t in enumerate(titles, 1)]}]}}}


@pytest.mark.asyncio
async def test_sync_list_seeds_then_fetches_new(monkeypatch):
    """sync_list seeds the baseline on the first (unseeded) run WITHOUT fetching the backlog, then
    auto-acquires only titles that appear AFTER (same contract as follow_tick)."""
    from sqlalchemy import delete
    from app.db import SessionLocal, init_db
    from app.models import CatalogWork, ListSubscription, User
    init_db(); db = SessionLocal()
    db.execute(delete(ListSubscription)); db.execute(delete(CatalogWork)); db.execute(delete(User)); db.commit()
    u = User(username="lu", email="lu@x.com", password_hash="x", role="user"); db.add(u); db.commit()
    sub = ListSubscription(user_id=u.id, provider="anilist", list_ref="user", display_name="My AniList",
                           variant="ebook", known_keys=None)
    db.add(sub); db.commit(); db.refresh(sub)
    row = CatalogWork(provider="x", provider_ref="r", domain="d", work_url="w", title="Berserk",
                      norm_key="berserk", media_kind="comic")
    db.add(row); db.commit()
    acquired = []

    async def fake_resolve(_db, title, author):
        return row if title == "Berserk" else None
    async def fake_acquire(_db, _row, **kw):
        acquired.append((kw.get("variant"), kw.get("context", {}).get("origin")))
        return {"status": "downloading"}
    monkeypatch.setattr("app.ingestion.series._resolve_book_row", fake_resolve)
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)
    monkeypatch.setattr("app.ingestion.acquire.user_priority", lambda _db, _u: ["pipeline"])

    # First run (unseeded) → baseline only, NO fetch.
    _patch(monkeypatch, lambda m, u_, kw: _Resp(payload=_anilist_payload("Mushoku Tensei")))
    assert await li.sync_list(db, sub) == 0
    db.refresh(sub)
    assert sub.known_keys == ["mushoku tensei"] and acquired == []

    # A new title appears → fetched once, tagged origin "list:anilist".
    _patch(monkeypatch, lambda m, u_, kw: _Resp(payload=_anilist_payload("Mushoku Tensei", "Berserk")))
    assert await li.sync_list(db, sub) == 1
    db.refresh(sub)
    assert "berserk" in sub.known_keys and acquired == [("ebook", "list:anilist")]
    db.close()


@pytest.mark.asyncio
async def test_sync_list_variant_both_does_two_acquires(monkeypatch):
    """variant='both' fetches each new title as an ebook AND an audiobook (two acquire calls)."""
    from sqlalchemy import delete
    from app.db import SessionLocal, init_db
    from app.models import CatalogWork, ListSubscription, User
    init_db(); db = SessionLocal()
    db.execute(delete(ListSubscription)); db.execute(delete(CatalogWork)); db.execute(delete(User)); db.commit()
    u = User(username="lu2", email="lu2@x.com", password_hash="x", role="user"); db.add(u); db.commit()
    sub = ListSubscription(user_id=u.id, provider="anilist", list_ref="user", display_name="L",
                           variant="both", known_keys=["seed"])   # seeded → new title fetches
    db.add(sub); db.commit()
    row = CatalogWork(provider="x", provider_ref="r", domain="d", work_url="w", title="Berserk",
                      norm_key="berserk", media_kind="comic")
    db.add(row); db.commit()
    variants = []
    async def fake_resolve(_db, t, a): return row
    async def fake_acquire(_db, _row, **kw): variants.append(kw.get("variant")); return {"status": "grabbed"}
    monkeypatch.setattr("app.ingestion.series._resolve_book_row", fake_resolve)
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)
    monkeypatch.setattr("app.ingestion.acquire.user_priority", lambda _db, _u: ["pipeline"])
    _patch(monkeypatch, lambda m, u_, kw: _Resp(payload=_anilist_payload("Berserk")))
    assert await li.sync_list(db, sub) == 1
    assert sorted(variants) == ["audiobook", "ebook"]
    db.close()


def test_list_import_api_flow(monkeypatch):
    """End-to-end API: providers → preview → create (curated) → list → patch → delete."""
    from sqlalchemy import delete
    from app.db import SessionLocal, init_db
    from app.models import ListSubscription, User, UserSession

    async def fake_fetch(provider, list_ref, *, list_name=None, config=None):
        return [li.ListItem(title="Dune", author="Frank Herbert"),
                li.ListItem(title="Hyperion", author="Dan Simmons")]
    async def fake_sync(db, sub, **kw):
        return 0   # avoid real acquire in the background initial-sync
    monkeypatch.setattr(li, "fetch_list", fake_fetch)
    monkeypatch.setattr(li, "sync_list", fake_sync)

    init_db(); db = SessionLocal()
    for m in (ListSubscription, UserSession, User):
        db.execute(delete(m))
    db.commit(); db.close()

    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "adminpw1"})
        assert any(p["key"] == "anilist" for p in c.get("/api/list-imports/providers").json()["providers"])

        r = c.post("/api/list-imports/preview", json={"provider": "goodreads", "list_ref": "12345"})
        assert r.status_code == 200 and r.json()["count"] == 2

        r = c.post("/api/list-imports", json={
            "provider": "goodreads", "list_ref": "12345", "display_name": "My GR", "variant": "ebook",
            "items": [{"title": "Dune", "selected": True}, {"title": "Hyperion", "selected": False}]})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        assert r.json()["variant"] == "ebook" and r.json()["active"] is True

        # Unselected "Hyperion" is baselined (won't be fetched); "Dune" stays "new".
        db = SessionLocal(); sub = db.get(ListSubscription, sid)
        assert sub.known_keys == ["hyperion"]; db.close()

        assert len(c.get("/api/list-imports").json()) == 1
        # duplicate add → 409
        assert c.post("/api/list-imports", json={"provider": "goodreads", "list_ref": "12345",
                      "display_name": "x", "variant": "ebook", "items": []}).status_code == 409

        r = c.patch(f"/api/list-imports/{sid}", json={"variant": "both", "active": False})
        assert r.json()["variant"] == "both" and r.json()["active"] is False
        assert c.patch(f"/api/list-imports/{sid}", json={"variant": "nope"}).status_code == 400

        assert c.delete(f"/api/list-imports/{sid}").json()["deleted"] is True
        assert len(c.get("/api/list-imports").json()) == 0


@pytest.mark.asyncio
async def test_sync_list_auto_series_and_follow(monkeypatch):
    """auto_series expands a fetched title's series; auto_follow_series creates a seeded series follow."""
    from sqlalchemy import delete, select
    from app.db import SessionLocal, init_db
    from app.models import CatalogWork, ListSubscription, Subscription, User
    init_db(); db = SessionLocal()
    for m in (ListSubscription, Subscription, CatalogWork, User):
        db.execute(delete(m))
    db.commit()
    u = User(username="ls", email="ls@x.com", password_hash="x", role="user"); db.add(u); db.commit()
    sub = ListSubscription(user_id=u.id, provider="anilist", list_ref="user", display_name="L",
                           variant="ebook", known_keys=["seed"], auto_series=True, auto_follow_series=True)
    db.add(sub); db.commit()
    row = CatalogWork(provider="x", provider_ref="r", domain="d", work_url="w", title="Mistborn",
                      norm_key="mistborn", media_kind="text")
    db.add(row); db.commit()
    expanded = []

    async def fake_resolve(_db, t, a): return row
    async def fake_acquire(_db, _row, **kw): return {"status": "downloading"}
    async def fake_detect(_db, _row):
        return {"series": "Mistborn", "books": [{"title": "The Final Empire"}, {"title": "The Well of Ascension"}]}
    async def fake_acq_series(_db, _row, **kw): expanded.append(kw.get("want_all")); return []
    monkeypatch.setattr("app.ingestion.series._resolve_book_row", fake_resolve)
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)
    monkeypatch.setattr("app.ingestion.acquire.user_priority", lambda _db, _u: ["pipeline"])
    monkeypatch.setattr("app.ingestion.series.detect_series", fake_detect)
    monkeypatch.setattr("app.ingestion.series.acquire_series", fake_acq_series)
    _patch(monkeypatch, lambda m, u_, kw: _Resp(payload=_anilist_payload("Mistborn")))

    assert await li.sync_list(db, sub) == 1
    assert expanded == [True]                                  # series expanded (want_all)
    follow = db.scalar(select(Subscription).where(Subscription.user_id == u.id, Subscription.kind == "series"))
    assert follow is not None and follow.key == "mistborn"     # series follow created
    from app.ingestion.extract import norm_title
    assert set(follow.known_keys) == {norm_title("The Final Empire"), norm_title("The Well of Ascension")}
    # Idempotent: a second run with the follow already present doesn't duplicate it.
    monkeypatch.setattr("app.ingestion.series._resolve_book_row", fake_resolve)
    sub.known_keys = ["seed"]; db.commit()
    await li.sync_list(db, sub)
    assert db.scalar(select(__import__("sqlalchemy").func.count(Subscription.id))
                     .where(Subscription.user_id == u.id, Subscription.kind == "series")) == 1
    db.close()
