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
from app.models import BrokenRelease, CatalogWork, DownloadJob, Integration, UsenetGrab, Work


@pytest.fixture(autouse=True)
def _stub_queue_delete(monkeypatch):
    """_cleanup_staging now also calls queue_delete (to cancel a still-downloading abandoned candidate
    so it can't complete as an orphan). Default it to a no-op so tests don't make real SAB calls; a
    test that asserts the cancel overrides this in its own body."""
    async def _noop(self, nzo_id, *, del_files=True):
        return {}
    monkeypatch.setattr(SABnzbdClient, "queue_delete", _noop)


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
    db.execute(delete(BrokenRelease)); db.execute(delete(UsenetGrab))
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


def test_promote_is_atomic_overwrite_no_temp_leak(tmp_path):
    """F0.7: promotion swaps the file into place atomically (os.replace, never remove-then-move)
    and overwrites a prior copy without leaving .part temps behind."""
    import os
    lib = tmp_path / "lib"
    lib.mkdir()
    src1 = tmp_path / "book.epub"
    src1.write_bytes(b"FIRST")
    out1 = dl._promote(str(src1), str(lib), "My Book")
    assert out1 and out1.endswith("book.epub") and open(out1, "rb").read() == b"FIRST"
    assert not src1.exists()                          # moved, not copied
    # a second verified copy for the same book overwrites in one atomic step
    src2 = tmp_path / "book.epub"
    src2.write_bytes(b"SECOND")
    out2 = dl._promote(str(src2), str(lib), "My Book")
    assert out2 == out1 and open(out2, "rb").read() == b"SECOND"
    leftovers = [p for p in os.listdir(os.path.dirname(out1)) if ".part" in p]
    assert leftovers == []                            # no temp residue
    # no lib dir → returned in place
    src3 = tmp_path / "loose.epub"
    src3.write_bytes(b"X")
    assert dl._promote(str(src3), None, "T") == str(src3)


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
async def test_grab_dedups_across_identity_when_norm_keys_differ(monkeypatch):
    """S-DUP-5: two rows for the same work under DIFFERENT titles (different norm_key) but the same
    canonical identity_key must dedup — a second user's request piggybacks the in-flight download
    instead of spawning a duplicate grab."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    cw.identity_key = "anilist:42"; db.commit()
    other = CatalogWork(provider="web_index", provider_ref="w3", domain="d", work_url="u3",
                        title="Translated Title", author="Andy Weir", media_kind="text",
                        norm_key="translated title", identity_key="anilist:42")
    db.add(other); db.commit(); db.refresh(other)
    calls = {"n": 0}

    async def fake_add_url(self, url, *, category=None, nzbname=None, priority=None):
        calls["n"] += 1
        return {"nzo_ids": ["nzo-x"]}

    monkeypatch.setattr(SABnzbdClient, "add_url", fake_add_url)
    a = await dl.grab_release(db, cw, FakeScored(), user_id=1)
    b = await dl.grab_release(db, other, FakeScored(), user_id=2)  # diff user, diff norm_key, same identity
    assert calls["n"] == 1                          # only ONE enqueue across the identity cluster
    assert b.id != a.id and b.user_id == 2          # user 2 gets their own follower row...
    assert b.nzo_id == a.nzo_id == "nzo-x"          # ...piggybacking the same SAB download
    db.close()


@pytest.mark.asyncio
async def test_reconcile_imports_operator_retried_failed_job(monkeypatch):
    """A job Shelf marked FAILED whose download an operator later retried in SAB to success is
    re-imported once its nzo shows 'completed' in SAB history — then it won't re-import (idempotent)."""
    from app.integrations.sabnzbd import HistorySlot
    init_db(); db = SessionLocal(); cw = _setup(db)
    job = DownloadJob(catalog_work_id=cw.id, user_id=1, title="Project Hail Mary",
                      release_title="Project.Hail.Mary.epub", nzo_id="nzoF", status="failed",
                      grab_kind="auto", candidates=[{"download_url": "u"}])
    db.add(job); db.commit(); db.refresh(job)

    async def hist(self, *, limit=100, category=None):
        return [HistorySlot(nzo_id="nzoF", name="Project.Hail.Mary", status="Completed",
                            category="shelf", storage="/media/NAS/Downloads/PHM",
                            fail_message=None, bytes=123)]
    monkeypatch.setattr(SABnzbdClient, "history", hist)

    def fake_import(_db, j, _sab):
        j.status = "imported"; j.work_id = 999
        return "imported"
    monkeypatch.setattr(dl, "_import_completed", fake_import)

    out = await dl.reconcile_completed_tick(db)
    assert out["reconciled"] == 1
    db.refresh(job)
    assert job.status == "imported" and job.storage_path == "/media/NAS/Downloads/PHM"
    # Second run is a no-op — the job is no longer failed, so it can't be re-imported.
    assert (await dl.reconcile_completed_tick(db))["reconciled"] == 0
    db.close()


@pytest.mark.asyncio
async def test_reconcile_ignores_unmatched_completion(monkeypatch):
    """A completed SAB item that matches no failed job (different nzo AND name) is left alone — the
    reconciler only acts on prior job requests."""
    from app.integrations.sabnzbd import HistorySlot
    init_db(); db = SessionLocal(); cw = _setup(db)
    db.add(DownloadJob(catalog_work_id=cw.id, user_id=1, title="Other Book",
                       release_title="Other.Book.epub", nzo_id="nzoX", status="failed",
                       grab_kind="auto"))
    db.commit()

    async def hist(self, *, limit=100, category=None):
        return [HistorySlot(nzo_id="nzoZ", name="Unrelated.Thing", status="Completed",
                            category="shelf", storage="/media/NAS/z", fail_message=None, bytes=1)]
    monkeypatch.setattr(SABnzbdClient, "history", hist)
    called = {"n": 0}

    def fake_import(_db, _j, _sab):
        called["n"] += 1
        return "imported"
    monkeypatch.setattr(dl, "_import_completed", fake_import)

    out = await dl.reconcile_completed_tick(db)
    assert out["reconciled"] == 0 and called["n"] == 0
    db.close()


@pytest.mark.asyncio
async def test_reconcile_imports_from_staging_dir_when_history_purged(monkeypatch, tmp_path):
    """Disk-staging pass: SAB purged its history, but the completed files still sit in .shelf-staging.
    The reconciler scans the dir and imports a SETTLED folder matching a FAILED job (verify-gated),
    routing the ebook into the library so staging can't pile up."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    library = tmp_path / "library"; library.mkdir()
    staging = tmp_path / ".shelf-staging"; staging.mkdir()
    sab = _stage_sab(db, library=library)              # category=shelf, identity (empty) path mappings
    folder = staging / "Project Hail Mary"; folder.mkdir()
    _make_epub(folder / "phm.epub", title="Project Hail Mary", author="Andy Weir")
    os.utime(folder, (1, 1))                            # old mtime → settled, not in-flight
    seed = staging / "_seed"; seed.mkdir()             # roots _staging_root at the .shelf-staging dir
    job = DownloadJob(catalog_work_id=cw.id, user_id=None, title="Project Hail Mary",
                      release_title="Project Hail Mary", nzo_id="nzoX", status="failed",
                      grab_kind="auto", storage_path=str(seed))
    db.add(job); db.commit(); db.refresh(job)

    async def no_hist(self, *, limit=100, category=None):
        return []                                      # SAB no longer has the history
    monkeypatch.setattr(SABnzbdClient, "history", no_hist)
    monkeypatch.setattr(dl, "ensure_watched_folder", lambda db_, root: SimpleNamespace(path=root, id=1))
    monkeypatch.setattr("app.ingestion.local_folder.sync_folder", _fake_sync)

    out = await dl.reconcile_completed_tick(db)
    db.refresh(job)
    assert job.status == "imported", out
    assert os.path.exists(os.path.join(str(library), "Project Hail Mary", "phm.epub"))  # routed to library
    assert not folder.exists()                          # staging folder cleaned after import
    db.close()


def test_gc_recognizes_shelf_staging_and_sweeps_dead_leftovers(tmp_path):
    """The GC now recognizes a '.shelf-staging' drop dir and sweeps orphan + failed/imported leftover
    folders (only IN-FLIGHT jobs' staging is protected), so the staging area can't pile up."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    staging = tmp_path / ".shelf-staging"; staging.mkdir()
    _stage_sab(db, library=tmp_path / "lib")
    orphan = staging / "orphan job"; orphan.mkdir()
    failed_left = staging / "failed leftover"; failed_left.mkdir()
    active = staging / "active download"; active.mkdir()
    for d in (orphan, failed_left, active):
        os.utime(d, (1, 1))                             # past the 2h grace
    db.add(DownloadJob(catalog_work_id=cw.id, title="X", status="failed", storage_path=str(failed_left)))
    db.add(DownloadJob(catalog_work_id=cw.id, title="Y", status="downloading", storage_path=str(active)))
    db.commit()

    dl.sweep_orphan_staging(db)
    assert not orphan.exists() and not failed_left.exists()  # orphan + dead failed leftover swept
    assert active.exists()                                   # in-flight download protected
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
    async def q_only(self, *, limit=100, start=0, category=None):
        return [QueueSlot(nzo_id="nzo-1", filename="x", status="Downloading", percentage=10,
                          category="shelf", mb=10, mb_left=9)]
    async def h_empty(self, *, limit=100, category=None):
        return []
    monkeypatch.setattr(SABnzbdClient, "queue", q_only)
    monkeypatch.setattr(SABnzbdClient, "history", h_empty)
    await dl.poll_tick(db); db.refresh(job)
    assert job.status == "downloading"

    # In history Completed → import is invoked (stubbed to mark imported).
    async def q_empty(self, *, limit=100, start=0, category=None):
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
async def test_poll_advances_on_stall(monkeypatch):
    """13B: a queued download whose mb_left hasn't moved past _STALL_AFTER is advanced to the next
    candidate (not held for the 12h age cap)."""
    from datetime import timedelta
    init_db(); db = SessionLocal(); cw = _setup(db)
    job = DownloadJob(catalog_work_id=cw.id, user_id=1, title="Project Hail Mary",
                      nzo_id="nzo-1", status="downloading", grab_kind="manual")
    db.add(job); db.commit(); db.refresh(job)

    async def q_stuck(self, *, limit=100, start=0, category=None):
        return [QueueSlot(nzo_id="nzo-1", filename="x", status="Downloading", percentage=50,
                          category="shelf", mb=10, mb_left=5.0)]   # same mb_left every poll
    async def h_empty(self, *, limit=100, category=None):
        return []
    monkeypatch.setattr(SABnzbdClient, "queue", q_stuck)
    monkeypatch.setattr(SABnzbdClient, "history", h_empty)

    # First poll records the progress baseline (mb_left=5).
    await dl.poll_tick(db); db.refresh(job)
    assert job.progress_mb_left == 5.0 and job.progress_at is not None and job.status == "downloading"

    # Age the progress marker past the stall window; mb_left unchanged → next poll advances.
    job.progress_at = dl._utcnow() - timedelta(minutes=31)
    db.commit()
    called = {}
    async def fake_grab_next(db_, j, sab, *, reason):
        called["reason"] = reason
        j.status = "failed"; j.error = reason; db_.commit(); return "failed"
    monkeypatch.setattr(dl, "_grab_next", fake_grab_next)
    await dl.poll_tick(db); db.refresh(job)
    assert "stalled" in called.get("reason", "") and job.status == "failed"
    db.close()


@pytest.mark.asyncio
async def test_poll_does_not_advance_when_globally_paused(monkeypatch):
    """A GLOBAL SAB pause reports its slots as 'Queued' (not 'paused'), so their mb_left is frozen.
    The poll must NOT read that as a per-download stall and advance the cascade — doing so abandoned
    near-complete downloads en masse and orphaned them when the queue was later resumed."""
    from datetime import timedelta
    init_db(); db = SessionLocal(); cw = _setup(db)
    job = DownloadJob(catalog_work_id=cw.id, user_id=1, title="Project Hail Mary",
                      nzo_id="nzo-1", status="downloading", grab_kind="manual")
    db.add(job); db.commit(); db.refresh(job)

    async def q_frozen(self, *, limit=100, start=0, category=None):
        return [QueueSlot(nzo_id="nzo-1", filename="x", status="Queued", percentage=99,
                          category="shelf", mb=10, mb_left=1.0)]   # 1 MB from done, frozen by the pause
    async def h_empty(self, *, limit=100, category=None):
        return []
    async def paused_true(self):
        return True
    monkeypatch.setattr(SABnzbdClient, "queue", q_frozen)
    monkeypatch.setattr(SABnzbdClient, "history", h_empty)
    monkeypatch.setattr(SABnzbdClient, "is_paused", paused_true)

    await dl.poll_tick(db); db.refresh(job)                 # baseline
    job.progress_at = dl._utcnow() - timedelta(minutes=120)  # age FAR past the stall window
    db.commit()

    called = {}
    async def fake_grab_next(db_, j, sab, *, reason):
        called["reason"] = reason
        j.status = "failed"; db_.commit(); return "failed"
    monkeypatch.setattr(dl, "_grab_next", fake_grab_next)

    await dl.poll_tick(db); db.refresh(job)
    assert "reason" not in called, "cascade advanced during a global pause"
    assert job.status == "downloading"
    db.close()


def test_untracked_match_hooks_catalog_or_falls_back_standalone(tmp_path):
    """An untracked completion is matched to the catalog by verifying its content against FTS
    candidates; a real match returns that CatalogWork, and content that matches nothing returns
    (None, <the file's own title>) so it can still be imported as a standalone Work."""
    init_db(); db = SessionLocal(); cw = _setup(db)   # cw = 'Project Hail Mary' / Andy Weir

    # matches the catalog work → returns it (release name carries an author prefix + format tokens)
    d1 = tmp_path / "match"; d1.mkdir()
    _make_epub(d1 / "phm.epub", title="Project Hail Mary", author="Andy Weir")
    matched, title = dl._untracked_match(
        db, str(d1), "Andy Weir - Project Hail Mary (retail) (epub)", is_audio=False, floor=0.6)
    assert matched is not None and matched.id == cw.id and title == "Project Hail Mary"

    # matches no catalog title → standalone: (None, the file's own embedded title)
    d2 = tmp_path / "nomatch"; d2.mkdir()
    _make_epub(d2 / "z.epub", title="An Utterly Unrelated Book 9Z", author="Nobody")
    matched2, title2 = dl._untracked_match(
        db, str(d2), "Nobody - An Utterly Unrelated Book 9Z (epub)", is_audio=False, floor=0.6)
    assert matched2 is None and "Unrelated Book" in title2
    db.close()


@pytest.mark.asyncio
async def test_standalone_audiobook_titled_from_tags_and_deduped(monkeypatch, tmp_path):
    """A STANDALONE audiobook (no catalog match) with a mangled scene folder name is titled from its
    embedded tags (album/artist), imports even though the filename-recall backstop fails (tracks are
    'Kapitel N', never the book title), and duplicate grabs of the same book dedupe to ONE Work."""
    init_db(); db = SessionLocal(); _setup(db)
    library = tmp_path / "library"; library.mkdir()
    sab = _stage_sab(db, library=library)

    # ffprobe would read these off the .mp3s; mock the reader + the structural audio check so the
    # test needs no real audio codec.
    monkeypatch.setattr("app.ingestion.verify.read_audio_meta",
                        lambda root: {"title": "Was hinter den vermauerten Türen geschah",
                                      "author": "Wolf Schneider"})
    monkeypatch.setattr("app.ingestion.verify.check_media_file", lambda p, k: (True, "ok"))

    def _audio_job(idx):
        d = tmp_path / f"stg{idx}"; d.mkdir()
        (d / "01 Kapitel 1.mp3").write_bytes(b"ID3fakeaudio")   # find_audio_files needs real files
        (d / "02 Kapitel 2.mp3").write_bytes(b"ID3fakeaudio")
        job = DownloadJob(catalog_work_id=None, user_id=None, fmt="audio", grab_kind="untracked",
                          title=f"(0{idx}_13) - Description - _Wolf Schneider - Was ....par2_",
                          status="completed", storage_path=str(d))
        db.add(job); db.commit(); db.refresh(job)
        return job

    v1 = dl._import_completed(db, _audio_job(1), sab)
    v2 = dl._import_completed(db, _audio_job(2), sab)   # a duplicate grab of the same audiobook
    assert v1 == "imported" and v2 == "imported"

    # Scope to this title (other tests share the DB and leave audio Works behind): both imports of the
    # same book collapse to ONE Work — titled/authored from the tags, on the audiobook path.
    audio = db.scalars(select(Work).where(
        Work.media_kind == "audio",
        Work.title == "Was hinter den vermauerten Türen geschah")).all()
    assert len(audio) == 1                                            # deduped to ONE Work
    assert audio[0].author == "Wolf Schneider"                        # from tags, not the mangled name
    assert "/Audiobooks/" in (audio[0].local_path or "")             # on the audiobook path
    db.close()


@pytest.mark.asyncio
async def test_cleanup_staging_cancels_queue_not_just_history(monkeypatch):
    """Abandoning a candidate must cancel it in the SAB QUEUE too — not only delete_history (a no-op
    for a still-downloading nzo) — else the abandoned download completes and lands as an orphan."""
    sab = Integration(kind="sabnzbd", base_url="http://sab.invalid", api_key="k", config={})
    calls = []
    async def rec_q(self, nzo_id, *, del_files=True): calls.append("queue")
    async def rec_h(self, nzo_id, *, del_files=False): calls.append("history")
    monkeypatch.setattr(SABnzbdClient, "queue_delete", rec_q)
    monkeypatch.setattr(SABnzbdClient, "delete_history", rec_h)
    await dl._cleanup_staging(DownloadJob(title="x", nzo_id="nzo-xyz", status="downloading"), sab)
    assert calls == ["queue", "history"]


@pytest.mark.asyncio
async def test_poll_marks_failed(monkeypatch):
    init_db(); db = SessionLocal(); cw = _setup(db)
    job = DownloadJob(catalog_work_id=cw.id, title="x", nzo_id="nzo-2", status="downloading")
    db.add(job); db.commit(); db.refresh(job)

    async def q_empty(self, *, limit=100, start=0, category=None):
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
async def test_import_recovers_series_and_hook_when_add_to_library_fails(monkeypatch, tmp_path):
    """If add_to_library raises after a durable import, the rollback must NOT lose the series tag or
    the catalog hook (review F10/F23/F25). The work stays imported and both persist after recovery."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    library = tmp_path / "library"; library.mkdir()
    sab = _stage_sab(db, library=library)
    staging = tmp_path / "staging" / "job1"; staging.mkdir(parents=True)
    _make_epub(staging / "phm.epub", title="Project Hail Mary", author="Andy Weir")
    job = DownloadJob(catalog_work_id=cw.id, user_id=1, title="Project Hail Mary",
                      nzo_id="nzoA", status="completed", storage_path=str(staging),
                      candidates=[{"key": "guid:c1", "download_url": "u1"}], attempt=0)
    db.add(job); db.commit(); db.refresh(job)

    monkeypatch.setattr(dl, "ensure_watched_folder", lambda db_, root: SimpleNamespace(path=root, id=1))
    monkeypatch.setattr("app.ingestion.local_folder.sync_folder", _fake_sync)
    monkeypatch.setattr(dl, "_notify_import", lambda *a, **k: None)
    monkeypatch.setattr(dl, "_apply_series", lambda work, cw_: setattr(work, "series", "Hail Saga"))
    monkeypatch.setattr("app.library.add_to_library",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("shelf placement failed")))

    verdict = dl._import_completed(db, job, sab)
    db.refresh(job); db.refresh(cw)
    assert verdict == "imported" and job.status == "imported" and job.verified
    w = db.get(Work, job.work_id)
    assert w is not None and cw.hooked_work_id == w.id   # catalog hook survived the rollback
    assert w.series == "Hail Saga"                        # series survived the rollback (the fix)
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

    async def q_empty(self, *, limit=100, start=0, category=None):
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

    async def q_empty(self, *, limit=100, start=0, category=None):
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

    async def q_empty(self, *, limit=100, start=0, category=None):
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
async def test_cascade_exhausted_marks_pipeline_source_exhausted(monkeypatch, tmp_path):
    """Wave B additive: when the usenet cascade is exhausted, the per-(work, pipeline) source row goes
    TERMINAL ('exhausted') — alongside the existing title-level mark_unavailable."""
    from app.ingestion import ledger, source_state
    from app.models import WorkSourceSearch
    init_db(); db = SessionLocal(); cw = _setup(db)
    sab = _stage_sab(db, library=tmp_path / "library")
    # Seed the ledger row + its pipeline source child so the worker hook has somewhere to record.
    req = ledger._upsert(db, cw)
    source_state.ensure_rows(db, req, ["pipeline"])
    job = DownloadJob(catalog_work_id=cw.id, title="Project Hail Mary", nzo_id="nzoX",
                      status="downloading", attempt=0,
                      candidates=[{"key": "guid:only", "download_url": "u1"}])
    db.add(job); db.commit(); db.refresh(job)

    async def q_empty(self, *, limit=100, start=0, category=None):
        return []
    async def h_fail(self, *, limit=100, category=None):
        return [HistorySlot(nzo_id="nzoX", name="x", status="Failed", category="shelf",
                            storage=None, fail_message="repair failed", bytes=0)]
    monkeypatch.setattr(SABnzbdClient, "queue", q_empty)
    monkeypatch.setattr(SABnzbdClient, "history", h_fail)
    monkeypatch.setattr(SABnzbdClient, "delete_history", _no_del)

    await dl.poll_tick(db); db.refresh(job)
    assert job.status == "failed"
    row = db.scalar(select(WorkSourceSearch).where(
        WorkSourceSearch.content_request_id == req.id, WorkSourceSearch.source == "pipeline"))
    assert row.status == "exhausted"
    db.close()


@pytest.mark.asyncio
async def test_cascade_infra_outage_marks_pipeline_unavailable_not_exhausted(monkeypatch, tmp_path):
    """Wave B P0 regression: when the cascade can't advance because SAB is UNREACHABLE (not because
    candidates broke), the pipeline source row is TRANSIENT 'unavailable' (retried), NOT terminal
    'exhausted' — a brief outage must never permanently lock the source out (R22)."""
    from app.ingestion import ledger, source_state
    from app.integrations import IntegrationError
    from app.models import WorkSourceSearch
    init_db(); db = SessionLocal(); cw = _setup(db)
    _stage_sab(db, library=tmp_path / "library")
    req = ledger._upsert(db, cw)
    source_state.ensure_rows(db, req, ["pipeline"])
    job = DownloadJob(catalog_work_id=cw.id, title="Project Hail Mary", nzo_id="nzoX",
                      status="downloading", attempt=0,
                      candidates=[{"key": "guid:a", "download_url": "u1"},
                                  {"key": "guid:b", "download_url": "u2"}])
    db.add(job); db.commit(); db.refresh(job)

    async def q_empty(self, *, limit=100, start=0, category=None):
        return []
    async def h_fail(self, *, limit=100, category=None):
        return [HistorySlot(nzo_id="nzoX", name="x", status="Failed", category="shelf",
                            storage=None, fail_message="repair failed", bytes=0)]
    async def enqueue_down(*a, **k):
        raise IntegrationError("sabnzbd unreachable")
    monkeypatch.setattr(SABnzbdClient, "queue", q_empty)
    monkeypatch.setattr(SABnzbdClient, "history", h_fail)
    monkeypatch.setattr(SABnzbdClient, "delete_history", _no_del)
    monkeypatch.setattr(dl, "_enqueue_available", enqueue_down)

    await dl.poll_tick(db); db.refresh(job)
    assert job.status == "failed"
    row = db.scalar(select(WorkSourceSearch).where(
        WorkSourceSearch.content_request_id == req.id, WorkSourceSearch.source == "pipeline"))
    assert row.status == "unavailable" and row.next_retry_at is not None
    db.close()


@pytest.mark.asyncio
async def test_poll_stale_branch_tz_safe(monkeypatch):
    """A just-enqueued job not yet visible in SAB queue/history hits the stale check; SQLite returns
    a naive created_at, so the _utcnow() subtraction must not raise (regression)."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    job = DownloadJob(catalog_work_id=cw.id, title="x", nzo_id="ghost", status="downloading",
                      candidates=[{"key": "guid:c", "download_url": "u"}])
    db.add(job); db.commit(); db.refresh(job)

    async def empty(self, *, limit=100, start=0, category=None):
        return []
    monkeypatch.setattr(SABnzbdClient, "queue", empty)
    monkeypatch.setattr(SABnzbdClient, "history", empty)
    await dl.poll_tick(db); db.refresh(job)            # must not raise
    assert job.status == "downloading"                  # fresh job → not stale → stays active
    db.close()


@pytest.mark.asyncio
async def test_poll_waits_when_completion_not_visible(monkeypatch, tmp_path):
    """A completed download whose files aren't visible yet (mount lag) must NOT be treated as a
    verify failure: the release stays untouched (not broken/deleted) and the job re-polls."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    _stage_sab(db, library=tmp_path / "lib")
    job = DownloadJob(catalog_work_id=cw.id, title="Project Hail Mary", nzo_id="nzoW",
                      status="downloading", storage_path="/nope/not/here", attempt=0,
                      candidates=[{"key": "guid:c", "download_url": "u"}])
    db.add(job); db.commit(); db.refresh(job)

    async def q_empty(self, *, limit=100, start=0, category=None):
        return []
    async def h_done(self, *, limit=100, category=None):
        return [HistorySlot(nzo_id="nzoW", name="x", status="Completed", category="shelf",
                            storage="/nope/not/here/job", fail_message=None, bytes=10)]
    monkeypatch.setattr(SABnzbdClient, "queue", q_empty)
    monkeypatch.setattr(SABnzbdClient, "history", h_done)
    monkeypatch.setattr(SABnzbdClient, "delete_history", _no_del)

    await dl.poll_tick(db); db.refresh(job)
    assert job.status == "downloading"                     # waiting, not failed
    assert not broken.is_broken(db, {"key": "guid:c"})     # good release NOT blacklisted
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


# ---- Per-listing daily download cap (≤2/day per usenet listing → defer) ----------------------

def _seed_grabs(db, key, n, *, ages_h):
    """Seed `n` ledger grabs of `key`, each `ages_h` hours old."""
    now = datetime.now(UTC)
    for h in ages_h:
        db.add(UsenetGrab(release_key=key, created_at=now - timedelta(hours=h)))
    db.commit()


def test_grab_blocked_until_counts_window():
    init_db(); db = SessionLocal(); _setup(db)
    key = "guid:rl1"
    assert dl._grab_blocked_until(db, key, limit=2) is None          # 0 grabs → allowed
    _seed_grabs(db, key, 1, ages_h=[1])
    assert dl._grab_blocked_until(db, key, limit=2) is None          # 1 grab → still allowed
    _seed_grabs(db, key, 1, ages_h=[3])
    until = dl._grab_blocked_until(db, key, limit=2)                 # 2 grabs → blocked
    assert until is not None
    # A 25h-old grab is outside the window and must not count.
    _seed_grabs(db, key, 1, ages_h=[25])
    assert dl._grab_blocked_until(db, key, limit=2) is not None      # still 2 in-window
    db.close()


@pytest.mark.asyncio
async def test_grab_defers_when_listing_capped(monkeypatch):
    init_db(); db = SessionLocal(); cw = _setup(db)
    key = dl._candidate_from_scored(FakeScored())["key"]
    _seed_grabs(db, key, 2, ages_h=[2, 5])                          # already 2 today

    async def must_not_add(self, url, **k):
        raise AssertionError("should not enqueue a capped listing")
    monkeypatch.setattr(SABnzbdClient, "add_url", must_not_add)

    job = await dl.grab_release(db, cw, FakeScored(), user_id=1)
    assert job.status == "deferred" and job.not_before is not None and job.nzo_id is None
    db.close()


@pytest.mark.asyncio
async def test_grab_prefers_uncapped_alternative(monkeypatch):
    """When the top listing is capped but another candidate isn't, grab the alternative now."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    _seed_grabs(db, "guid:capped", 2, ages_h=[1, 2])
    cands = [
        {"key": "guid:capped", "download_url": "u1", "title": "capped", "fmt": "epub"},
        {"key": "guid:free", "download_url": "u2", "title": "free", "fmt": "epub"},
    ]
    added = {"url": None}

    async def fake_add(self, url, *, category=None, nzbname=None, priority=None):
        added["url"] = url
        return {"nzo_ids": ["nzo-free"]}
    monkeypatch.setattr(SABnzbdClient, "add_url", fake_add)

    job = await dl.grab_release(db, cw, candidates=cands, user_id=1)
    assert job.status == "queued" and added["url"] == "u2" and job.attempt == 1
    # the grab was recorded against the uncapped listing
    assert db.scalar(select(UsenetGrab).where(UsenetGrab.release_key == "guid:free")) is not None
    db.close()


@pytest.mark.asyncio
async def test_enqueue_records_ledger(monkeypatch):
    init_db(); db = SessionLocal(); cw = _setup(db)

    async def fake_add(self, url, *, category=None, nzbname=None, priority=None):
        return {"nzo_ids": ["nzo-1"]}
    monkeypatch.setattr(SABnzbdClient, "add_url", fake_add)

    job = await dl.grab_release(db, cw, FakeScored(), user_id=1)
    key = dl._candidate_from_scored(FakeScored())["key"]
    rows = db.scalars(select(UsenetGrab).where(UsenetGrab.release_key == key)).all()
    assert len(rows) == 1 and rows[0].nzo_id == job.nzo_id
    db.close()


@pytest.mark.asyncio
async def test_deferred_job_resumes_when_window_passes(monkeypatch):
    init_db(); db = SessionLocal(); cw = _setup(db)
    _stage_sab(db, library=None)  # category/config present
    # A deferred job whose not_before is already in the past.
    job = DownloadJob(catalog_work_id=cw.id, title="Project Hail Mary", status="deferred",
                      not_before=datetime.now(UTC) - timedelta(minutes=1), attempt=0,
                      candidates=[{"key": "guid:r", "download_url": "u", "title": "r", "fmt": "epub"}])
    db.add(job); db.commit(); db.refresh(job)

    async def q_empty(self, *, limit=100, start=0, category=None):
        return []
    async def h_empty(self, *, limit=100, category=None):
        return []
    async def fake_add(self, url, *, category=None, nzbname=None, priority=None):
        return {"nzo_ids": ["nzo-resumed"]}
    monkeypatch.setattr(SABnzbdClient, "queue", q_empty)
    monkeypatch.setattr(SABnzbdClient, "history", h_empty)
    monkeypatch.setattr(SABnzbdClient, "add_url", fake_add)

    out = await dl.poll_tick(db); db.refresh(job)
    assert out.get("resumed") == 1
    assert job.status in ("queued", "downloading") and job.nzo_id == "nzo-resumed"
    assert job.not_before is None
    db.close()


@pytest.mark.asyncio
async def test_deferred_job_dedups_rerequest(monkeypatch):
    """A deferred (capped) grab must block a re-request from spawning a second primary, and a
    different user piggybacks onto it as a deferred follower — so the cap isn't defeated."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    key = dl._candidate_from_scored(FakeScored())["key"]
    _seed_grabs(db, key, 2, ages_h=[1, 2])

    async def must_not_add(self, url, **k):
        raise AssertionError("capped listing must not enqueue")
    monkeypatch.setattr(SABnzbdClient, "add_url", must_not_add)

    j1 = await dl.grab_release(db, cw, FakeScored(), user_id=1)
    assert j1.status == "deferred"
    j2 = await dl.grab_release(db, cw, FakeScored(), user_id=1)   # same user re-requests
    assert j2.id == j1.id                                          # same job, no duplicate
    j3 = await dl.grab_release(db, cw, FakeScored(), user_id=2)   # different user
    assert j3.id != j1.id and j3.status == "deferred" and j3.candidates is None  # deferred follower
    primaries = db.scalars(select(DownloadJob).where(DownloadJob.candidates.is_not(None))).all()
    assert len(primaries) == 1                                     # still exactly one primary
    db.close()


@pytest.mark.asyncio
async def test_resume_exhausted_fails_followers(monkeypatch):
    """If a deferred primary can't grab anything on resume, its deferred followers fail too
    (not stranded forever)."""
    init_db(); db = SessionLocal(); cw = _setup(db)
    _stage_sab(db, library=None)
    primary = DownloadJob(catalog_work_id=cw.id, user_id=1, title="Project Hail Mary",
                          status="deferred", not_before=datetime.now(UTC) - timedelta(minutes=1),
                          attempt=0, candidates=[{"key": "k", "title": "x"}])  # no download_url
    follower = DownloadJob(catalog_work_id=cw.id, user_id=2, title="Project Hail Mary",
                           status="deferred", not_before=datetime.now(UTC) - timedelta(minutes=1))
    db.add_all([primary, follower]); db.commit(); db.refresh(primary); db.refresh(follower)

    async def q_empty(self, *, limit=100, start=0, category=None):
        return []
    async def h_empty(self, *, limit=100, category=None):
        return []
    monkeypatch.setattr(SABnzbdClient, "queue", q_empty)
    monkeypatch.setattr(SABnzbdClient, "history", h_empty)
    await dl.poll_tick(db); db.refresh(primary); db.refresh(follower)
    assert primary.status == "failed" and follower.status == "failed"
    db.close()


def test_cleanup_jobs_prunes_old_terminal_only():
    """Cleanup prunes finished (imported/failed) fetch jobs past retention, keeping recent ones and
    anything still in flight."""
    from app.ingestion.downloads import cleanup_jobs
    from app.models import DownloadJob
    init_db(); db = SessionLocal()
    db.execute(delete(DownloadJob)); db.commit()
    now = datetime.now(UTC)
    db.add_all([
        DownloadJob(title="old-ok", status="imported", grab_kind="stock",
                    completed_at=now - timedelta(days=30)),
        DownloadJob(title="old-fail", status="failed", grab_kind="stock",
                    created_at=now - timedelta(days=30)),            # no completed_at → uses created_at
        DownloadJob(title="recent-ok", status="imported", grab_kind="stock",
                    completed_at=now - timedelta(days=1)),
        DownloadJob(title="still-downloading", status="downloading", grab_kind="stock",
                    created_at=now - timedelta(days=30)),            # in-flight → never pruned
    ])
    db.commit()
    out = cleanup_jobs(db, retention=timedelta(days=14))
    assert out["pruned"] == 2
    remaining = {j.title for j in db.scalars(select(DownloadJob)).all()}
    assert remaining == {"recent-ok", "still-downloading"}
    db.close()
