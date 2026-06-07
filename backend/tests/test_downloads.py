"""Tests for the download orchestration (grab → SABnzbd → import), with SAB mocked."""
from __future__ import annotations

import io
import os
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

import app.ingestion.adapters  # noqa: F401  — register the local_folder adapter
from app.db import SessionLocal, init_db
from app.ingestion import broken
from app.ingestion import downloads as dl
from app.integrations import IntegrationError
from app.integrations.sabnzbd import HistorySlot, QueueSlot, SABnzbdClient
from app.models import BrokenRelease, CatalogWork, DownloadJob, Integration, Work


def _make_epub(path, *, title, author):
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>'
        '<dc:language>en</dc:language></metadata></package>'
    )
    container = (
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        '</rootfiles></container>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
    path.write_bytes(buf.getvalue())
    return str(path)


def _fake_sync(db_, folder):
    """Stand-in for the watched-folder sync: import every file under the folder as a Work."""
    s = dl._local_source(db_)
    for dp, _dirs, files in os.walk(folder.path):
        for n in files:
            fp = os.path.join(dp, n)
            if db_.scalar(select(Work).where(Work.local_path == fp)):
                continue
            db_.add(Work(source_id=s.id, source_work_ref=fp, title=os.path.splitext(n)[0],
                         local_path=fp, local_size=os.path.getsize(fp)))
    db_.commit()


async def _no_del(self, nzo_id, *, del_files=False):
    return {"status": True}


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
    is_boxset: bool = False


@dataclass
class FakeScored:
    release: FakeRel = field(default_factory=FakeRel)
    info: FakeInfo = field(default_factory=FakeInfo)
    auto_ok: bool = True
    accepted: bool = True
    confidence: float = 0.95


def _setup(db):
    db.execute(delete(DownloadJob)); db.execute(delete(CatalogWork)); db.execute(delete(Integration))
    db.execute(delete(BrokenRelease))
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
        j.status = "imported"; db_.commit(); return "imported"
    async def fake_del(self, nzo_id, *, del_files=False):
        return {"status": True}
    monkeypatch.setattr(dl, "_import_completed", fake_import)
    monkeypatch.setattr(SABnzbdClient, "delete_history", fake_del)
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
    async def fake_del(self, nzo_id, *, del_files=False):
        return {"status": True}
    monkeypatch.setattr(SABnzbdClient, "queue", q_empty)
    monkeypatch.setattr(SABnzbdClient, "history", h_fail)
    monkeypatch.setattr(SABnzbdClient, "delete_history", fake_del)
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


def _stage_sab(db, *, library, mappings=None):
    sab = db.scalar(select(Integration).where(Integration.kind == "sabnzbd"))
    sab.config = {"category": "shelf", "library_path": str(library), "path_mappings": mappings or []}
    db.commit()
    return sab


@pytest.mark.asyncio
async def test_verify_pass_promotes_and_imports(monkeypatch, tmp_path):
    """A verified download is moved OUT of staging into the library, then imported + linked. The
    staging area (where SAB drops) is never the watched folder."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    library = tmp_path / "library"; library.mkdir()
    sab = _stage_sab(db, library=library)
    staging = tmp_path / "staging" / "job1"; staging.mkdir(parents=True)
    _make_epub(staging / "phm.epub", title="Project Hail Mary", author="Andy Weir")

    job = DownloadJob(catalog_work_id=cw.id, user_id=None, title="Project Hail Mary",
                      nzo_id="nzoA", status="completed", storage_path=str(staging),
                      candidates=[{"key": "guid:c1", "download_url": "u1"}], attempt=0)
    db.add(job); db.commit(); db.refresh(job)

    monkeypatch.setattr(dl, "ensure_watched_folder", lambda db_, root: SimpleNamespace(path=root, id=1))
    monkeypatch.setattr("app.ingestion.local_folder.sync_folder", _fake_sync)

    verdict = dl._import_completed(db, job, sab)
    db.refresh(job); db.refresh(cw)
    assert verdict == "imported" and job.status == "imported" and job.verified
    promoted = os.path.join(str(library), "Project Hail Mary", "phm.epub")
    assert os.path.exists(promoted)                          # moved into the library
    assert not os.path.exists(str(staging / "phm.epub"))     # gone from staging
    w = db.get(Work, job.work_id)
    assert w is not None and w.local_path == promoted and cw.hooked_work_id == w.id
    db.close()


@pytest.mark.asyncio
async def test_verify_fail_advances_to_next_candidate(monkeypatch, tmp_path):
    """The download completed but the content is a DIFFERENT book → mark that release broken and
    automatically grab the next candidate."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    sab = _stage_sab(db, library=tmp_path / "library")
    staging = tmp_path / "job"; staging.mkdir()
    _make_epub(staging / "wrong.epub", title="The Martian", author="Andy Weir")  # not what we asked

    job = DownloadJob(catalog_work_id=cw.id, title="Project Hail Mary", nzo_id="nzoA",
                      status="downloading", attempt=0,
                      candidates=[{"key": "guid:c1", "download_url": "u1", "title": "r1"},
                                  {"key": "guid:c2", "download_url": "u2", "title": "r2"}])
    db.add(job); db.commit(); db.refresh(job)

    async def q_empty(self, *, limit=100):
        return []
    async def h_done(self, *, limit=100, category=None):
        return [HistorySlot(nzo_id="nzoA", name="x", status="Completed", category="shelf",
                            storage=str(staging), fail_message=None, bytes=10)]
    async def fake_add(self, url, *, category=None, nzbname=None, priority=None):
        return {"nzo_ids": ["nzoB"]}
    monkeypatch.setattr(SABnzbdClient, "queue", q_empty)
    monkeypatch.setattr(SABnzbdClient, "history", h_done)
    monkeypatch.setattr(SABnzbdClient, "add_url", fake_add)
    monkeypatch.setattr(SABnzbdClient, "delete_history", _no_del)

    await dl.poll_tick(db); db.refresh(job)
    assert job.attempt == 1 and job.nzo_id == "nzoB" and job.status in ("queued", "downloading")
    assert broken.is_broken(db, {"key": "guid:c1"})       # the wrong release won't be tried again
    assert not broken.is_broken(db, {"key": "guid:c2"})
    db.close()


@pytest.mark.asyncio
async def test_sab_failure_advances_to_next_candidate(monkeypatch, tmp_path):
    """A corrupt/failed download (missing par2 blocks) → broken + next candidate."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    sab = _stage_sab(db, library=tmp_path / "library")
    job = DownloadJob(catalog_work_id=cw.id, title="Project Hail Mary", nzo_id="nzoA",
                      status="downloading", attempt=0,
                      candidates=[{"key": "guid:c1", "download_url": "u1"},
                                  {"key": "guid:c2", "download_url": "u2"}])
    db.add(job); db.commit(); db.refresh(job)

    async def q_empty(self, *, limit=100):
        return []
    async def h_fail(self, *, limit=100, category=None):
        return [HistorySlot(nzo_id="nzoA", name="x", status="Failed", category="shelf",
                            storage=None, fail_message="missing blocks", bytes=0)]
    async def fake_add(self, url, *, category=None, nzbname=None, priority=None):
        return {"nzo_ids": ["nzoB"]}
    monkeypatch.setattr(SABnzbdClient, "queue", q_empty)
    monkeypatch.setattr(SABnzbdClient, "history", h_fail)
    monkeypatch.setattr(SABnzbdClient, "add_url", fake_add)
    monkeypatch.setattr(SABnzbdClient, "delete_history", _no_del)

    await dl.poll_tick(db); db.refresh(job)
    assert job.attempt == 1 and job.nzo_id == "nzoB"
    assert broken.is_broken(db, {"key": "guid:c1"})
    db.close()


@pytest.mark.asyncio
async def test_cascade_exhausted_marks_failed(monkeypatch, tmp_path):
    init_db(); db = SessionLocal(); cw = _setup(db)
    sab = _stage_sab(db, library=tmp_path / "library")
    job = DownloadJob(catalog_work_id=cw.id, title="Project Hail Mary", nzo_id="nzoA",
                      status="downloading", attempt=0,
                      candidates=[{"key": "guid:only", "download_url": "u1"}])
    db.add(job); db.commit(); db.refresh(job)

    async def q_empty(self, *, limit=100):
        return []
    async def h_fail(self, *, limit=100, category=None):
        return [HistorySlot(nzo_id="nzoA", name="x", status="Failed", category="shelf",
                            storage=None, fail_message="repair failed", bytes=0)]
    monkeypatch.setattr(SABnzbdClient, "queue", q_empty)
    monkeypatch.setattr(SABnzbdClient, "history", h_fail)
    monkeypatch.setattr(SABnzbdClient, "delete_history", _no_del)

    await dl.poll_tick(db); db.refresh(job)
    assert job.status == "failed" and "repair" in (job.error or "")
    assert broken.is_broken(db, {"key": "guid:only"})
    db.close()


@pytest.mark.asyncio
async def test_poll_stale_branch_tz_safe(monkeypatch):
    """A just-enqueued job not yet visible in SAB queue/history hits the stale check; SQLite returns
    a naive created_at, so the _utcnow() subtraction must not raise (regression)."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    job = DownloadJob(catalog_work_id=cw.id, title="x", nzo_id="ghost", status="downloading",
                      candidates=[{"key": "guid:c", "download_url": "u"}])
    db.add(job); db.commit(); db.refresh(job)

    async def empty(self, *, limit=100, category=None):
        return []
    monkeypatch.setattr(SABnzbdClient, "queue", empty)
    monkeypatch.setattr(SABnzbdClient, "history", empty)
    await dl.poll_tick(db); db.refresh(job)            # must not raise
    assert job.status == "downloading"                  # fresh job → not stale → stays active
    db.close()


@pytest.mark.asyncio
async def test_auto_grab_builds_cascade(monkeypatch):
    init_db(); db = SessionLocal(); cw = _setup(db)
    captured = {}

    async def fake_find(db_, book, **k):
        return [FakeScored(auto_ok=False, confidence=0.7, release=FakeRel(guid="spec")),
                FakeScored(auto_ok=True, confidence=0.95, release=FakeRel(guid="auto"))]
    async def fake_grab(db_, c, scored=None, *, candidates=None, **k):
        captured["candidates"] = candidates
        return DownloadJob(catalog_work_id=c.id, title=c.title, status="queued")
    monkeypatch.setattr("app.ingestion.release_matcher.find_releases", fake_find)
    monkeypatch.setattr(dl, "grab_release", fake_grab)

    job = await dl.auto_grab(db, cw, user_id=1)
    assert job is not None
    assert captured["candidates"][0]["auto_ok"] is True          # auto-grabbable tried first
    assert any(not c["auto_ok"] for c in captured["candidates"])  # speculative included for verify

    async def none_found(db_, book, **k):
        return []
    monkeypatch.setattr("app.ingestion.release_matcher.find_releases", none_found)
    assert await dl.auto_grab(db, cw, user_id=1) is None          # nothing plausible → no grab

    async def only_spec(db_, book, **k):
        return [FakeScored(auto_ok=False, confidence=0.7)]
    monkeypatch.setattr("app.ingestion.release_matcher.find_releases", only_spec)
    assert await dl.auto_grab(db, cw, user_id=1, speculative=False) is None   # no bandwidth on guesses
    assert await dl.auto_grab(db, cw, user_id=1) is not None                  # default: grab + verify
    db.close()
