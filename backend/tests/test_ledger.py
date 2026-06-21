"""Missing-content ledger: lifecycle hooks (note/unavailable/resolved), the acquire gate, the
periodic spread-out re-check tick, and the /missing API (scoping, stats, admin recheck)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import config_store
from app.db import SessionLocal, init_db
from app.ingestion import acquire, ledger
from app.main import app
from app.models import (
    CatalogWork,
    ContentRequest,
    ContentRequestRequester,
    QueuedHook,
    SourceAttempt,
    User,
    UserSession,
    WorkSourceSearch,
)


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for m in (WorkSourceSearch, SourceAttempt, QueuedHook, ContentRequestRequester,
              ContentRequest, CatalogWork, UserSession, User):
        db.execute(delete(m))
    db.commit()
    config_store.update(db, {"missing_recheck_days": "", "missing_recheck_batch": "",
                             "auto_request_series": False})
    db.close()
    yield


def _cw(db, *, norm="the book", title="The Book", media="text"):
    cw = CatalogWork(provider="openlibrary", provider_ref="r", domain="d", work_url=f"u-{norm}",
                     title=title, author="Auth", media_kind=media, norm_key=norm)
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


def _user(db, name, role="user"):
    u = User(username=name, password_hash="x", role=role, is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _aware(dt):
    return dt if (dt is None or dt.tzinfo) else dt.replace(tzinfo=UTC)


# ----------------------------------------------------------------- helper unit behavior
def test_note_request_opens_row_and_attaches_requester():
    db = SessionLocal()
    cw = _cw(db)
    row = ledger.note_request(db, cw, user_id=5)
    assert row is not None and row.status == "open"
    # second user requesting the same title reuses the row, adds a requester (no duplicate row)
    ledger.note_request(db, cw, user_id=6)
    rows = db.scalars(select(ContentRequest)).all()
    assert len(rows) == 1
    reqs = db.scalars(select(ContentRequestRequester.user_id)
                      .where(ContentRequestRequester.request_id == row.id)).all()
    assert set(reqs) == {5, 6}
    # re-requesting by the same user is idempotent (UNIQUE(request_id, user_id))
    ledger.note_request(db, cw, user_id=5)
    assert db.scalar(select(ContentRequestRequester.id).where(
        ContentRequestRequester.request_id == row.id,
        ContentRequestRequester.user_id == 5)) is not None
    assert len(db.scalars(select(ContentRequestRequester)
                          .where(ContentRequestRequester.user_id == 5)).all()) == 1
    db.close()


def test_mark_unavailable_schedules_future_recheck_and_gates():
    db = SessionLocal()
    cw = _cw(db)
    # first-time request is NOT gated
    assert ledger.is_gated(db, cw) == (False, None)
    row = ledger.mark_unavailable(db, cw, reason="no_match", provider="pipeline")
    assert row.status == "unavailable" and row.attempts == 1 and row.failure_reason == "no_match"
    assert _aware(row.next_check_at) > datetime.now(UTC)
    gated, nca = ledger.is_gated(db, cw)
    assert gated is True and _aware(nca) > datetime.now(UTC)
    # a due row (next_check_at in the past) is no longer gated
    row.next_check_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    assert ledger.is_gated(db, cw)[0] is False
    db.close()


def test_transient_reason_gets_short_recheck_not_full_lockout():
    """A transient provider block (Cloudflare/429/timeout) must NOT lock a recoverable title out for
    the full 14-day window — transient reasons get a short re-check; only permanent reasons get the
    long jittered interval."""
    db = SessionLocal()
    config_store.update(db, {"missing_recheck_days": 14})
    now = datetime.now(UTC)
    for reason in ("blocked", "rate_limited", "timeout"):
        cw = _cw(db, norm=f"transient {reason}", title=reason)
        row = ledger.mark_unavailable(db, cw, reason=reason, provider="libgen")
        delta = (_aware(row.next_check_at) - now).total_seconds()
        assert delta < 12 * 3600, f"{reason}: expected a short re-check, got {delta}s (~{delta/86400:.1f}d)"
    cw = _cw(db, norm="permanently gone", title="Gone")
    row = ledger.mark_unavailable(db, cw, reason="no_match", provider="pipeline")
    assert (_aware(row.next_check_at) - now).total_seconds() > 10 * 86400  # full window, not short
    db.close()


def test_jitter_spreads_recheck_within_band():
    db = SessionLocal()
    config_store.update(db, {"missing_recheck_days": 14})
    now = datetime.now(UTC)
    offs = [( ledger._next_check_at(now) - now).total_seconds() for _ in range(200)]
    base = 14 * 86400
    assert min(offs) >= base * 0.75 - 1 and max(offs) <= base * 1.25 + 1
    assert max(offs) - min(offs) > base * 0.2     # actually spread, not constant
    db.close()


def test_mark_resolved_clears_gate():
    db = SessionLocal()
    cw = _cw(db)
    ledger.mark_unavailable(db, cw, reason="no_match")
    assert ledger.is_gated(db, cw)[0] is True
    row = ledger.mark_resolved(db, cw)
    assert row.status == "resolved" and row.resolved_at is not None and row.next_check_at is None
    assert ledger.is_gated(db, cw)[0] is False
    db.close()


# ----------------------------------------------------------------- acquire gate
@pytest.mark.asyncio
async def test_acquire_exhaustion_records_unavailable_with_requester(monkeypatch):
    db = SessionLocal()
    cw = _cw(db, norm="lonely")
    # no routes configured / nothing enqueues → acquire returns "none" and records the ledger
    out = await acquire.acquire(db, cw, user_id=42, priority=acquire.DEFAULT_PRIORITY)
    assert out["status"] == "none"
    row = db.scalar(select(ContentRequest).where(ContentRequest.norm_key == "lonely"))
    assert row.status == "unavailable" and _aware(row.next_check_at) > datetime.now(UTC)
    assert db.scalar(select(ContentRequestRequester.id).where(
        ContentRequestRequester.request_id == row.id,
        ContentRequestRequester.user_id == 42)) is not None
    db.close()


@pytest.mark.asyncio
async def test_second_request_for_gated_title_does_not_search_but_attaches_requester(monkeypatch):
    db = SessionLocal()
    cw = _cw(db, norm="gatedbook")
    ledger.mark_unavailable(db, cw, reason="no_match")

    searched = {"n": 0}

    async def fake_auto_grab(*a, **k):
        searched["n"] += 1
        return None
    monkeypatch.setattr("app.ingestion.downloads.auto_grab", fake_auto_grab)

    out = await acquire.acquire(db, cw, user_id=99, priority=acquire.DEFAULT_PRIORITY)
    assert out["status"] == "gated" and out["next_check_at"]
    assert searched["n"] == 0                                   # NO search happened
    row = db.scalar(select(ContentRequest).where(ContentRequest.norm_key == "gatedbook"))
    assert db.scalar(select(ContentRequestRequester.id).where(
        ContentRequestRequester.request_id == row.id,
        ContentRequestRequester.user_id == 99)) is not None      # but the requester was attached
    db.close()


@pytest.mark.asyncio
async def test_force_bypasses_gate():
    db = SessionLocal()
    cw = _cw(db, norm="forcebook")
    ledger.mark_unavailable(db, cw, reason="no_match")
    out = await acquire.acquire(db, cw, user_id=None, priority=acquire.DEFAULT_PRIORITY, force=True)
    assert out["status"] != "gated"     # force searched (and re-recorded "none" since unconfigured)
    db.close()


@pytest.mark.asyncio
async def test_acquire_success_marks_resolved(monkeypatch):
    db = SessionLocal()
    cw = _cw(db, norm="winner")
    cw.provider = "web_index"; cw.hooked_work_id = None; db.commit()  # crawlable → web_index route
    ledger.mark_unavailable(db, cw, reason="no_match")            # previously gated

    from app.models import Work
    hooked = Work(title="Winner"); db.add(hooked); db.commit(); db.refresh(hooked)

    async def fake_hook(db_, entry, **k):
        return hooked
    monkeypatch.setattr("app.ingestion.catalog.hook_entry", fake_hook)

    out = await acquire.acquire(db, cw, user_id=None, priority=["web_index"], force=True)
    assert out["status"] == "hooked"
    row = db.scalar(select(ContentRequest).where(ContentRequest.norm_key == "winner"))
    assert row.status == "resolved"
    db.close()


# ----------------------------------------------------------------- source-retry tick (Wave B)
@pytest.mark.asyncio
async def test_source_retry_tick_legacy_sweep_resolves_and_reschedules(monkeypatch):
    """The legacy sweep (Wave B step 3): ``unavailable`` ContentRequests with ZERO per-source children
    (rows that predate Wave B) get a full-cascade force re-acquire on their due next_check_at — a found
    title resolves, a still-missing one is re-marked with a fresh next_check_at, a not-due one is left
    alone. Mirrors the old missing_recheck_tick semantics."""
    from app.ingestion import scheduler
    db = SessionLocal()
    due_found = _cw(db, norm="due-found")
    due_miss = _cw(db, norm="due-miss")
    not_due = _cw(db, norm="not-due")
    for cw in (due_found, due_miss, not_due):
        ledger.mark_unavailable(db, cw, reason="no_match")   # title-level row, NO source children
    past = datetime.now(UTC) - timedelta(minutes=1)
    future = datetime.now(UTC) + timedelta(days=30)
    db.scalar(select(ContentRequest).where(ContentRequest.norm_key == "due-found")).next_check_at = past
    db.scalar(select(ContentRequest).where(ContentRequest.norm_key == "due-miss")).next_check_at = past
    db.scalar(select(ContentRequest).where(ContentRequest.norm_key == "not-due")).next_check_at = future
    db.commit()

    async def fake_acquire(db_, cw, **k):
        assert k.get("force") is True              # the tick always force-searches
        assert k.get("route") is None              # legacy sweep is a FULL-cascade re-acquire
        if cw.norm_key == "due-found":
            ledger.mark_resolved(db_, cw)
            return {"status": "hooked"}
        ledger.mark_unavailable(db_, cw, reason="no_match")   # still missing → fresh jitter
        return {"status": "none"}
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)

    await scheduler.source_retry_tick.__wrapped__(db)

    found = db.scalar(select(ContentRequest).where(ContentRequest.norm_key == "due-found"))
    miss = db.scalar(select(ContentRequest).where(ContentRequest.norm_key == "due-miss"))
    nd = db.scalar(select(ContentRequest).where(ContentRequest.norm_key == "not-due"))
    assert found.status == "resolved"
    assert miss.status == "unavailable" and _aware(miss.next_check_at) > datetime.now(UTC)
    assert _aware(nd.next_check_at) == _aware(future)            # not-due was never touched
    db.close()


@pytest.mark.asyncio
async def test_source_retry_tick_respects_batch_cap(monkeypatch):
    from app.ingestion import scheduler
    db = SessionLocal()
    config_store.update(db, {"missing_recheck_batch": 2})
    past = datetime.now(UTC) - timedelta(minutes=1)
    for i in range(5):
        cw = _cw(db, norm=f"b{i}")
        ledger.mark_unavailable(db, cw, reason="no_match")
        db.scalar(select(ContentRequest).where(ContentRequest.norm_key == f"b{i}")).next_check_at = past
    db.commit()

    seen = []

    async def fake_acquire(db_, cw, **k):
        seen.append(cw.norm_key)
        ledger.mark_unavailable(db_, cw, reason="no_match")
        return {"status": "none"}
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)

    await scheduler.source_retry_tick.__wrapped__(db)
    assert len(seen) == 2          # legacy-sweep batch cap honored
    db.close()


@pytest.mark.asyncio
async def test_source_retry_tick_re_searches_only_due_source(monkeypatch):
    """Wave B step 2: a per-source ``unavailable`` row whose next_retry_at is due triggers
    ``acquire(route=<that source>, force=True)`` — only that source is re-searched (R21)."""
    from app.ingestion import scheduler, source_state
    from app.models import WorkSourceSearch
    db = SessionLocal()
    cw = _cw(db, norm="duesrc")
    req = ledger.mark_unavailable(db, cw, reason="blocked")
    source_state.ensure_rows(db, req, ["torrent", "pipeline", "libgen"])
    # pipeline is unavailable + due; the other two stay pending (not due).
    row = db.scalar(select(WorkSourceSearch).where(
        WorkSourceSearch.content_request_id == req.id, WorkSourceSearch.source == "pipeline"))
    row.status = "unavailable"
    row.next_retry_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()

    seen = []
    async def fake_acquire(db_, c, **k):
        seen.append(k.get("route"))
        assert k.get("force") is True
        return {"status": "none"}
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)

    await scheduler.source_retry_tick.__wrapped__(db)
    assert seen == ["pipeline"]    # ONLY the due source was re-searched
    db.close()


@pytest.mark.asyncio
async def test_source_retry_tick_reaps_stale_lease():
    """Wave B step 1: a 'searching' row whose lease has gone stale (its searcher crashed) is returned
    to 'pending' so it can be searched again."""
    from app.ingestion import scheduler, source_state
    from app.models import WorkSourceSearch
    db = SessionLocal()
    cw = _cw(db, norm="stale")
    req = ledger.mark_unavailable(db, cw, reason="no_match")
    source_state.ensure_rows(db, req, ["torrent"])
    row = db.scalar(select(WorkSourceSearch).where(WorkSourceSearch.content_request_id == req.id))
    row.status = "searching"
    row.lease_token = "x"
    row.leased_at = datetime.now(UTC) - timedelta(hours=2)   # stale
    db.commit()

    await scheduler.source_retry_tick.__wrapped__(db)
    db.refresh(row)
    assert row.status == "pending" and row.lease_token is None
    db.close()


# ----------------------------------------------------------------- API
def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200


def _seed_users_and_rows():
    """An admin + two users, and two unavailable ledger rows (one wanted by each user)."""
    from app.auth import hash_password
    db = SessionLocal()
    admin = _user(db, "root", role="admin"); admin.password_hash = hash_password("rootpw12")
    alice = _user(db, "alice"); alice.password_hash = hash_password("alicepw12")
    bob = _user(db, "bob"); bob.password_hash = hash_password("bobpw1234")
    db.commit()
    cwa = _cw(db, norm="alicebook", title="Alice Book")
    cwb = _cw(db, norm="bobbook", title="Bob Book")
    ra = ledger.mark_unavailable(db, cwa, reason="no_match", provider="pipeline")
    rb = ledger.mark_unavailable(db, cwb, reason="blocked", provider="libgen")
    ledger.note_request(db, cwa, alice.id)
    ledger.note_request(db, cwb, bob.id)
    ids = {"admin": admin.id, "alice": alice.id, "bob": bob.id, "ra": ra.id, "rb": rb.id}
    db.close()
    return ids


def test_get_missing_scopes_to_caller_and_admin_sees_all():
    _seed_users_and_rows()
    with TestClient(app) as c:
        _login(c, "alice", "alicepw12")
        mine = c.get("/api/missing").json()
        assert {m["title"] for m in mine} == {"Alice Book"}
        assert mine[0]["requested_at"] is not None and mine[0]["requesters"] is None
    with TestClient(app) as c:
        _login(c, "root", "rootpw12")
        allrows = c.get("/api/missing").json()
        assert {m["title"] for m in allrows} == {"Alice Book", "Bob Book"}
        a = next(m for m in allrows if m["title"] == "Alice Book")
        assert a["requester_count"] == 1 and a["requesters"] == ["alice"]
        # filters
        assert {m["title"] for m in c.get("/api/missing?reason=blocked").json()} == {"Bob Book"}
        assert c.get("/api/missing?status=resolved").json() == []
        assert c.get("/api/missing?reason=bogus").status_code == 400


def test_missing_stats_admin_only():
    _seed_users_and_rows()
    with TestClient(app) as c:
        _login(c, "alice", "alicepw12")
        assert c.get("/api/missing/stats").status_code == 403
    with TestClient(app) as c:
        _login(c, "root", "rootpw12")
        s = c.get("/api/missing/stats").json()
        assert s["total"] == 2 and s["total_unavailable"] == 2
        assert s["by_status"]["unavailable"] == 2
        assert s["by_reason"]["no_match"] == 1 and s["by_reason"]["blocked"] == 1
        assert s["next_due_at"] is not None


def test_admin_recheck_bypasses_gate(monkeypatch):
    ids = _seed_users_and_rows()

    from app.models import Work
    captured = {"force": None}

    async def fake_acquire(db_, cw, *, user_id=None, priority=None, force=False, **k):
        captured["force"] = force
        ledger.mark_resolved(db_, cw)              # pretend it's now obtainable
        return {"status": "hooked"}
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)

    with TestClient(app) as c:
        _login(c, "root", "rootpw12")
        r = c.post(f"/api/missing/{ids['ra']}/recheck")
        assert r.status_code == 200
        assert r.json()["status"] == "resolved"
    assert captured["force"] is True

    # a regular user can't force a recheck
    with TestClient(app) as c:
        _login(c, "alice", "alicepw12")
        assert c.post(f"/api/missing/{ids['rb']}/recheck").status_code == 403


def test_admin_recheck_resets_source_rows(monkeypatch):
    """Decision #4a: the admin recheck RESETS every durable per-source row (no_match/exhausted/
    unavailable → pending, leases cleared) so the forced re-acquire re-searches every source — and the
    response exposes the per-source state (the info-icon payload)."""
    from app.ingestion import source_state
    from app.models import ContentRequest, WorkSourceSearch
    ids = _seed_users_and_rows()
    db = SessionLocal()
    req = db.get(ContentRequest, ids["ra"])
    source_state.ensure_rows(db, req, ["torrent", "pipeline", "libgen"])
    # Drive the three sources into the three terminal/transient states the reset must clear.
    for src, st in (("torrent", "no_match"), ("pipeline", "exhausted"), ("libgen", "unavailable")):
        row = db.scalar(select(WorkSourceSearch).where(
            WorkSourceSearch.content_request_id == req.id, WorkSourceSearch.source == src))
        row.status = st
        row.lease_token = "stale"
    db.commit()

    async def fake_acquire(db_, cw, **k):
        return {"status": "none"}                  # don't actually search
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)

    with TestClient(app) as c:
        _login(c, "root", "rootpw12")
        r = c.post(f"/api/missing/{ids['ra']}/recheck")
        assert r.status_code == 200
        body = r.json()
        srcs = {s["source"]: s for s in body["sources"]}      # per-source state exposed in the API
        assert set(srcs) == {"torrent", "pipeline", "libgen"}
        assert all(s["status"] == "pending" for s in srcs.values())   # all reset

    db2 = SessionLocal()
    rows = db2.scalars(select(WorkSourceSearch).where(
        WorkSourceSearch.content_request_id == ids["ra"])).all()
    assert rows and all(r.status == "pending" and r.lease_token is None for r in rows)
    db2.close(); db.close()


def test_rescan_endpoint_queues_and_status(monkeypatch):
    """POST /missing/rescan (admin) queues the scope + GET /missing/rescan/status reports progress;
    a regular user is forbidden; an invalid scope (zero or >1 set) 400s."""
    from app.models import AppSetting
    ids = _seed_users_and_rows()
    db = SessionLocal(); db.execute(delete(AppSetting)); db.commit(); db.close()

    with TestClient(app) as c:
        _login(c, "alice", "alicepw12")
        assert c.post("/api/missing/rescan", json={"all": True}).status_code == 403
        assert c.get("/api/missing/rescan/status").status_code == 403

    with TestClient(app) as c:
        _login(c, "root", "rootpw12")
        assert c.post("/api/missing/rescan", json={}).status_code == 400          # nothing set
        assert c.post("/api/missing/rescan",
                      json={"all": True, "author": "x"}).status_code == 400        # >1 set
        r = c.post("/api/missing/rescan", json={"all": True})
        assert r.status_code == 200 and r.json()["queued"] == 2                    # both seeded rows
        s = c.get("/api/missing/rescan/status").json()
        assert s["total"] == 2 and s["queued"] == 2 and s["active"] is True and s["done"] == 0


def _qh(db, *, title, user_id=None, reason="goodreads", status="pending"):
    qh = QueuedHook(title=title, norm_key=title.lower(), reason=reason, status=status,
                    user_id=user_id)
    db.add(qh); db.commit(); db.refresh(qh)
    return qh


def test_goodreads_queued_hooks_surface_in_missing_with_tag():
    """R4/Batch E: pending Goodreads QueuedHook rows appear in /missing as virtual entries tagged
    origin='goodreads', scoped per-user, excluded by reason/non-open status filters."""
    ids = _seed_users_and_rows()
    db = SessionLocal()
    _qh(db, title="Alice GR", user_id=ids["alice"])
    _qh(db, title="Orphan GR", user_id=None)                          # unowned → admin-only
    _qh(db, title="Related X", user_id=ids["alice"], reason="related")  # not goodreads → never
    _qh(db, title="Hooked GR", user_id=ids["alice"], status="hooked")   # not pending → never
    db.close()

    with TestClient(app) as c:
        _login(c, "root", "rootpw12")
        allrows = c.get("/api/missing").json()
        titles = {m["title"] for m in allrows}
        assert {"Alice GR", "Orphan GR"} <= titles
        assert "Related X" not in titles and "Hooked GR" not in titles
        gr = next(m for m in allrows if m["title"] == "Alice GR")
        assert gr["origin"] == "goodreads" and gr["status"] == "open"
        # tagged rows carry no failure_reason → excluded by a reason filter or a non-open status
        assert "Alice GR" not in {m["title"] for m in c.get("/api/missing?reason=no_match").json()}
        assert "Alice GR" not in {m["title"] for m in c.get("/api/missing?status=resolved").json()}

    with TestClient(app) as c:
        _login(c, "alice", "alicepw12")
        titles = {m["title"] for m in c.get("/api/missing").json()}
        assert "Alice GR" in titles            # her own queued hook
        assert "Orphan GR" not in titles       # unowned hook is admin-only

    with TestClient(app) as c:
        _login(c, "bob", "bobpw1234")
        assert "Alice GR" not in {m["title"] for m in c.get("/api/missing").json()}  # scoped out


# ----------------------------------------------------------------- Wave D: origin stamping
def test_note_request_origin_stamps_new_row_only():
    """The auto-series hook tags a NEW sibling row origin='series' + the series name, but must NOT
    overwrite a row a user requested directly (which has no origin)."""
    db = SessionLocal()
    cw = _cw(db, norm="sib1", title="Sibling One")
    row = ledger.note_request(db, cw, user_id=1, origin="series", origin_detail="Mistborn")
    assert row.origin == "series" and row.origin_detail == "Mistborn"

    cw2 = _cw(db, norm="direct1", title="Direct One")
    direct = ledger.note_request(db, cw2, user_id=1)              # plain request → no origin
    assert direct.origin is None
    ledger.note_request(db, cw2, user_id=2, origin="series", origin_detail="X")  # later auto-pull
    db.refresh(direct)
    assert direct.origin is None and direct.origin_detail is None  # not overwritten
    db.close()


# ----------------------------------------------------------------- Wave D: sort + new API fields
def _cw_series(db, *, norm, title, series, pos):
    cw = CatalogWork(provider="openlibrary", provider_ref="r", domain="d", work_url=f"u-{norm}",
                     title=title, author="Auth", media_kind="text", norm_key=norm,
                     extra={"series": series, "series_position": pos})
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


def test_missing_sort_and_series_fields(monkeypatch):
    """Wave D: ``sort`` orders the list (newest default unchanged); the API surfaces catalog_work_id +
    series + series_position from the joined catalog row WITHOUT running detect_series; a bad sort 400s."""
    import app.ingestion.series as series_mod
    monkeypatch.setattr(series_mod, "detect_series",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("detect_series at list time")))
    from app.auth import hash_password
    db = SessionLocal()
    admin = _user(db, "root", role="admin"); admin.password_hash = hash_password("rootpw12"); db.commit()
    cz = _cw_series(db, norm="zeta", title="Zeta", series="Zephyr", pos=2)
    ca = _cw_series(db, norm="alpha", title="Alpha", series="Aurora", pos=1)
    for cw in (cz, ca):                          # cz created first → lower id → newest puts ca first
        ledger.mark_unavailable(db, cw, reason="no_match")
    db.close()

    with TestClient(app) as c:
        _login(c, "root", "rootpw12")
        newest = [m["title"] for m in c.get("/api/missing?sort=newest").json()]
        assert newest == ["Alpha", "Zeta"]                       # default order = newest id desc
        title_sort = [m["title"] for m in c.get("/api/missing?sort=title").json()]
        assert title_sort == ["Alpha", "Zeta"]
        series_sort = [m["title"] for m in c.get("/api/missing?sort=series").json()]
        assert series_sort == ["Alpha", "Zeta"]                  # Aurora < Zephyr
        a = next(m for m in c.get("/api/missing").json() if m["title"] == "Alpha")
        assert a["series"] == "Aurora" and a["series_position"] == 1
        assert a["catalog_work_id"] is not None and a["origin"] == "request"
        assert c.get("/api/missing?sort=bogus").status_code == 400


# ----------------------------------------------------------------- Wave D: auto-series hook
@pytest.mark.asyncio
async def test_auto_series_noop_when_toggle_off(monkeypatch):
    """auto_request_series defaults OFF → acquire_catalog never touches detect_series/acquire_series."""
    from app.ingestion import catalog as catalog_mod
    db = SessionLocal()
    config_store.update(db, {"auto_request_series": ""})         # ensure default (off)
    cw = _cw(db, norm="solo")
    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        return []
    monkeypatch.setattr("app.ingestion.series.acquire_series", boom)

    await catalog_mod._maybe_auto_series(db, cw, user=_user(db, "u1"), shelf_id=None)
    assert called["n"] == 0
    db.close()


@pytest.mark.asyncio
async def test_auto_series_enqueues_siblings_when_on(monkeypatch):
    """Toggle ON → acquire_catalog's hook runs acquire_series(want_all, origin='series')."""
    from app.ingestion import catalog as catalog_mod
    db = SessionLocal()
    config_store.update(db, {"auto_request_series": True})
    cw = _cw(db, norm="seed")
    seen = {}

    async def fake_acq(db_, c, *, refs, want_all, user_id, shelf_id=None, origin=None):
        seen.update(refs=refs, want_all=want_all, origin=origin)
        return []
    monkeypatch.setattr("app.ingestion.series.acquire_series", fake_acq)

    await catalog_mod._maybe_auto_series(db, cw, user=_user(db, "u2"), shelf_id=None)
    assert seen == {"refs": None, "want_all": True, "origin": "series"}
    config_store.update(db, {"auto_request_series": ""})         # reset for other tests
    db.close()


# ----------------------------------------------------------------- Released/Planned gate (Feature 1)
from datetime import date


def _cw_year(db, *, norm, year=None, extra=None):
    cw = CatalogWork(provider="openlibrary", provider_ref="r", domain="d", work_url=f"u-{norm}",
                     title=norm.title(), author="Auth", media_kind="text", norm_key=norm,
                     year=year, extra=extra)
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


def test_planned_until_future_year_unknown_and_past():
    """_planned_until: a FUTURE year (or future full date) → that date; an unknown date or a past/
    current year → None (Released — never blocks a fetchable title)."""
    db = SessionLocal()
    next_year = datetime.now(UTC).year + 1
    fut = _cw_year(db, norm="future-year", year=next_year)
    assert ledger._planned_until(fut) == date(next_year, 1, 1)
    fut_date = _cw_year(db, norm="future-date",
                        extra={"release_date": f"{next_year}-09-01"})
    assert ledger._planned_until(fut_date) == date(next_year, 9, 1)
    # unknown (no year, no extra date) → None
    assert ledger._planned_until(_cw_year(db, norm="unknown")) is None
    # past full date / past year → None even though a date IS known
    assert ledger._planned_until(_cw_year(db, norm="past-date",
                                          extra={"release_date": "1999-01-01"})) is None
    assert ledger._planned_until(_cw_year(db, norm="past-year", year=1999)) is None
    db.close()


@pytest.mark.asyncio
async def test_acquire_gates_planned_future_title_without_searching(monkeypatch):
    """A future-release title is marked status='planned' (+ release_date) and is NOT searched — even
    under force. The pipeline search must never run."""
    db = SessionLocal()
    next_year = datetime.now(UTC).year + 1
    cw = _cw_year(db, norm="planned1", year=next_year)

    searched = {"n": 0}
    async def fake_auto_grab(*a, **k):
        searched["n"] += 1
        return None
    monkeypatch.setattr("app.ingestion.downloads.auto_grab", fake_auto_grab)

    out = await acquire.acquire(db, cw, user_id=7, priority=acquire.DEFAULT_PRIORITY, force=True)
    assert out["status"] == "planned" and out["route"] is None
    assert out["release_date"] == f"{next_year}-01-01"
    assert searched["n"] == 0                                   # NO search even under force
    row = db.scalar(select(ContentRequest).where(ContentRequest.norm_key == "planned1"))
    assert row.status == "planned" and row.release_date == date(next_year, 1, 1)
    assert ledger.is_gated(db, cw)[0] is True                   # planned is gated
    db.close()


def test_is_gated_planned_row_without_release_date_is_not_gated():
    """STATE-3: a 'planned' row whose release_date is NULL can never un-plan (the sweep requires
    release_date NOT NULL) → it must NOT be gated forever; treat the dateless planned row as
    released/searchable."""
    db = SessionLocal()
    cw = _cw(db, norm="dateless-planned")
    row = ledger._upsert(db, cw)
    row.status = "planned"
    row.release_date = None
    db.commit()
    assert ledger.is_gated(db, cw) == (False, None)
    db.close()


@pytest.mark.asyncio
async def test_acquire_searches_normally_for_unknown_or_past_release(monkeypatch):
    """Unknown/past release date → Released → searched normally (no 'planned' gate). With nothing
    configured, acquire exhausts to 'none' and records the row unavailable (the existing behavior)."""
    db = SessionLocal()
    cw = _cw_year(db, norm="released1")                        # no year / no extra date = unknown
    out = await acquire.acquire(db, cw, user_id=8, priority=acquire.DEFAULT_PRIORITY)
    assert out["status"] == "none"
    row = db.scalar(select(ContentRequest).where(ContentRequest.norm_key == "released1"))
    assert row.status == "unavailable" and row.release_date is None
    db.close()


@pytest.mark.asyncio
async def test_planned_to_released_sweep_flips_and_searches(monkeypatch):
    """source_retry_tick's sweep: a 'planned' row whose release_date has passed flips to 'open',
    resets sources, and is force-re-acquired (auto-searched the moment it's released)."""
    from app.ingestion import scheduler
    db = SessionLocal()
    cw = _cw(db, norm="dropped")
    # A planned row whose release date is already in the past (i.e. it just released).
    row = ledger.mark_planned(db, cw, date(2000, 1, 1))
    assert row.status == "planned"

    seen = []
    async def fake_acquire(db_, c, **k):
        seen.append((c.norm_key, k.get("force")))
        return {"status": "none"}
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)

    await scheduler.source_retry_tick.__wrapped__(db)
    db.refresh(row)
    assert row.status == "open" and row.release_date is None    # gate cleared
    assert ("dropped", True) in seen                            # auto-searched (forced)
    db.close()


# ----------------------------------------------------------------- Mass rescan (Feature 2)
def test_rescan_queues_matching_searchable_rows_only():
    """queue_rescan stamps rescan_queued_at + resets sources on SEARCHABLE rows only (unavailable/open);
    planned + resolved are excluded. Scopes: all | author | series | ids."""
    from app.ingestion import rescan
    from app.models import AppSetting
    db = SessionLocal()
    db.execute(delete(AppSetting))
    db.commit()
    una = _cw(db, norm="una", title="Una"); ledger.mark_unavailable(db, una, reason="no_match")
    opn = _cw(db, norm="opn", title="Opn"); ledger.note_request(db, opn, user_id=1)  # status open
    plan = _cw(db, norm="plan", title="Plan"); ledger.mark_planned(db, plan, date(2999, 1, 1))
    res = _cw(db, norm="res", title="Res"); ledger.mark_unavailable(db, res, reason="no_match")
    ledger.mark_resolved(db, res)

    n = rescan.queue_rescan(db, scope="all")
    assert n == 2                                              # only unavailable + open
    queued = {r.norm_key for r in db.scalars(select(ContentRequest).where(
        ContentRequest.rescan_queued_at.is_not(None))).all()}
    assert queued == {"una", "opn"}                           # NOT plan/res
    st = rescan.rescan_status(db)
    assert st["total"] == 2 and st["queued"] == 2 and st["active"] is True and st["done"] == 0
    db.close()


def test_rescan_scope_author_and_ids():
    from app.ingestion import rescan
    from app.models import AppSetting
    db = SessionLocal()
    db.execute(delete(AppSetting)); db.commit()
    a = _cw(db, norm="a", title="A"); ra = ledger.mark_unavailable(db, a, reason="no_match")
    b = _cw(db, norm="b", title="B"); rb = ledger.mark_unavailable(db, b, reason="no_match")
    ra.author = "Jane"; rb.author = "John"; db.commit()
    assert rescan.queue_rescan(db, scope="author", author="jane") == 1   # case-insensitive
    # clear and re-queue by ids
    for r in db.scalars(select(ContentRequest)).all():
        r.rescan_queued_at = None
    db.execute(delete(AppSetting)); db.commit()
    assert rescan.queue_rescan(db, scope="ids", ids=[rb.id]) == 1
    only = db.scalars(select(ContentRequest).where(
        ContentRequest.rescan_queued_at.is_not(None))).all()
    assert [r.id for r in only] == [rb.id]
    db.close()


@pytest.mark.asyncio
async def test_rescan_drain_processes_sequentially_and_clears(monkeypatch):
    """rescan_drain_tick re-acquires queued rows ONE BY ONE (force=True), clears each marker, and ends
    the run (clears rescan_run) when the queue empties. The status endpoint then reports total/done."""
    from app.ingestion import rescan
    from app.models import AppSetting
    db = SessionLocal()
    db.execute(delete(AppSetting)); db.commit()
    config_store.update(db, {"missing_recheck_batch": 2})
    for i in range(3):
        cw = _cw(db, norm=f"q{i}", title=f"Q{i}")
        ledger.mark_unavailable(db, cw, reason="no_match")
    assert rescan.queue_rescan(db, scope="all") == 3

    order = []
    async def fake_acquire(db_, c, **k):
        assert k.get("force") is True
        order.append(c.norm_key)
        return {"status": "none"}
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)

    # First drain: batch cap of 2 → processes 2, queue still has 1, run still active.
    await rescan.rescan_drain_tick(db)
    assert len(order) == 2
    st = rescan.rescan_status(db)
    assert st["total"] == 3 and st["queued"] == 1 and st["done"] == 2 and st["active"] is True

    # Second drain: processes the last one, queue empties → run cleared (idle).
    await rescan.rescan_drain_tick(db)
    assert len(order) == 3
    st = rescan.rescan_status(db)
    assert st["queued"] == 0 and st["active"] is False and st["total"] == 0
    assert db.get(AppSetting, rescan.RESCAN_RUN_KEY) is None
    db.close()


@pytest.mark.asyncio
async def test_rescan_drain_clears_markers_when_acquire_raises(monkeypatch):
    """ERR-1: an acquire that raises (and rolls back) must NOT wedge the queue — every queued row's
    rescan_queued_at is still cleared (via a defensive re-fetch, not db.refresh which would raise
    ObjectDeletedError on a detached row) and the tick doesn't crash."""
    from app.ingestion import rescan
    from app.models import AppSetting
    db = SessionLocal()
    db.execute(delete(AppSetting)); db.commit()
    config_store.update(db, {"missing_recheck_batch": 5})
    for i in range(3):
        cw = _cw(db, norm=f"boom{i}", title=f"Boom{i}")
        ledger.mark_unavailable(db, cw, reason="no_match")
    assert rescan.queue_rescan(db, scope="all") == 3

    async def boom_acquire(db_, c, **k):
        raise RuntimeError("acquire blew up")
    monkeypatch.setattr("app.ingestion.acquire.acquire", boom_acquire)

    await rescan.rescan_drain_tick(db)        # must not raise

    remaining = db.scalars(select(ContentRequest).where(
        ContentRequest.rescan_queued_at.is_not(None))).all()
    assert remaining == []                    # every marker cleared despite the failures
    db.close()
