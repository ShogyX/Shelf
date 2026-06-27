"""External reading-list import: each provider parses its canned response into ListItems."""
from __future__ import annotations

import json as _json
import re
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


def _anilist_page(entries, has_next=False):
    """One AniList Page response. ``entries`` = list of (status, fmt, title, author) tuples."""
    media = []
    for i, (status, fmt, title, author) in enumerate(entries, 1):
        media.append({"status": status, "media": {
            "id": i, "format": fmt, "title": {"english": title},
            "staff": {"nodes": [{"name": {"full": author}}] if author else []}}})
    return {"data": {"Page": {"pageInfo": {"hasNextPage": has_next}, "mediaList": media}}}


@pytest.mark.asyncio
async def test_anilist(monkeypatch):
    # The provider now filters server-side (status_in), so the mock must honor the requested status.
    def h(m, u, kw):
        status = (kw.get("json", {}).get("variables", {}) or {}).get("status")
        entries = [("PLANNING", "NOVEL", "Mushoku Tensei", "Rifujin na Magonote"),
                   ("CURRENT", "MANGA", "Berserk", "Kentaro Miura")]
        if status:
            entries = [e for e in entries if e[0] in status]
        return _Resp(payload=_anilist_page(entries))
    _patch(monkeypatch, h)
    items = await li.fetch_list("anilist", "someuser", list_name="PLANNING")
    assert len(items) == 1                                     # status filter kept only PLANNING
    assert items[0].title == "Mushoku Tensei" and items[0].author == "Rifujin na Magonote"
    assert items[0].media_kind == "text"                       # NOVEL → text
    allk = await li.fetch_list("anilist", "someuser")          # no filter → both
    assert {i.title for i in allk} == {"Mushoku Tensei", "Berserk"}
    assert next(i for i in allk if i.title == "Berserk").media_kind == "comic"


@pytest.mark.asyncio
async def test_anilist_paginates(monkeypatch):
    """AniList walks Page(page:N) until hasNextPage is false, accumulating across pages."""
    def h(m, u, kw):
        page = (kw.get("json", {}).get("variables", {}) or {}).get("page", 1)
        if page == 1:
            return _Resp(payload=_anilist_page([("PLANNING", "NOVEL", "A", "x")], has_next=True))
        if page == 2:
            return _Resp(payload=_anilist_page([("PLANNING", "NOVEL", "B", "y")], has_next=True))
        return _Resp(payload=_anilist_page([("PLANNING", "NOVEL", "C", "z")], has_next=False))
    _patch(monkeypatch, h)
    items = await li.fetch_list("anilist", "u")
    assert {i.title for i in items} == {"A", "B", "C"}         # all 3 pages walked, stops on hasNextPage=False


@pytest.mark.asyncio
async def test_anilist_page_cap(monkeypatch):
    """An always-hasNextPage server is bounded by MAX_PAGES (no infinite loop), and the truncation logs."""
    calls = {"n": 0}
    def h(m, u, kw):
        calls["n"] += 1
        page = (kw.get("json", {}).get("variables", {}) or {}).get("page", 1)
        return _Resp(payload=_anilist_page([("PLANNING", "NOVEL", f"T{page}", "a")], has_next=True))
    _patch(monkeypatch, h)
    monkeypatch.setattr(li, "MAX_PAGES", 4)
    items = await li.fetch_list("anilist", "u")
    assert calls["n"] == 4 and len(items) == 4                 # stopped at the page cap, didn't run forever


def _gr_rss(*books):
    """Goodreads list_rss feed. ``books`` = list of (title, book_id, author) tuples."""
    items = "".join(
        f"<item><title>{t}</title><book_id>{bid}</book_id><author_name>{a}</author_name>"
        f"<book_image_url>http://img/{bid}.jpg</book_image_url></item>"
        for t, bid, a in books)
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'


@pytest.mark.asyncio
async def test_goodreads(monkeypatch):
    # Single page with one item (< 100/page) → the loop stops after page 1.
    rss = _gr_rss(("Dune (Dune #1)", 234, "Frank Herbert"))
    _patch(monkeypatch, lambda m, u, kw: _Resp(text=rss) if "page=1" in u else _Resp(text=_gr_rss()))
    items = await li.fetch_list("goodreads", "12345-name", list_name="to-read")
    assert len(items) == 1
    assert items[0].title == "Dune"                            # series suffix stripped
    assert items[0].author == "Frank Herbert" and items[0].ext_id == "234"


@pytest.mark.asyncio
async def test_goodreads_paginates(monkeypatch):
    """Goodreads walks &page=N accumulating ~100/page until a short/empty page ends it."""
    full = [(f"B{i}", 1000 + i, "A") for i in range(100)]      # a full page (==100) → keep going
    def h(m, u, kw):
        pg = int(re.search(r"page=(\d+)", u).group(1))
        if pg == 1:
            return _Resp(text=_gr_rss(*full))
        if pg == 2:
            return _Resp(text=_gr_rss(("Last", 9999, "Z")))    # short page (1 < 100) → stop after this
        return _Resp(text=_gr_rss())                           # empty (shouldn't be reached)
    _patch(monkeypatch, h)
    items = await li.fetch_list("goodreads", "12345", list_name="read")
    assert len(items) == 101                                   # 100 from page 1 + 1 from page 2
    assert any(i.title == "Last" for i in items)


@pytest.mark.asyncio
async def test_goodreads_dup_guard_stops_loop(monkeypatch):
    """A server that ignores &page= re-serves page 1 forever — the dup-guard (same first item) stops it."""
    calls = {"n": 0}
    full = [(f"B{i}", 1000 + i, "A") for i in range(100)]      # always a full page → would loop forever
    def h(m, u, kw):
        calls["n"] += 1
        return _Resp(text=_gr_rss(*full))
    _patch(monkeypatch, h)
    items = await li.fetch_list("goodreads", "12345", list_name="read")
    assert calls["n"] == 2                                     # page 1 read, page 2 = same first item → stop
    assert len(items) == 100                                   # only the first page's items kept (deduped)


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


def _hc_payload(*titles):
    return {"data": {"users": [{"user_books": [
        {"book": {"title": t, "contributions": [{"author": {"name": "Andy Weir"}}]}} for t in titles]}]}}


@pytest.mark.asyncio
async def test_hardcover(monkeypatch):
    # One book (< the 100/page window) → the loop stops after the first page.
    _patch(monkeypatch, lambda m, u, kw: _Resp(payload=_hc_payload("Project Hail Mary")))
    items = await li.fetch_list("hardcover", "weiruser", list_name="want", config={"hc_token": "tok"})
    assert len(items) == 1 and items[0].title == "Project Hail Mary" and items[0].author == "Andy Weir"
    with pytest.raises(li.ListImportError):                    # no token → clear error
        await li.fetch_list("hardcover", "weiruser")


@pytest.mark.asyncio
async def test_hardcover_paginates(monkeypatch):
    """Hardcover walks offset/limit (limit 100) until a short/empty page; offset advances per page."""
    page0 = _hc_payload(*[f"B{i}" for i in range(100)])        # full 100 → fetch next offset
    def h(m, u, kw):
        offset = (kw.get("json", {}).get("variables", {}) or {}).get("offset", 0)
        if offset == 0:
            return _Resp(payload=page0)
        if offset == 100:
            return _Resp(payload=_hc_payload("Tail"))          # short page → stop
        return _Resp(payload=_hc_payload())                    # empty (shouldn't be reached)
    _patch(monkeypatch, h)
    items = await li.fetch_list("hardcover", "u", list_name="read", config={"hc_token": "tok"})
    assert len(items) == 101 and any(i.title == "Tail" for i in items)


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
async def test_amazon_wishlist_paginates(monkeypatch):
    """Amazon renders ~10 items then lazy-loads via showMoreUrl/paginationToken — follow the chain."""
    page1 = ('<html><body><span>wishlist</span>'
             '<a id="itemName_A">Book One (Paperback)</a>'
             '<div id="itemImage_A"><img data-a-hires="https://m/img/x._SS135_.jpg"></div>'
             '<script>{"showMoreUrl":"/hz/wishlist/slv/items?filter=unpurchased&paginationToken=TOK2"}</script>'
             '</body></html>')
    page2 = '<html><body><a id="itemName_B">Book Two</a></body></html>'   # no showMoreUrl → stop
    def h(m, u, kw):
        return _Resp(text=page2) if "paginationToken=TOK2" in u else _Resp(text=page1)
    _patch(monkeypatch, h)
    items = await li.fetch_list("amazon_wishlist", "https://www.amazon.com/hz/wishlist/ls/X?ref_=wl_share")
    assert {i.title for i in items} == {"Book One", "Book Two"}                  # both pages walked
    one = next(i for i in items if i.title == "Book One")
    assert one.cover_url == "https://m/img/x._SS300_.jpg"                        # cover extracted + upsized


@pytest.mark.asyncio
async def test_dedup_and_unknown_provider(monkeypatch):
    payload = _anilist_page([("PLANNING", "NOVEL", "Dup", None), ("PLANNING", "NOVEL", "dup", None)])
    _patch(monkeypatch, lambda m, u, kw: _Resp(payload=payload))
    items = await li.fetch_list("anilist", "u")
    assert len(items) == 1                                     # "Dup"/"dup" de-duplicated
    with pytest.raises(li.ListImportError):
        await li.fetch_list("nope", "x")


def _anilist_payload(*titles):
    return _anilist_page([("PLANNING", "NOVEL", t, None) for t in titles])


@pytest.mark.asyncio
async def test_sync_list_ingests_resolvable_titles(monkeypatch):
    """sync_list (scan + process) caches every list title as a per-item row, then acquires the ones that
    RESOLVE — tagging the acquire origin — while an unresolvable title stays un-done for a later retry.
    The whole list is ingested (no skip-the-backlog baseline); progress lives in per-item status."""
    from sqlalchemy import delete, select
    from app.db import SessionLocal, init_db
    from app.models import CatalogWork, ListSubscription, ListSubscriptionItem, User
    init_db(); db = SessionLocal()
    db.execute(delete(ListSubscriptionItem)); db.execute(delete(ListSubscription))
    db.execute(delete(CatalogWork)); db.execute(delete(User)); db.commit()
    u = User(username="lu", email="lu@x.com", password_hash="x", role="user"); db.add(u); db.commit()
    sub = ListSubscription(user_id=u.id, provider="anilist", list_ref="user", display_name="My AniList",
                           variant="ebook", known_keys=None)
    db.add(sub); db.commit(); db.refresh(sub)
    row = CatalogWork(provider="x", provider_ref="r", domain="d", work_url="w", title="Berserk",
                      norm_key="berserk", media_kind="comic")
    db.add(row); db.commit()
    acquired = []

    async def fake_resolve(_db, title, author, media_kind=None):
        return row if title == "Berserk" else None
    async def fake_acquire(_db, _row, **kw):
        acquired.append((kw.get("variant"), kw.get("context", {}).get("origin")))
        return {"status": "downloading"}
    monkeypatch.setattr("app.ingestion.series._resolve_book_row", fake_resolve)
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)
    monkeypatch.setattr("app.ingestion.acquire.user_priority", lambda _db, _u: ["pipeline"])

    # Only the resolvable title (Berserk) is acquired; the unresolvable one stays for a retry.
    _patch(monkeypatch, lambda m, u_, kw: _Resp(payload=_anilist_payload("Mushoku Tensei", "Berserk")))
    assert await li.sync_list(db, sub) == 1
    assert acquired == [("ebook", "list:anilist")]
    items = {i.norm_key: i for i in db.scalars(select(ListSubscriptionItem).where(
        ListSubscriptionItem.subscription_id == sub.id)).all()}
    assert items["berserk"].status == "done" and items["berserk"].catalog_id == row.id
    assert items["mushoku tensei"].status == "failed"   # didn't resolve → not done, retried later
    db.close()


@pytest.mark.asyncio
async def test_sync_list_variant_both_does_two_acquires(monkeypatch):
    """variant='both' fetches each new title as an ebook AND an audiobook (two acquire calls)."""
    from sqlalchemy import delete
    from app.db import SessionLocal, init_db
    from app.models import CatalogWork, ListSubscription, ListSubscriptionItem, User
    init_db(); db = SessionLocal()
    db.execute(delete(ListSubscriptionItem)); db.execute(delete(ListSubscription))
    db.execute(delete(CatalogWork)); db.execute(delete(User)); db.commit()
    u = User(username="lu2", email="lu2@x.com", password_hash="x", role="user"); db.add(u); db.commit()
    sub = ListSubscription(user_id=u.id, provider="anilist", list_ref="user", display_name="L",
                           variant="both")
    db.add(sub); db.commit()
    row = CatalogWork(provider="x", provider_ref="r", domain="d", work_url="w", title="Berserk",
                      norm_key="berserk", media_kind="comic")
    db.add(row); db.commit()
    variants = []
    async def fake_resolve(_db, t, a, media_kind=None): return row
    async def fake_acquire(_db, _row, **kw): variants.append(kw.get("variant")); return {"status": "grabbed"}
    monkeypatch.setattr("app.ingestion.series._resolve_book_row", fake_resolve)
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)
    monkeypatch.setattr("app.ingestion.acquire.user_priority", lambda _db, _u: ["pipeline"])
    _patch(monkeypatch, lambda m, u_, kw: _Resp(payload=_anilist_payload("Berserk")))
    assert await li.sync_list(db, sub) == 1
    assert sorted(variants) == ["audiobook", "ebook"]
    db.close()


@pytest.mark.asyncio
async def test_both_kicks_off_each_format_then_hands_to_ledger(monkeypatch):
    """For variant='both', the list fires acquire for BOTH formats (each opens its own per-format ledger
    row that the LEDGER then retries independently) and marks the item done — it doesn't itself chase the
    audiobook. Even when neither format is available right now, the item finishes (handed to the ledger)."""
    from sqlalchemy import delete, select
    from app.db import SessionLocal, init_db
    from app.models import CatalogWork, ListSubscription, ListSubscriptionItem, User
    init_db(); db = SessionLocal()
    db.execute(delete(ListSubscriptionItem)); db.execute(delete(ListSubscription))
    db.execute(delete(CatalogWork)); db.execute(delete(User)); db.commit()
    u = User(username="ab", email="ab@x.com", password_hash="x", role="user"); db.add(u); db.commit()
    sub = ListSubscription(user_id=u.id, provider="goodreads", list_ref="1", display_name="L",
                           variant="both", mode="download", known_keys=None)
    db.add(sub); db.commit(); db.refresh(sub)
    row = CatalogWork(provider="x", provider_ref="r", domain="d", work_url="w", title="Dune",
                      norm_key="dune", media_kind="text")
    db.add(row); db.commit()
    li.cache_list_items(db, sub, [li.ListItem(title="Dune")])
    db.commit()

    calls = []
    async def fake_resolve(_db, t, a, media_kind=None): return row
    async def fake_acquire(_db, _row, **kw): calls.append(kw.get("variant")); return {"status": "none"}
    monkeypatch.setattr("app.ingestion.series._resolve_book_row", fake_resolve)
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)
    monkeypatch.setattr("app.ingestion.acquire.user_priority", lambda _db, _u: ["pipeline"])

    # Neither format available right now, yet the item is DONE — each format was handed to its ledger.
    assert await li.process_pending(db, sub, limit=10) == 1
    assert sorted(calls) == ["audiobook", "ebook"]   # BOTH formats kicked off (independently tracked)
    it = db.scalars(select(ListSubscriptionItem).where(ListSubscriptionItem.subscription_id == sub.id)).one()
    assert it.status == "done"
    db.close()


@pytest.mark.asyncio
async def test_process_pending_catalog_mode_resolves_without_acquiring(monkeypatch):
    """In catalog mode, process_pending resolves each title's metadata + marks it done, but NEVER calls
    acquire (cataloguing makes a title browsable in Discovery without downloading it)."""
    from sqlalchemy import delete, select
    from app.db import SessionLocal, init_db
    from app.models import CatalogWork, ListSubscription, ListSubscriptionItem, User
    init_db(); db = SessionLocal()
    db.execute(delete(ListSubscriptionItem)); db.execute(delete(ListSubscription))
    db.execute(delete(CatalogWork)); db.execute(delete(User)); db.commit()
    u = User(username="cm", email="cm@x.com", password_hash="x", role="user"); db.add(u); db.commit()
    sub = ListSubscription(user_id=u.id, provider="goodreads", list_ref="1", display_name="L",
                           variant="ebook", mode="catalog", known_keys=None)
    db.add(sub); db.commit(); db.refresh(sub)
    row = CatalogWork(provider="x", provider_ref="r", domain="d", work_url="w", title="Dune",
                      norm_key="dune", media_kind="text")
    db.add(row); db.commit()
    li.cache_list_items(db, sub, [li.ListItem(title="Dune"), li.ListItem(title="Nope")])
    db.commit()
    acquired = []
    async def fake_resolve(_db, title, author, media_kind=None):
        return row if title == "Dune" else None
    async def boom_acquire(*a, **k):
        acquired.append(1); return {"status": "downloading"}
    monkeypatch.setattr("app.ingestion.series._resolve_book_row", fake_resolve)
    monkeypatch.setattr("app.ingestion.acquire.acquire", boom_acquire)
    monkeypatch.setattr("app.ingestion.acquire.user_priority", lambda _db, _u: ["pipeline"])

    done = await li.process_pending(db, sub, limit=10)
    assert done == 1 and acquired == []   # Dune catalogued; NOTHING acquired
    items = {i.norm_key: i for i in db.scalars(select(ListSubscriptionItem).where(
        ListSubscriptionItem.subscription_id == sub.id)).all()}
    assert items["dune"].status == "done" and items["dune"].catalog_id == row.id
    assert items["nope"].status == "failed"   # unresolved → retried later, never acquired
    db.close()


def test_list_import_api_flow(monkeypatch):
    """End-to-end API: providers → preview → create (curated) → list → patch → delete."""
    from sqlalchemy import delete, select
    from app.db import SessionLocal, init_db
    from app.models import ListSubscription, ListSubscriptionItem, User, UserSession

    async def fake_fetch(provider, list_ref, *, list_name=None, config=None, limit=None):
        return [li.ListItem(title="Dune", author="Frank Herbert"),
                li.ListItem(title="Hyperion", author="Dan Simmons")]
    async def fake_sync(db, sub, **kw):
        return 0   # avoid real acquire in the background initial-sync
    monkeypatch.setattr(li, "fetch_list", fake_fetch)
    monkeypatch.setattr(li, "sync_list", fake_sync)

    init_db(); db = SessionLocal()
    for m in (ListSubscriptionItem, ListSubscription, UserSession, User):
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

        # Unselected "Hyperion" is remembered as 'skipped' (never ingested); selected "Dune" is 'pending'.
        db = SessionLocal()
        statuses = {i.norm_key: i.status for i in db.scalars(select(ListSubscriptionItem).where(
            ListSubscriptionItem.subscription_id == sid)).all()}
        assert statuses == {"dune": "pending", "hyperion": "skipped"}; db.close()

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

    async def fake_resolve(_db, t, a, media_kind=None): return row
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
    for m in (ListSubscription, Subscription, CatalogWork, User):   # don't leak into later tests
        db.execute(delete(m))
    db.commit()
    db.close()


def test_list_import_resolve_and_items_and_register_kindle(monkeypatch):
    """resolve = catalog-first→upstream match per title; items = re-fetch covers; register stores kindle."""
    from sqlalchemy import delete, select
    from app.db import SessionLocal, init_db
    from app.models import (
        CatalogWork, ListSubscription, ListSubscriptionItem, User, UserSession, UserSettings)

    init_db(); db = SessionLocal()
    for m in (ListSubscriptionItem, ListSubscription, UserSettings, UserSession, CatalogWork, User):
        db.execute(delete(m))
    db.add(CatalogWork(provider="x", provider_ref="r", domain="d", work_url="w", title="Dune",
                       norm_key="dune", media_kind="text"))
    db.commit(); db.close()

    async def fake_resolve(_db, title, author, media_kind=None):
        return _db.scalar(select(CatalogWork).where(CatalogWork.norm_key == title.lower()))
    monkeypatch.setattr("app.ingestion.series._resolve_book_row", fake_resolve)

    async def fake_fetch(provider, list_ref, *, list_name=None, config=None, limit=None):
        return [li.ListItem(title="Dune", author="Frank Herbert", cover_url="http://c/dune.jpg")]
    async def fake_sync(db, sub, **kw): return 0
    monkeypatch.setattr(li, "fetch_list", fake_fetch)
    monkeypatch.setattr(li, "sync_list", fake_sync)

    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "adminpw1"})
        # resolve: matched + unmatched
        r = c.post("/api/list-imports/resolve", json={"items": [{"title": "Dune"}, {"title": "Nope"}]})
        assert r.status_code == 200
        d = r.json()
        assert d[0]["match_title"] == "Dune" and d[1]["match_catalog_id"] is None
        # create a sub, then re-fetch its items (covers) for the cover row
        sid = c.post("/api/list-imports", json={"provider": "goodreads", "list_ref": "1", "display_name": "G",
                     "variant": "ebook", "items": []}).json()["id"]
        items = c.get(f"/api/list-imports/{sid}/items").json()
        assert items["count"] == 1 and items["items"][0]["cover_url"] == "http://c/dune.jpg"

    # registration with a kindle email stores it on the new user's settings
    from app.config_store import update as cfg_update
    db = SessionLocal(); cfg_update(db, {"registration_mode": "open"}); db.close()
    with TestClient(app) as c:
        r = c.post("/api/auth/register", json={"username": "newbie", "email": "n@x.com",
                   "password": "abcdef12", "kindle_email": "newbie@kindle.com"})
        assert r.status_code == 200, r.text
    db = SessionLocal()
    uid = db.scalar(select(User.id).where(User.username == "newbie"))
    us = db.scalar(select(UserSettings).where(UserSettings.user_id == uid))
    assert us is not None and us.kindle_email == "newbie@kindle.com"
    db.close()


# --------------------------------------------------------------------- list-item cache + change-scan
def _reset(db, *models):
    from sqlalchemy import delete
    for m in models:
        db.execute(delete(m))
    db.commit()


def test_import_persists_items_and_get_items_serves_from_cache(monkeypatch):
    """Import caches the previewed items; GET /items serves them from the DB WITHOUT calling fetch_list."""
    from sqlalchemy import select
    from app.db import SessionLocal, init_db
    from app.models import ListSubscription, ListSubscriptionItem, User, UserSession

    fetch_calls = {"n": 0}

    async def fake_fetch(provider, list_ref, *, list_name=None, config=None, limit=None):
        fetch_calls["n"] += 1
        return [li.ListItem(title="Dune", author="Frank Herbert")]
    async def fake_sync(db, sub, **kw):
        return 0   # don't let the background initial-sync touch the cache in this test
    monkeypatch.setattr(li, "fetch_list", fake_fetch)
    monkeypatch.setattr(li, "sync_list", fake_sync)

    init_db(); db = SessionLocal()
    _reset(db, ListSubscriptionItem, ListSubscription, UserSession, User)
    db.close()

    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "adminpw1"})
        sid = c.post("/api/list-imports", json={
            "provider": "goodreads", "list_ref": "9", "display_name": "GR", "variant": "ebook",
            "items": [{"title": "Dune", "author": "Frank Herbert", "selected": True},
                      {"title": "Hyperion", "author": "Dan Simmons", "selected": False}]}).json()["id"]

        # The import persisted BOTH titles to the cache table.
        db = SessionLocal()
        rows = db.scalars(select(ListSubscriptionItem).where(
            ListSubscriptionItem.subscription_id == sid)).all()
        assert {r.title for r in rows} == {"Dune", "Hyperion"}
        db.close()

        before = fetch_calls["n"]
        out = c.get(f"/api/list-imports/{sid}/items").json()
        assert {i["title"] for i in out["items"]} == {"Dune", "Hyperion"} and out["count"] == 2
        assert fetch_calls["n"] == before                    # served from cache → NO live fetch


@pytest.mark.asyncio
async def test_sync_scan_diffs_cache_with_zero_resolution(monkeypatch):
    """The change-scan inserts an ADDED title + marks a REMOVED one in the cache, and makes ZERO
    cover/work-resolution calls (the scan only diffs; resolution stays lazy)."""
    from sqlalchemy import select
    from app.db import SessionLocal, init_db
    from app.models import CatalogWork, ListSubscription, ListSubscriptionItem, User

    init_db(); db = SessionLocal()
    _reset(db, ListSubscriptionItem, ListSubscription, CatalogWork, User)
    u = User(username="cu", email="cu@x.com", password_hash="x", role="user"); db.add(u); db.commit()
    # known_keys already covers every title the next fetch will contain, so NO title is "new to
    # acquire" — the scan only updates the cache (add/remove diff). That's the zero-resolution path.
    sub = ListSubscription(user_id=u.id, provider="anilist", list_ref="user", display_name="L",
                           variant="ebook", known_keys=["dune", "hyperion", "foundation"])
    db.add(sub); db.commit(); db.refresh(sub)
    # Cache starts with Dune + Hyperion (both currently on the list).
    li.cache_list_items(db, sub, [li.ListItem(title="Dune"), li.ListItem(title="Hyperion")])
    db.commit()

    resolved = {"n": 0}
    async def boom_resolve(*a, **k):
        resolved["n"] += 1
        return None
    monkeypatch.setattr("app.ingestion.series._resolve_book_row", boom_resolve)

    # Next fetch: Hyperion gone, "Foundation" added. scan_list only diffs the cache — it never resolves.
    _patch(monkeypatch, lambda m, u_, kw: _Resp(payload=_anilist_payload("Dune", "Foundation")))
    await li.scan_list(db, sub, force=True)

    rows = {r.norm_key: r for r in db.scalars(select(ListSubscriptionItem).where(
        ListSubscriptionItem.subscription_id == sub.id)).all()}
    assert rows["dune"].removed_at is None                    # still present
    assert rows["hyperion"].removed_at is not None            # marked removed
    assert "foundation" in rows and rows["foundation"].removed_at is None   # added
    assert resolved["n"] == 0                                 # scan resolved NOTHING

    # cached_items returns only the live (non-removed) titles.
    live = {it.title for it in li.cached_items(db, sub.id)}
    assert live == {"Dune", "Foundation"}
    _reset(db, ListSubscriptionItem, ListSubscription, CatalogWork, User)
    db.close()


def test_migration_creates_item_table_idempotently():
    """The 0043 table exists after init_db, and re-running the migration upgrade is a no-op (idempotent)."""
    import sqlalchemy as sa
    from app.db import SessionLocal, engine, init_db

    init_db()
    insp = sa.inspect(engine)
    assert "list_subscription_items" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("list_subscription_items")}
    assert {"subscription_id", "norm_key", "title", "author", "ref", "media_kind",
            "cover_url", "first_seen_at", "removed_at"} <= cols

    # Re-running upgrade() against the existing table must not raise (inspect-before guard).
    mig = _load_migration()
    db = SessionLocal()
    try:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        ctx = MigrationContext.configure(db.connection())
        with Operations.context(ctx):
            mig.upgrade()        # idempotent: table already present → early return, no error
    finally:
        db.close()
    assert "list_subscription_items" in sa.inspect(engine).get_table_names()


def _load_migration():
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(li.__file__), "..", "..", "alembic", "versions",
                        "0043_list_subscription_items.py")
    spec = importlib.util.spec_from_file_location("mig0043", os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
