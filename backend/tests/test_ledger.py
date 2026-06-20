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
    config_store.update(db, {"missing_recheck_days": "", "missing_recheck_batch": ""})
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
