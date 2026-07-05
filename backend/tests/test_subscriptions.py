"""Wave E — follow author/series + request-all-by-author + follow_tick (providers mocked)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import series
from app.ingestion.extract import _author_norm, norm_title
from app.main import app
from app.models import CatalogWork, LibraryItem, Subscription, User, UserSession, Work


def _reset():
    db = SessionLocal()
    for m in (Subscription, LibraryItem, CatalogWork, UserSession, Work, User):
        db.execute(delete(m))
    db.commit()
    db.close()


def _cw(db, title, author="Brandon Sanderson", extra=None, hooked=None):
    cw = CatalogWork(provider="openlibrary", provider_ref=title, domain="openlibrary.org",
                     work_url="x:" + title, title=title, author=author, media_kind="text",
                     norm_key=norm_title(title), extra=extra, hooked_work_id=hooked)
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


@pytest.fixture
def client():
    init_db()
    _reset()
    c = TestClient(app)
    c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
    c.post("/api/users", json={"username": "joe", "password": "test1234", "role": "user"})
    c.post("/api/users", json={"username": "bob", "password": "test1234", "role": "user"})
    return c


def _login(username):
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": username, "password": "test1234"})
    return c


@pytest.fixture(autouse=True)
def _capture_backlog(monkeypatch):
    """Neutralize + record the follow-author background back-catalog grab, so no test accidentally
    fires a real provider fetch. Tests that care inspect the returned (user_id, author_name) list."""
    from app.routers import subscriptions as subs
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(subs, "_schedule_author_backlog", lambda uid, name: calls.append((uid, name)))
    return calls


def _uid(username):
    db = SessionLocal()
    uid = db.scalar(select(User.id).where(User.username == username))
    db.close()
    return uid


def test_follow_author_by_name_without_catalog_row(client, monkeypatch):
    # The library detail modal follows an author by NAME (no catalog_id) — Wave 5.
    async def fake_enum(db, name):
        return [{"title": "Some Book", "author": name, "ref": "r1", "year": 2001,
                 "position": None, "cover_url": None, "catalog_id": None, "hooked_work_id": None}]
    monkeypatch.setattr(series, "enumerate_author", fake_enum)
    joe = _login("joe")
    r = joe.post("/api/subscriptions", json={"kind": "author", "author_name": "Ursula K. Le Guin"})
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "Ursula K. Le Guin"
    # No name and no catalog row → 400.
    assert joe.post("/api/subscriptions", json={"kind": "author"}).status_code == 400


def test_follow_author_schedules_backlog_grab(client, monkeypatch, _capture_backlog):
    # Follow-author = subscribe (track future) AND grab the existing back-catalog now (#6).
    async def fake_enum(db, name):
        return [{"title": "Old", "author": name, "ref": "r1", "year": 2000,
                 "position": None, "cover_url": None, "catalog_id": None, "hooked_work_id": None}]
    monkeypatch.setattr(series, "enumerate_author", fake_enum)
    joe = _login("joe")
    r = joe.post("/api/subscriptions", json={"kind": "author", "author_name": "Iain M. Banks"})
    assert r.status_code == 200, r.text
    assert _capture_backlog == [(_uid("joe"), "Iain M. Banks")]  # backlog grab scheduled once


def test_follow_series_does_not_grab_backlog(client, monkeypatch, _capture_backlog):
    # Series follow tracks FUTURE volumes only — no automatic backlog grab (SeriesModal "Grab all"
    # covers that explicitly).
    async def fake_detect(db, cw):
        return {"series": "The Culture", "books": []}
    monkeypatch.setattr(series, "detect_series", fake_detect)
    db = SessionLocal()
    cw = _cw(db, "Consider Phlebas", author="Iain M. Banks", extra={"series": "The Culture"})
    cid = cw.id; db.close()
    joe = _login("joe")
    r = joe.post("/api/subscriptions",
                 json={"kind": "series", "catalog_id": cid, "series_name": "The Culture"})
    assert r.status_code == 200, r.text
    assert _capture_backlog == []


def test_backlog_grab_invokes_acquire_author(client, monkeypatch):
    # The background helper asks acquire_author for the WHOLE roster, tagged origin="following".
    import asyncio as _aio
    from app.routers import subscriptions as subs
    rec: dict = {}

    async def fake_acquire(db, name, *, refs, want_all, user_id, shelf_id=None,
                           origin=None, origin_detail=None):
        rec.update(name=name, want_all=want_all, user_id=user_id, origin=origin)
        return []
    monkeypatch.setattr(series, "acquire_author", fake_acquire)
    joe_id = _uid("joe")
    _aio.run(subs._grab_author_backlog_bg(joe_id, "Le Guin"))
    assert rec == {"name": "Le Guin", "want_all": True, "user_id": joe_id, "origin": "following"}


# ----------------------------------------------------------------------------- CRUD + 403
def test_subscribe_list_unfollow_toggle_and_cross_user_403(client, monkeypatch):
    # Seeding hits a provider — stub it to a fixed roster (so the baseline is seeded, not networked).
    async def fake_enum(db, name):
        return [{"title": "Old Book", "author": name, "ref": "r1", "year": 2000,
                 "position": None, "cover_url": None, "catalog_id": None, "hooked_work_id": None}]
    monkeypatch.setattr(series, "enumerate_author", fake_enum)

    db = SessionLocal()
    cw = _cw(db, "Some Title", author="Jane Doe"); cid = cw.id; db.close()

    joe = _login("joe")
    # subscribe (author)
    r = joe.post("/api/subscriptions", json={"kind": "author", "catalog_id": cid})
    assert r.status_code == 200, r.text
    sub = r.json()
    assert sub["kind"] == "author" and sub["display_name"] == "Jane Doe"
    assert sub["auto_request"] is True            # default True (R15)
    sub_id = sub["id"]
    # the baseline was seeded so day-1 backlog isn't auto-fired
    db = SessionLocal()
    s = db.get(Subscription, sub_id)
    assert s.known_keys == [norm_title("Old Book")]
    assert s.key == _author_norm("Jane Doe")
    db.close()

    # idempotent re-subscribe → same row
    r2 = joe.post("/api/subscriptions", json={"kind": "author", "catalog_id": cid})
    assert r2.status_code == 200 and r2.json()["id"] == sub_id

    # list (own only)
    lst = joe.get("/api/subscriptions").json()
    assert [x["id"] for x in lst] == [sub_id]

    # toggle auto off
    r3 = joe.patch(f"/api/subscriptions/{sub_id}", json={"auto_request": False})
    assert r3.status_code == 200 and r3.json()["auto_request"] is False

    # cross-user: bob can't see, patch, or delete joe's sub
    bob = _login("bob")
    assert bob.get("/api/subscriptions").json() == []
    assert bob.patch(f"/api/subscriptions/{sub_id}", json={"active": False}).status_code == 403
    assert bob.delete(f"/api/subscriptions/{sub_id}").status_code == 403

    # owner unfollow
    assert joe.delete(f"/api/subscriptions/{sub_id}").status_code == 200
    assert joe.get("/api/subscriptions").json() == []


# --------------------------------------------------------------- enumerate_author (provider stub)
@pytest.mark.asyncio
async def test_enumerate_author_dedups_bundles_and_author_gates(monkeypatch):
    init_db(); _reset()
    db = SessionLocal()
    _cw(db, "Owned One", author="Terry Mancour", hooked=55)   # owned → annotated hooked

    async def fake_gb(client, q, key):
        return [
            {"title": "Spellmonger", "author_name": ["Terry Mancour"], "first_publish_year": 2011,
             "position": None, "key": "gb:1"},
            {"title": "Spellmonger Omnibus", "author_name": ["Terry Mancour"], "key": "gb:o"},  # bundle
            {"title": "Imposter Tale", "author_name": ["Someone Else"], "key": "gb:i"},         # wrong author
        ]

    async def fake_ol(client, q, *, limit):
        return [
            {"title": "Warmage", "author_name": ["Terry Mancour"], "first_publish_year": 2012,
             "key": "/works/w"},
            {"title": "Spellmonger", "author_name": ["Terry Mancour"], "key": "/works/dup"},  # dup norm_key
            {"title": "Owned One", "author_name": ["Terry Mancour"], "key": "/works/own"},
        ]

    monkeypatch.setattr(series, "_gb_author_volumes", fake_gb)
    monkeypatch.setattr(series, "_ol_query", fake_ol)
    monkeypatch.setattr("app.ingestion.book_catalog._gb_key", lambda db: "")

    books = await series.enumerate_author(db, "Terry Mancour")
    titles = [b["title"] for b in books]
    assert "Spellmonger" in titles and "Warmage" in titles
    assert titles.count("Spellmonger") == 1                  # deduped
    assert "Spellmonger Omnibus" not in titles               # bundle dropped
    assert "Imposter Tale" not in titles                     # author-gated
    owned = next(b for b in books if b["title"] == "Owned One")
    assert owned["hooked_work_id"] == 55                      # annotated owned
    db.close()


# ----------------------------------------------------------- request-all-by-author (count/cap/skip)
@pytest.mark.asyncio
async def test_acquire_author_full_count_cap_and_adds_in_stock(monkeypatch):
    init_db(); _reset()
    db = SessionLocal()
    u = User(username="x", password_hash="x", role="user"); db.add(u); db.commit(); db.refresh(u)

    # A FULL roster of 35 + one in-stock (on disk, not in the user's library): enumerate returns the
    # full count; acquire caps at 30. The in-stock one is ADDED to the user's library (not skipped).
    cwStock = _cw(db, "Book 0 stock row", author="Prolific")
    cwStock.hooked_work_id = 7; db.commit()
    roster = [{"title": f"Book {i}", "author": "Prolific", "ref": f"r{i}", "year": 2000 + i,
               "position": None, "cover_url": None,
               "catalog_id": (cwStock.id if i == 0 else None),
               "hooked_work_id": (7 if i == 0 else None)} for i in range(35)]

    async def fake_enum(db_, name):
        return list(roster)
    grabbed = []

    async def fake_resolve(db_, title, author, media_kind=None):
        return _cw(db, title + " row", author=author)

    async def fake_acquire(db_, row, *, user_id, priority, shelf_id=None, context=None):
        grabbed.append(row.title)
        if row.hooked_work_id:   # in stock → acquire adds it to the user's library
            return {"route": "library", "status": "hooked", "work_id": row.hooked_work_id}
        return {"route": "pipeline", "status": "downloading", "job_id": 1}

    monkeypatch.setattr(series, "enumerate_author", fake_enum)
    monkeypatch.setattr(series, "_resolve_book_row", fake_resolve)
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)

    res = await series.acquire_author(db, "Prolific", refs=None, want_all=True, user_id=u.id,
                                      origin="following")
    # The cap (30) is enforced server-side: 30 results, and the in-stock one is ADDED, not skipped.
    assert len(res) == series.SERIES_ACQUIRE_CAP == 30
    by_ref = {r["ref"]: r["status"] for r in res}
    assert by_ref["r0"] == "hooked"               # in stock → added to the user's library
    assert "Book 0 stock row" in grabbed
    assert len(grabbed) == 30                      # all 30 chosen acquired (1 added-from-stock + 29 fetched)
    db.close()


# ----------------------------------------------------------------------- follow_tick behaviors
def _make_sub(kind, key, display, auto, known, user_id):
    db = SessionLocal()
    s = Subscription(user_id=user_id, kind=kind, key=key, display_name=display, active=True,
                     auto_request=auto, known_keys=known, auto_added=0)
    db.add(s); db.commit(); db.refresh(s); sid = s.id; db.close()
    return sid


def _a_user():
    db = SessionLocal()
    u = User(username="follower", password_hash="x", role="user"); db.add(u); db.commit()
    db.refresh(u); uid = u.id; db.close()
    return uid


@pytest.mark.asyncio
async def test_follow_tick_auto_on_opens_following_row_and_counts(monkeypatch):
    init_db(); _reset()
    uid = _a_user()
    sid = _make_sub("author", _author_norm("New Author"), "New Author", True,
                    known=[norm_title("Old One")], user_id=uid)

    # Roster grew by one NEW, not-owned book.
    async def fake_enum(db_, name):
        return [
            {"title": "Old One", "author": "New Author", "ref": "r0", "hooked_work_id": None,
             "catalog_id": None, "position": None, "year": None, "cover_url": None},
            {"title": "Brand New", "author": "New Author", "ref": "r1", "hooked_work_id": None,
             "catalog_id": None, "position": None, "year": None, "cover_url": None},
        ]
    acquired = []

    async def fake_resolve(db_, title, author, media_kind=None):
        d = SessionLocal()
        cw = _cw(d, title + " row", author=author); d.close()
        return cw

    async def fake_acquire(db_, row, *, user_id, priority, context=None, **kw):
        acquired.append((row.title, user_id, context.get("origin"), context.get("origin_detail")))
        return {"route": "pipeline", "status": "downloading"}

    monkeypatch.setattr(series, "enumerate_author", fake_enum)
    monkeypatch.setattr(series, "_resolve_book_row", fake_resolve)
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)

    from app.ingestion import scheduler
    db = SessionLocal()
    await scheduler.follow_tick.__wrapped__(db)
    db.close()

    # Only the NEW title fired, tagged origin="following" + the sub's user_id.
    assert acquired == [("Brand New row", uid, "following", "New Author")]
    db = SessionLocal()
    s = db.get(Subscription, sid)
    assert s.auto_added == 1
    assert set(s.known_keys) == {norm_title("Old One"), norm_title("Brand New")}  # baseline advanced
    assert s.last_checked_at is not None
    db.close()


@pytest.mark.asyncio
async def test_follow_tick_unseeded_establishes_baseline_without_fetching(monkeypatch):
    # known_keys=None (the subscribe-time seed failed) → the FIRST tick must record the whole roster
    # as the baseline and fetch NOTHING, NOT treat the backlog as new and fire it (P1 anti-flood).
    init_db(); _reset()
    uid = _a_user()
    sid = _make_sub("author", _author_norm("Prolific"), "Prolific", True, known=None, user_id=uid)

    async def fake_enum(db_, name):
        return [{"title": f"Backlog {i}", "author": "Prolific", "ref": f"r{i}",
                 "hooked_work_id": None, "catalog_id": None, "position": None, "year": None,
                 "cover_url": None} for i in range(50)]
    acquired = []

    async def fake_acquire(db_, row, **kw):
        acquired.append(row.title); return {"route": "pipeline", "status": "downloading"}
    monkeypatch.setattr(series, "enumerate_author", fake_enum)
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)

    from app.ingestion import scheduler
    db = SessionLocal()
    await scheduler.follow_tick.__wrapped__(db)
    db.close()

    assert acquired == []                                   # day-1 backlog NOT fetched
    db = SessionLocal()
    s = db.get(Subscription, sid)
    assert s.known_keys is not None and len(s.known_keys) == 50   # baseline established
    assert s.auto_added == 0
    db.close()


@pytest.mark.asyncio
async def test_follow_tick_auto_off_is_noop_but_advances_baseline(monkeypatch):
    init_db(); _reset()
    uid = _a_user()
    sid = _make_sub("author", _author_norm("Quiet"), "Quiet", False,
                    known=[norm_title("Old One")], user_id=uid)

    async def fake_enum(db_, name):
        return [
            {"title": "Old One", "author": "Quiet", "ref": "r0", "hooked_work_id": None,
             "catalog_id": None, "position": None, "year": None, "cover_url": None},
            {"title": "Unwanted New", "author": "Quiet", "ref": "r1", "hooked_work_id": None,
             "catalog_id": None, "position": None, "year": None, "cover_url": None},
        ]

    async def boom(*a, **k):
        raise AssertionError("auto-off sub must not acquire")

    monkeypatch.setattr(series, "enumerate_author", fake_enum)
    monkeypatch.setattr("app.ingestion.acquire.acquire", boom)

    from app.ingestion import scheduler
    db = SessionLocal()
    await scheduler.follow_tick.__wrapped__(db)
    db.close()

    db = SessionLocal()
    s = db.get(Subscription, sid)
    assert s.auto_added == 0
    # The baseline still advances (so re-enabling later doesn't flood the now-known title).
    assert set(s.known_keys) == {norm_title("Old One"), norm_title("Unwanted New")}
    db.close()


@pytest.mark.asyncio
async def test_follow_tick_skips_owned_new_title(monkeypatch):
    init_db(); _reset()
    uid = _a_user()
    _make_sub("author", _author_norm("Owns"), "Owns", True,
              known=[], user_id=uid)

    async def fake_enum(db_, name):
        return [{"title": "Already Owned", "author": "Owns", "ref": "r1", "hooked_work_id": 99,
                 "catalog_id": 5, "position": None, "year": None, "cover_url": None}]

    async def boom(*a, **k):
        raise AssertionError("owned title must not be re-acquired")

    monkeypatch.setattr(series, "enumerate_author", fake_enum)
    monkeypatch.setattr("app.ingestion.acquire.acquire", boom)

    from app.ingestion import scheduler
    db = SessionLocal()
    await scheduler.follow_tick.__wrapped__(db)
    db.close()


@pytest.mark.asyncio
async def test_follow_tick_transient_leaves_baseline_unchanged(monkeypatch):
    init_db(); _reset()
    uid = _a_user()
    sid = _make_sub("author", _author_norm("Flaky"), "Flaky", True,
                    known=[norm_title("Known")], user_id=uid)

    async def fake_enum(db_, name):
        series._mark_transient()          # a provider blip while enumerating
        return [{"title": "Maybe New", "author": "Flaky", "ref": "r1", "hooked_work_id": None,
                 "catalog_id": None, "position": None, "year": None, "cover_url": None}]

    async def boom(*a, **k):
        raise AssertionError("transient round must not acquire")

    monkeypatch.setattr(series, "enumerate_author", fake_enum)
    monkeypatch.setattr("app.ingestion.acquire.acquire", boom)

    from app.ingestion import scheduler
    db = SessionLocal()
    await scheduler.follow_tick.__wrapped__(db)
    db.close()

    db = SessionLocal()
    s = db.get(Subscription, sid)
    assert s.known_keys == [norm_title("Known")]   # NOT poisoned by the partial roster
    assert s.auto_added == 0
    db.close()
