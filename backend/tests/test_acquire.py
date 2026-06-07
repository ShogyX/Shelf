"""Tests for acquisition routing (fetch-source priority + route resolution) and the queued-hook
pipeline fallback."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.ingestion import acquire
from app.models import AppSetting, CatalogWork, DownloadJob, Integration, QueuedHook, Work


def _reset(db):
    for m in (DownloadJob, QueuedHook, CatalogWork, Integration, Work):
        db.execute(delete(m))
    for k in list(db.scalars(__import__("sqlalchemy").select(AppSetting.key)).all()):
        row = db.get(AppSetting, k)
        if row and "fetch_source_priority" in k:
            db.delete(row)
    db.commit()


def _cw(db, provider="web_index", *, ref="r", norm="the book", title="The Book",
        integration_id=None, hooked=None):
    cw = CatalogWork(provider=provider, provider_ref=ref, domain="d", work_url="u",
                     title=title, author="Auth", media_kind="text", norm_key=norm,
                     integration_id=integration_id, hooked_work_id=hooked)
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


def _enable_pipeline(db):
    db.add(Integration(kind="prowlarr", name="P", base_url="u", api_key="k", enabled=True))
    db.add(Integration(kind="sabnzbd", name="S", base_url="u", api_key="k", enabled=True))
    db.commit()


def test_priority_clean_and_overrides():
    init_db(); db = SessionLocal(); _reset(db)
    assert acquire.global_priority(db) == acquire.DEFAULT_PRIORITY
    # bad/duplicate entries cleaned, omitted routes appended for a full fallback chain
    acquire.set_global_priority(db, ["web_index", "web_index", "bogus"])
    assert acquire.global_priority(db) == ["web_index", "pipeline", "readarr", "kapowarr"]
    # per-user override, then clear
    user = SimpleNamespace(id=7)
    acquire.set_user_priority(db, 7, ["pipeline"])
    assert acquire.user_priority(db, user)[0] == "pipeline"
    acquire.set_user_priority(db, 7, None)
    assert acquire.user_priority(db, user) == acquire.global_priority(db)
    db.close()


def test_available_routes():
    init_db(); db = SessionLocal(); _reset(db)
    web = _cw(db, "web_index")
    assert "web_index" in acquire.available_routes(db, web)
    assert "pipeline" not in acquire.available_routes(db, web)  # no SAB/Prowlarr yet
    _enable_pipeline(db)
    assert "pipeline" in acquire.available_routes(db, web)
    db.close()


@pytest.mark.asyncio
async def test_acquire_prefers_pipeline_when_first(monkeypatch):
    init_db(); db = SessionLocal(); _reset(db)
    _enable_pipeline(db)
    rep = _cw(db, "openlibrary", ref="/works/B")
    grabbed = {}

    async def fake_auto_grab(db_, cw, *, user_id=None, shelf_id=None, context=None):
        grabbed["cw"] = cw.id
        return SimpleNamespace(id=99)
    monkeypatch.setattr("app.ingestion.downloads.auto_grab", fake_auto_grab)

    out = await acquire.acquire(db, rep, user_id=None, priority=["pipeline", "web_index"])
    assert out["route"] == "pipeline" and out["status"] == "downloading" and out["job_id"] == 99
    db.close()


@pytest.mark.asyncio
async def test_acquire_falls_through_to_web_index(monkeypatch):
    init_db(); db = SessionLocal(); _reset(db)
    # pipeline first in priority, but not configured → must fall through to web_index hook
    web = _cw(db, "web_index")
    hooked = Work(title="The Book"); db.add(hooked); db.commit(); db.refresh(hooked)

    async def fake_hook(db_, entry, **k):
        return hooked
    monkeypatch.setattr("app.ingestion.catalog.hook_entry", fake_hook)

    out = await acquire.acquire(db, web, user_id=None, priority=["pipeline", "web_index"])
    assert out["route"] == "web_index" and out["status"] == "hooked" and out["work_id"] == hooked.id
    db.close()


@pytest.mark.asyncio
async def test_acquire_already_hooked_short_circuits():
    init_db(); db = SessionLocal(); _reset(db)
    rep = _cw(db, "web_index", hooked=123)
    out = await acquire.acquire(db, rep, user_id=None, priority=acquire.DEFAULT_PRIORITY)
    assert out["status"] == "hooked" and out["work_id"] == 123
    db.close()


@pytest.mark.asyncio
async def test_queued_hook_pipeline_fallback(monkeypatch):
    """A pending hook with no crawlable source falls back to the pipeline → status 'downloading'."""
    from app.integrations import metadata_sync as ms
    init_db(); db = SessionLocal(); _reset(db)
    _enable_pipeline(db)
    _cw(db, "openlibrary", ref="/works/Z", norm="zelda", title="Zelda")  # a grabbable book row
    qh = QueuedHook(title="Zelda", norm_key="zelda", reason="goodreads", user_id=1, status="pending")
    db.add(qh); db.commit(); db.refresh(qh)

    async def fake_auto_grab(db_, cw, *, user_id=None, shelf_id=None):
        return SimpleNamespace(id=55)
    monkeypatch.setattr("app.ingestion.downloads.auto_grab", fake_auto_grab)

    out = await ms.process_queued_hooks(db)
    db.refresh(qh)
    assert qh.status == "downloading" and qh.detail == "dljob:55"
    db.close()


def test_reconcile_downloading_hooks():
    from app.integrations import metadata_sync as ms
    init_db(); db = SessionLocal(); _reset(db)
    w = Work(title="X"); db.add(w); db.commit(); db.refresh(w)
    job = DownloadJob(title="X", status="imported", work_id=w.id); db.add(job); db.commit(); db.refresh(job)
    qh = QueuedHook(title="X", norm_key="x", reason="goodreads", status="downloading",
                    detail=f"dljob:{job.id}")
    db.add(qh); db.commit(); db.refresh(qh)
    ms._reconcile_downloading_hooks(db)
    db.refresh(qh)
    assert qh.status == "hooked" and qh.hooked_work_id == w.id

    # a failed job sends it back to pending for retry, charging an attempt
    job2 = DownloadJob(title="Y", status="failed"); db.add(job2); db.commit(); db.refresh(job2)
    qh2 = QueuedHook(title="Y", norm_key="y", reason="goodreads", status="downloading",
                     detail=f"dljob:{job2.id}")
    db.add(qh2); db.commit(); db.refresh(qh2)
    ms._reconcile_downloading_hooks(db)
    db.refresh(qh2)
    assert qh2.status == "pending" and qh2.attempts == 1
    db.close()


def test_reconcile_bounds_retries():
    """A repeatedly-failing pipeline download must eventually give up (no infinite re-grab)."""
    from app.integrations import metadata_sync as ms
    init_db(); db = SessionLocal(); _reset(db)
    job = DownloadJob(title="Z", status="failed"); db.add(job); db.commit(); db.refresh(job)
    qh = QueuedHook(title="Z", norm_key="z", reason="goodreads", status="downloading",
                    detail=f"dljob:{job.id}")
    db.add(qh); db.commit()
    for _ in range(ms.MAX_HOOK_ATTEMPTS + 1):
        # simulate: each retry re-enters downloading then fails again
        qh.status, qh.detail = "downloading", f"dljob:{job.id}"
        db.commit()
        ms._reconcile_downloading_hooks(db)
        db.refresh(qh)
    assert qh.status == "failed"
    db.close()
