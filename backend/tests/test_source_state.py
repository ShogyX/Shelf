"""Wave B per-(work, source) search-state machine (app/ingestion/source_state.py).

Covers: ensure_rows idempotency, the CAS lease (two leases — one wins), the terminal skip-set, the
record() transitions, drop_upstream_unavailable on resolve, due_unavailable selection, and the
availability cap (source_available_now / next_source_free_at)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import ledger, source_state
from app.models import (
    CatalogWork,
    ContentRequest,
    ContentRequestRequester,
    Integration,
    SourceAttempt,
    WorkSourceSearch,
)


def _db():
    init_db()
    db = SessionLocal()
    for m in (WorkSourceSearch, SourceAttempt, ContentRequestRequester, ContentRequest,
              CatalogWork, Integration):
        db.execute(delete(m))
    db.commit()
    return db


def _req(db, *, norm="srcstate"):
    cw = CatalogWork(provider="openlibrary", provider_ref="r", domain="d", work_url=f"u-{norm}",
                     title="Book", author="A", media_kind="text", norm_key=norm)
    db.add(cw); db.commit(); db.refresh(cw)
    return ledger._upsert(db, cw)


def _rows(db, req):
    return {r.source: r for r in db.scalars(select(WorkSourceSearch).where(
        WorkSourceSearch.content_request_id == req.id)).all()}


def test_ensure_rows_idempotent_and_durable_only():
    db = _db(); req = _req(db)
    source_state.ensure_rows(db, req, ["torrent", "pipeline", "web_index", "readarr"])
    rows = _rows(db, req)
    assert set(rows) == {"torrent", "pipeline"}          # only durable sources get a row
    assert all(r.status == "pending" for r in rows.values())
    # second call adds the missing libgen row, leaves the existing two untouched (no duplicates)
    rows["torrent"].status = "no_match"; db.commit()
    source_state.ensure_rows(db, req, ["torrent", "pipeline", "libgen"])
    rows = _rows(db, req)
    assert set(rows) == {"torrent", "pipeline", "libgen"}
    assert rows["torrent"].status == "no_match"          # untouched
    db.close()


def test_lease_cas_one_wins():
    db = _db(); req = _req(db)
    source_state.ensure_rows(db, req, ["torrent"])
    t1 = source_state.lease(db, req, "torrent")
    t2 = source_state.lease(db, req, "torrent")           # row already leased (searching) → loses
    assert t1 is not None and t2 is None
    row = _rows(db, req)["torrent"]
    assert row.status == "searching" and row.lease_token == t1
    db.close()


def test_terminal_sources_and_skip():
    db = _db(); req = _req(db)
    source_state.ensure_rows(db, req, ["torrent", "pipeline", "libgen"])
    source_state.record(db, req, "torrent", "no_match")
    source_state.record(db, req, "pipeline", "exhausted")
    assert source_state.terminal_sources(db, req) == {"torrent", "pipeline"}
    # a terminal row cannot be leased (not in the leasable set) → re-search is skipped (R22)
    assert source_state.lease(db, req, "torrent") is None
    db.close()


def test_record_transitions_set_retry_only_for_unavailable():
    db = _db(); req = _req(db)
    source_state.ensure_rows(db, req, ["torrent"])
    source_state.lease(db, req, "torrent")
    retry = datetime.now(UTC) + timedelta(hours=6)
    source_state.record(db, req, "torrent", "unavailable", reason="blocked", retry_at=retry)
    row = _rows(db, req)["torrent"]
    assert row.status == "unavailable" and row.next_retry_at is not None
    assert row.lease_token is None and row.attempts == 1   # lease released, attempt bumped
    # a non-unavailable transition clears next_retry_at
    source_state.record(db, req, "torrent", "no_match")
    assert _rows(db, req)["torrent"].next_retry_at is None
    db.close()


def test_drop_upstream_unavailable_on_resolve():
    db = _db(); req = _req(db)
    source_state.ensure_rows(db, req, ["torrent", "pipeline", "libgen"])
    source_state.record(db, req, "pipeline", "unavailable", retry_at=datetime.now(UTC))
    source_state.record(db, req, "libgen", "unavailable", retry_at=datetime.now(UTC))
    source_state.record(db, req, "torrent", "matched")    # torrent imported the title
    source_state.drop_upstream_unavailable(db, req, keep_source="torrent")
    rows = _rows(db, req)
    assert rows["torrent"].status == "matched"            # importer untouched
    assert rows["pipeline"].status == "skipped"           # other unavailable rows dropped
    assert rows["libgen"].status == "skipped"
    db.close()


def test_due_unavailable_selects_only_due_unresolved():
    db = _db()
    req = _req(db, norm="due"); other = _req(db, norm="notdue")
    source_state.ensure_rows(db, req, ["pipeline"])
    source_state.ensure_rows(db, other, ["pipeline"])
    past = datetime.now(UTC) - timedelta(minutes=1)
    future = datetime.now(UTC) + timedelta(days=1)
    source_state.record(db, req, "pipeline", "unavailable", retry_at=past)
    source_state.record(db, other, "pipeline", "unavailable", retry_at=future)
    due = source_state.due_unavailable(db, limit=10)
    assert [d.content_request_id for d in due] == [req.id]   # only the past-due, unresolved one
    # resolving the parent excludes its due rows
    req.status = "resolved"; db.commit()
    assert source_state.due_unavailable(db, limit=10) == []
    db.close()


def test_availability_cap_and_next_free_at():
    db = _db()
    # opt-in cap of 2/day on the pipeline (sabnzbd) integration; uncapped = always available
    assert source_state.source_available_now(db, "pipeline") is True   # no integration → uncapped
    db.add(Integration(kind="sabnzbd", name="S", base_url="u", api_key="k", enabled=True,
                       config={"max_daily_requests": 2}))
    db.commit()
    now = datetime.now(UTC)
    db.add(SourceAttempt(source="pipeline", ok=True, created_at=now - timedelta(hours=2)))
    assert source_state.source_available_now(db, "pipeline") is True    # 1 < 2
    db.add(SourceAttempt(source="pipeline", ok=False, created_at=now - timedelta(hours=1)))
    db.commit()
    assert source_state.source_available_now(db, "pipeline") is False   # 2 >= 2 → capped
    free = source_state.next_source_free_at(db, "pipeline")
    assert free is not None and free > now                              # frees when the oldest ages out
    db.close()
