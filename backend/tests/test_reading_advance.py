"""Series resume auto-advance: the trigger gate and the background volume picker."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.models import CatalogWork, User, Work
from app.routers import reading
from app.schemas import ProgressOut


def _out(*, continue_id, last_id):
    return ProgressOut(
        work_id=1, last_chapter_id=last_id, scroll_fraction=1.0, paragraph_index=0,
        chapters_read=1, continue_chapter_id=continue_id,
    )


def test_should_advance_only_when_complete_series_and_at_end():
    series = Work(title="V1", series="S", status="complete")
    ongoing = Work(title="V1", series="S", status="ongoing")
    standalone = Work(title="V1", series=None, status="complete")
    at_end = _out(continue_id=5, last_id=5)
    mid = _out(continue_id=6, last_id=5)

    assert reading._should_advance_series(series, at_end) is True
    # ongoing/incomplete titles look "at end" only because later chapters aren't fetched yet
    assert reading._should_advance_series(ongoing, at_end) is False
    # not part of a series → nothing to advance to
    assert reading._should_advance_series(standalone, at_end) is False
    # still has a next chapter to read → not finished
    assert reading._should_advance_series(series, mid) is False


def _reset(db):
    db.execute(delete(Work))
    db.execute(delete(CatalogWork))
    db.execute(delete(User))
    db.commit()


def _setup(db):
    user = User(username="reader", password_hash="x", role="user")
    db.add(user)
    seed = CatalogWork(provider="hardcover", provider_ref="s1", domain="hardcover.app",
                       work_url="https://hardcover.app/s1", norm_key="v1",
                       title="V1", extra={"series": "S"})
    nxt = CatalogWork(provider="hardcover", provider_ref="s2", domain="hardcover.app",
                      work_url="https://hardcover.app/s2", norm_key="v2",
                      title="V2", extra={"series": "S"})
    db.add_all([seed, nxt])
    w1 = Work(title="V1", series="S", series_position=1.0, status="complete")
    db.add(w1)
    db.commit()
    db.refresh(user); db.refresh(seed); db.refresh(nxt); db.refresh(w1)
    return user, seed, nxt, w1


def _patch(monkeypatch, *, books, owned, calls):
    async def fake_detect(db, cw):
        return {"series": "S", "books": books}

    async def fake_acquire(db, cw, **kw):
        calls.append((cw.id, kw))
        return SimpleNamespace(id=1)

    monkeypatch.setattr("app.ingestion.series.detect_series", fake_detect)
    monkeypatch.setattr("app.ingestion.acquire.acquire", fake_acquire)
    monkeypatch.setattr("app.ingestion.acquire.user_priority", lambda db, u: 0)
    monkeypatch.setattr("app.library.library_work_ids", lambda db, uid: set(owned))


@pytest.mark.asyncio
async def test_advance_queues_next_unowned_volume(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        _reset(db)
        user, seed, nxt, w1 = _setup(db)
        books = [
            {"position": 1.0, "hooked_work_id": w1.id, "catalog_id": seed.id, "title": "V1"},
            {"position": 2.0, "hooked_work_id": None, "catalog_id": nxt.id, "title": "V2",
             "author": "A"},
        ]
        calls: list = []
        _patch(monkeypatch, books=books, owned={w1.id}, calls=calls)
        await reading._advance_series_bg(user.id, w1.id)
        assert len(calls) == 1 and calls[0][0] == nxt.id
        ctx = calls[0][1]["context"]
        assert ctx["volume"] == 2.0 and ctx["series"] == "S"
    finally:
        _reset(db); db.close()


@pytest.mark.asyncio
async def test_advance_skips_when_next_already_owned(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        _reset(db)
        user, seed, nxt, w1 = _setup(db)
        # Pretend the next volume is already hooked + in the user's library → nothing to queue.
        nxt.hooked_work_id = w1.id
        db.commit()
        books = [
            {"position": 1.0, "hooked_work_id": w1.id, "catalog_id": seed.id, "title": "V1"},
            {"position": 2.0, "hooked_work_id": w1.id, "catalog_id": nxt.id, "title": "V2"},
        ]
        calls: list = []
        _patch(monkeypatch, books=books, owned={w1.id}, calls=calls)
        await reading._advance_series_bg(user.id, w1.id)
        assert calls == []
    finally:
        _reset(db); db.close()
