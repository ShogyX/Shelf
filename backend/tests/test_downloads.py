"""Tests for the download orchestration (grab → SABnzbd → import), with SAB mocked."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

import app.ingestion.adapters  # noqa: F401  — register the local_folder adapter
from app.db import SessionLocal, init_db
from app.ingestion import downloads as dl
from app.integrations import IntegrationError
from app.integrations.sabnzbd import HistorySlot, QueueSlot, SABnzbdClient
from app.models import CatalogWork, DownloadJob, Integration, Work


@dataclass
class FakeRel:
    title: str = "Andy.Weir-Project.Hail.Mary.EPUB"
    download_url: str | None = "http://idx/nzb/1"
    indexer: str = "NzbPlanet"
    size: int = 10_000_000
    guid: str = "g1"


@dataclass
class FakeInfo:
    fmt: str | None = "epub"


@dataclass
class FakeScored:
    release: FakeRel = field(default_factory=FakeRel)
    info: FakeInfo = field(default_factory=FakeInfo)
    auto_ok: bool = True
    accepted: bool = True


def _setup(db):
    db.execute(delete(DownloadJob)); db.execute(delete(CatalogWork)); db.execute(delete(Integration))
    db.commit()
    db.add(Integration(kind="sabnzbd", name="SAB", base_url="http://sab", api_key="k",
                       enabled=True, config={"category": "shelf",
                                             "path_mappings": [{"remote": "/media/NAS", "local": "/mnt/NAS"}]}))
    cw = CatalogWork(provider="openlibrary", provider_ref="/works/PHM", domain="openlibrary.org",
                     work_url="x", title="Project Hail Mary", author="Andy Weir",
                     media_kind="text", norm_key="project hail mary")
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


def test_map_path():
    maps = [{"remote": "/media/NAS", "local": "/mnt/NAS"}]
    assert dl.map_path("/media/NAS/Books/x", maps) == "/mnt/NAS/Books/x"
    assert dl.map_path("/other/x", maps) == "/other/x"
    assert dl.map_path(None, maps) is None
    # longest remote prefix wins
    m2 = [{"remote": "/media", "local": "/a"}, {"remote": "/media/NAS", "local": "/b"}]
    assert dl.map_path("/media/NAS/x", m2) == "/b/x"


@pytest.mark.asyncio
async def test_grab_release_creates_and_is_idempotent(monkeypatch):
    init_db(); db = SessionLocal(); cw = _setup(db)
    calls = {"n": 0}

    async def fake_add_url(self, url, *, category=None, nzbname=None, priority=None):
        calls["n"] += 1
        assert category == "shelf"
        return {"nzo_ids": ["nzo-1"]}

    monkeypatch.setattr(SABnzbdClient, "add_url", fake_add_url)
    job = await dl.grab_release(db, cw, FakeScored(), user_id=1, kind="manual")
    assert job.status == "queued" and job.nzo_id == "nzo-1" and calls["n"] == 1
    # second grab for the same catalog book while active → returns the SAME job, no new add_url
    job2 = await dl.grab_release(db, cw, FakeScored(), user_id=1)
    assert job2.id == job.id and calls["n"] == 1
    db.close()


@pytest.mark.asyncio
async def test_grab_dedups_across_cluster_rows(monkeypatch):
    """Two catalog rows for the SAME title (same norm_key) must not double-enqueue."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    other = CatalogWork(provider="web_index", provider_ref="w2", domain="d", work_url="u2",
                        title="Project Hail Mary", author="Andy Weir", media_kind="text",
                        norm_key=cw.norm_key)
    db.add(other); db.commit(); db.refresh(other)
    calls = {"n": 0}

    async def fake_add_url(self, url, *, category=None, nzbname=None, priority=None):
        calls["n"] += 1
        return {"nzo_ids": ["nzo-1"]}

    monkeypatch.setattr(SABnzbdClient, "add_url", fake_add_url)
    a = await dl.grab_release(db, cw, FakeScored(), user_id=1)
    b = await dl.grab_release(db, other, FakeScored(), user_id=1)  # different row, same title
    assert calls["n"] == 1 and b.id == a.id  # deduped across the cluster
    db.close()


@pytest.mark.asyncio
async def test_grab_release_rejects_hooked_and_no_url(monkeypatch):
    init_db(); db = SessionLocal(); cw = _setup(db)
    monkeypatch.setattr(SABnzbdClient, "add_url",
                        lambda self, url, **k: (_ for _ in ()).throw(AssertionError("should not call")))
    cw.hooked_work_id = 999; db.commit()
    with pytest.raises(IntegrationError):
        await dl.grab_release(db, cw, FakeScored(), user_id=1)
    cw.hooked_work_id = None; db.commit()
    with pytest.raises(IntegrationError):
        await dl.grab_release(db, cw, FakeScored(release=FakeRel(download_url=None)), user_id=1)
    db.close()


@pytest.mark.asyncio
async def test_poll_transitions(monkeypatch):
    init_db(); db = SessionLocal(); cw = _setup(db)
    job = DownloadJob(catalog_work_id=cw.id, user_id=1, title="Project Hail Mary",
                      nzo_id="nzo-1", status="queued", grab_kind="manual")
    db.add(job); db.commit(); db.refresh(job)

    # In queue → downloading.
    async def q_only(self, *, limit=100):
        return [QueueSlot(nzo_id="nzo-1", filename="x", status="Downloading", percentage=10,
                          category="shelf", mb=10, mb_left=9)]
    async def h_empty(self, *, limit=100, category=None):
        return []
    monkeypatch.setattr(SABnzbdClient, "queue", q_only)
    monkeypatch.setattr(SABnzbdClient, "history", h_empty)
    await dl.poll_tick(db); db.refresh(job)
    assert job.status == "downloading"

    # In history Completed → import is invoked (stubbed to mark imported).
    async def q_empty(self, *, limit=100):
        return []
    async def h_done(self, *, limit=100, category=None):
        return [HistorySlot(nzo_id="nzo-1", name="x", status="Completed",
                            category="shelf", storage="/media/NAS/Books/x", fail_message=None, bytes=10)]
    monkeypatch.setattr(SABnzbdClient, "queue", q_empty)
    monkeypatch.setattr(SABnzbdClient, "history", h_done)

    def fake_import(db_, j, sab):
        j.status = "imported"; db_.commit()
    monkeypatch.setattr(dl, "_import_completed", fake_import)
    out = await dl.poll_tick(db); db.refresh(job)
    assert job.status == "imported" and out["imported"] == 1
    db.close()


@pytest.mark.asyncio
async def test_poll_marks_failed(monkeypatch):
    init_db(); db = SessionLocal(); cw = _setup(db)
    job = DownloadJob(catalog_work_id=cw.id, title="x", nzo_id="nzo-2", status="downloading")
    db.add(job); db.commit(); db.refresh(job)

    async def q_empty(self, *, limit=100):
        return []
    async def h_fail(self, *, limit=100, category=None):
        return [HistorySlot(nzo_id="nzo-2", name="x", status="Failed", category="shelf",
                            storage=None, fail_message="repair failed", bytes=0)]
    monkeypatch.setattr(SABnzbdClient, "queue", q_empty)
    monkeypatch.setattr(SABnzbdClient, "history", h_fail)
    await dl.poll_tick(db); db.refresh(job)
    assert job.status == "failed" and "repair" in (job.error or "")
    db.close()


@pytest.mark.asyncio
async def test_grab_failure_is_tracked_not_orphaned(monkeypatch):
    init_db(); db = SessionLocal(); cw = _setup(db)

    async def boom(self, url, **k):
        raise IntegrationError("SAB down")

    monkeypatch.setattr(SABnzbdClient, "add_url", boom)
    with pytest.raises(IntegrationError):
        await dl.grab_release(db, cw, FakeScored(), user_id=1)
    # a row exists recording the failure (no untracked SAB download)
    job = db.scalar(select(DownloadJob).where(DownloadJob.catalog_work_id == cw.id))
    assert job is not None and job.status == "failed" and "SAB down" in (job.error or "")
    db.close()


@pytest.mark.asyncio
async def test_second_user_piggybacks(monkeypatch):
    init_db(); db = SessionLocal(); cw = _setup(db)
    calls = {"n": 0}

    async def fake_add_url(self, url, *, category=None, nzbname=None, priority=None):
        calls["n"] += 1
        return {"nzo_ids": ["nzo-X"]}

    monkeypatch.setattr(SABnzbdClient, "add_url", fake_add_url)
    a = await dl.grab_release(db, cw, FakeScored(), user_id=1)
    b = await dl.grab_release(db, cw, FakeScored(), user_id=2)  # different user, in-flight
    assert calls["n"] == 1                       # NOT enqueued twice
    assert b.id != a.id and b.user_id == 2 and b.nzo_id == a.nzo_id  # piggybacked
    db.close()


@pytest.mark.asyncio
async def test_import_matches_release_filename_not_decoy(monkeypatch, tmp_path):
    init_db(); db = SessionLocal(); cw = _setup(db)
    src = dl._local_source(db)
    # An older, unrelated book already sitting in the drop zone, plus our actual download.
    decoy = Work(source_id=src.id, source_work_ref="d", title="Some Other Book",
                 local_path=str(tmp_path / "Some.Other.Book.EPUB-XYZ.epub"), local_size=100)
    right = Work(source_id=src.id, source_work_ref="r", title="Pride and Prejudice",
                 local_path=str(tmp_path / "Jane.Austen.Pride.and.Prejudice.RETAiL.EPUB-NODE.epub"),
                 local_size=200)
    db.add_all([decoy, right]); db.commit(); db.refresh(right)

    job = DownloadJob(catalog_work_id=cw.id, user_id=None, title="Pride and Prejudice",
                      release_title="Jane.Austen.Pride.and.Prejudice.2000.RETAiL.EPUB.eBook-NODE",
                      nzo_id="nzo-1", status="completed",
                      storage_path="/media/NAS/Books/Jane.Austen.Pride.and.Prejudice")
    db.add(job); db.commit(); db.refresh(job)

    # books_root = the tmp dir (exists); the sanitized subdir does not → fallback filename match.
    monkeypatch.setattr(dl, "map_path", lambda p, m: str(tmp_path) + "/sanitized-gone")
    monkeypatch.setattr(dl, "ensure_watched_folder", lambda db_, root: None)  # skip real sync
    sab = db.scalar(select(Integration).where(Integration.kind == "sabnzbd"))
    dl._import_completed(db, job, sab)
    db.refresh(job)
    assert job.status == "imported" and job.work_id == right.id  # NOT the decoy
    db.close()


@pytest.mark.asyncio
async def test_import_uses_exact_job_subdir(monkeypatch, tmp_path):
    """When the job's own folder exists, import links the file in it (largest) regardless of how the
    EPUB's internal title parses — no fragile title-overlap gate (the Warmage regression)."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    src = dl._local_source(db)
    jobdir = tmp_path / "Mancour, Terry - Spellmonger 02 - Warmage [epub]"
    jobdir.mkdir()
    # The imported Work's title parsed oddly from the EPUB (doesn't match the book title) — must
    # still link because the file is in the exact job folder.
    w = Work(source_id=src.id, source_work_ref="r",
             title="Spellmonger Book Two The Warmage Saga Edition",
             local_path=str(jobdir / "warmage.epub"), local_size=500)
    db.add(w); db.commit(); db.refresh(w)
    job = DownloadJob(catalog_work_id=cw.id, user_id=None, title="Warmage",
                      release_title="Mancour, Terry - Spellmonger 02 - Warmage [epub]",
                      nzo_id="n", status="completed", storage_path="/media/NAS/Books/job")
    db.add(job); db.commit(); db.refresh(job)
    monkeypatch.setattr(dl, "ensure_watched_folder", lambda db_, root: None)  # skip real sync
    sab = db.scalar(select(Integration).where(Integration.kind == "sabnzbd"))
    # SAB may report storage as the FOLDER or as the unpacked FILE inside it — both must import.
    for storage in (str(jobdir), str(jobdir / "warmage.epub")):
        job.status = "completed"; job.work_id = None; db.commit()
        monkeypatch.setattr(dl, "map_path", lambda p, m, s=storage: s)
        dl._import_completed(db, job, sab)
        db.refresh(job)
        assert job.status == "imported" and job.work_id == w.id, storage
    db.close()


@pytest.mark.asyncio
async def test_auto_grab_uses_best_auto_ok(monkeypatch):
    init_db(); db = SessionLocal(); cw = _setup(db)

    async def fake_find(db_, book, **k):
        return [FakeScored(auto_ok=False), FakeScored(auto_ok=True)]
    async def fake_grab(db_, c, s, **k):
        return DownloadJob(catalog_work_id=c.id, title=c.title, status="queued")
    monkeypatch.setattr("app.ingestion.release_matcher.find_releases", fake_find)
    monkeypatch.setattr(dl, "grab_release", fake_grab)
    job = await dl.auto_grab(db, cw, user_id=1)
    assert job is not None

    async def none_auto(db_, book, **k):
        return [FakeScored(auto_ok=False)]
    monkeypatch.setattr("app.ingestion.release_matcher.find_releases", none_auto)
    assert await dl.auto_grab(db, cw, user_id=1) is None
    db.close()
