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
    from app.safety import require_destructive_ok
    require_destructive_ok("test_acquire table reset")  # must never run against the prod DB
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
    # bad/duplicate entries cleaned; omitted routes fill in at their DEFAULT_PRIORITY-relative slot
    # (NOT appended last) — so the explicitly-listed web_index keeps its relative position while the
    # unranked routes take their default order.
    acquire.set_global_priority(db, ["web_index", "web_index", "bogus"])
    assert acquire.global_priority(db) == ["torrent", "pipeline", "libgen", "web_index", "readarr", "kapowarr"]
    # per-user override (full explicit order is preserved verbatim), then clear → inherit global
    user = SimpleNamespace(id=7)
    acquire.set_user_priority(db, 7, ["pipeline", "torrent", "libgen", "web_index", "readarr", "kapowarr"])
    assert acquire.user_priority(db, user)[0] == "pipeline"
    acquire.set_user_priority(db, 7, None)
    assert acquire.user_priority(db, user) == acquire.global_priority(db)
    db.close()


def test_legacy_override_missing_torrent_still_fires_torrent_first():
    """A priority saved BEFORE the torrent route existed (so it omits 'torrent') must not bury torrent
    at the back of the chain — it slots into its default-first position, ahead of usenet + AA, while
    the user's explicit relative order of the rest is preserved."""
    init_db(); db = SessionLocal(); _reset(db)
    legacy = ["web_index", "libgen", "pipeline", "readarr", "kapowarr"]  # no 'torrent'
    eff = acquire._clean(legacy)
    assert eff[0] == "torrent"
    assert eff.index("torrent") < eff.index("pipeline")   # before usenet
    assert eff.index("torrent") < eff.index("libgen")     # before the AA fallback
    # the rest keep the user's explicit relative order
    assert eff.index("web_index") < eff.index("libgen") < eff.index("pipeline")
    db.close()


@pytest.mark.asyncio
async def test_acquire_cascade_tries_torrent_then_usenet_then_aa(monkeypatch):
    """With the default priority and all three routes configured, acquire() must ATTEMPT torrent
    first, then the usenet pipeline, then the Anna's Archive (libgen) fallback — each only after the
    previous yields no confident match."""
    init_db(); db = SessionLocal(); _reset(db)
    _enable_pipeline(db)                                   # makes 'pipeline' an available route
    # Unique norm_key so this test's mark_unavailable (status none) can't gate other tests that
    # reuse the default norm ('_reset' doesn't clear the missing-content ledger).
    rep = _cw(db, "openlibrary", ref="/works/Casc", norm="cascade book", title="Cascade Book")
    calls: list[str] = []

    async def fake_torrent_grab(db_, rep_, **k): calls.append("torrent"); return None
    async def fake_auto_grab(db_, cw, **k): calls.append("pipeline"); return None
    async def fake_libgen_grab(db_, rep_, **k): calls.append("libgen"); return None
    monkeypatch.setattr("app.ingestion.torrents.configured", lambda db_: True)
    monkeypatch.setattr("app.ingestion.torrents.grab", fake_torrent_grab)
    monkeypatch.setattr("app.ingestion.downloads.auto_grab", fake_auto_grab)
    monkeypatch.setattr("app.ingestion.libgen.configured", lambda db_: True)
    monkeypatch.setattr("app.ingestion.libgen.grab", fake_libgen_grab)

    out = await acquire.acquire(db, rep, user_id=None, priority=acquire.DEFAULT_PRIORITY, force=True)
    assert calls == ["torrent", "pipeline", "libgen"]     # exact order, torrent FIRST
    assert out["status"] == "none"                        # all three found nothing → unavailable
    db.close()


@pytest.mark.asyncio
async def test_forced_route_no_match_does_not_gate_whole_title():
    """CODE-H1: forcing ONE route that finds nothing must NOT mark the title unavailable — that would
    gate every OTHER route too, so a later normal acquire would return 'gated' without searching."""
    from app.ingestion import ledger
    init_db(); db = SessionLocal(); _reset(db)
    rep = _cw(db, "openlibrary", ref="/works/ForceNoGate", norm="force no gate", title="Force No Gate")
    # Force libgen (not configured here) → it can fulfil nothing.
    out = await acquire.acquire(db, rep, user_id=None, priority=acquire.DEFAULT_PRIORITY, route="libgen")
    assert out["status"] == "none"
    # The title must remain UN-gated (the full chain was never tried).
    gated, _ = ledger.is_gated(db, rep)
    assert gated is False
    db.close()


@pytest.mark.asyncio
async def test_libgen_adopts_existing_work_no_duplicate(monkeypatch):
    """CODE-M1: a libgen job for a title a SIBLING grab already imported must ADOPT that Work — not
    re-download and create a second Work (the sequential-job duplicate-Work race)."""
    from app.ingestion import libgen
    init_db(); db = SessionLocal(); _reset(db)
    w = Work(title="Dup Guard"); db.add(w); db.commit(); db.refresh(w)
    # Two catalog members of ONE logical book (same norm_key); member A already imported → hooked.
    _cw(db, "openlibrary", ref="/works/A", norm="dup guard", title="Dup Guard", hooked=w.id)
    cw2 = _cw(db, "openlibrary", ref="/works/B", norm="dup guard", title="Dup Guard")
    job = DownloadJob(title="Dup Guard", catalog_work_id=cw2.id, status="pending", candidates=[{"key": "x"}])
    db.add(job); db.commit(); db.refresh(job)

    downloaded = {"n": 0}
    async def fake_resolve(*a, **k):
        downloaded["n"] += 1
        return "fail"
    monkeypatch.setattr(libgen, "_resolve_download", fake_resolve)

    await libgen._advance_job(db, job, None, None, None)  # cfg/fetcher unused on the adopt path
    db.refresh(job)
    assert job.status == "imported" and job.work_id == w.id
    assert downloaded["n"] == 0                  # adopted → never downloaded
    assert db.query(Work).count() == 1           # no duplicate Work
    db.close()


@pytest.mark.asyncio
async def test_libgen_does_not_adopt_wrong_author_sibling(monkeypatch):
    """CODE-M1 must NOT adopt a same-title but DIFFERENT-author sibling (e.g. a study guide) — a
    norm_key cluster can mix editions, so adopting blindly would give the requester the wrong book."""
    from app.ingestion import libgen
    init_db(); db = SessionLocal(); _reset(db)
    w = Work(title="Ambiguous"); db.add(w); db.commit(); db.refresh(w)
    cw1 = _cw(db, "openlibrary", ref="/works/A", norm="ambiguous", title="Ambiguous", hooked=w.id)
    cw1.author = "Alice Realwriter"
    cw2 = _cw(db, "openlibrary", ref="/works/B", norm="ambiguous", title="Ambiguous")
    cw2.author = "Bob Studyguide"
    db.commit()
    job = DownloadJob(title="Ambiguous", catalog_work_id=cw2.id, status="pending", candidates=[{"key": "x"}])
    db.add(job); db.commit(); db.refresh(job)

    downloaded = {"n": 0}
    async def fake_resolve(*a, **k):
        downloaded["n"] += 1
        return "fail"
    monkeypatch.setattr(libgen, "_resolve_download", fake_resolve)

    await libgen._advance_job(db, job, SimpleNamespace(max_concurrent=1), object(), None)
    db.refresh(job)
    assert downloaded["n"] >= 1        # authors incompatible → did NOT adopt, fell through to download
    assert job.work_id != w.id         # never adopted the wrong-author Work
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

    async def fake_auto_grab(db_, cw, *, user_id=None, shelf_id=None, context=None, variant="ebook"):
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

    async def fake_auto_grab(db_, cw, *, user_id=None, shelf_id=None, variant="ebook"):
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
