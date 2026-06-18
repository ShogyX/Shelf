"""Torrent route (Batch F) + VirusTotal gate (Batch G): the grab path, the qBittorrent poll/import,
and the malware gate — all with mocked qBittorrent / VirusTotal / matcher (no network)."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.ingestion import torrent_scan, torrents
from app.integrations.qbittorrent import TorrentInfo
from app.models import CatalogWork, DownloadJob, Integration, Work


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for m in (DownloadJob, CatalogWork, Integration, Work):
        db.execute(delete(m))
    db.commit()
    db.close()
    yield


def _cw(db, *, norm="the book", title="The Book", hooked=None):
    cw = CatalogWork(provider="googlebooks", provider_ref="r", domain="d", work_url="u",
                     title=title, author="Auth", media_kind="text", norm_key=norm,
                     hooked_work_id=hooked)
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


def _qb(db, **cfg):
    integ = Integration(kind="qbittorrent", name="qb", base_url="http://qb:8090",
                        api_key="pw", enabled=True, config={"username": "u", **cfg})
    db.add(integ); db.commit(); db.refresh(integ)
    return integ


def _vt(db, **cfg):
    integ = Integration(kind="virustotal", name="vt", base_url="", api_key="k",
                        enabled=True, config=cfg)
    db.add(integ); db.commit(); db.refresh(integ)
    return integ


# --------------------------------------------------------------- matcher: torrent seeder health (R22)
def test_seeded_torrent_outranks_dead_one():
    from app.ingestion import release_matcher as rm
    from app.integrations.prowlarr import Release
    prefs = rm.search_prefs(None)

    def mk(seeders, proto="torrent"):
        return Release(title="Dune by Frank Herbert EPUB", download_url="magnet:x", protocol=proto,
                       size=5_000_000, categories=[7020], seeders=seeders)

    dead = rm.score_release("Dune", "Frank Herbert", "en", mk(0), prefs)
    live = rm.score_release("Dune", "Frank Herbert", "en", mk(50), prefs)
    usenet = rm.score_release("Dune", "Frank Herbert", "en", mk(None, "usenet"), prefs)
    assert live.score > dead.score                 # a seeded torrent always outranks a dead one
    assert dead.accepted and live.accepted          # 0-seeder still accepted (grabbed only if sole option)
    assert usenet.score == pytest.approx(live.score - 0.05)  # usenet (seeders=None) unaffected by seed logic


# --------------------------------------------------------------- book-file selection / config
def test_book_file_ids_picks_only_books():
    files = [{"index": 0, "name": "x/cover.jpg"}, {"index": 1, "name": "x/book.epub"},
             {"index": 2, "name": "x/notes.txt"}]
    assert torrents._book_file_ids(files) == ([1, 2], 3)


def test_configured_requires_both(monkeypatch):
    db = SessionLocal()
    assert torrents.configured(db) is False
    _qb(db)
    assert torrents.configured(db) is False          # qBit alone isn't enough
    db.add(Integration(kind="prowlarr", name="p", base_url="u", api_key="k", enabled=True))
    db.commit()
    assert torrents.configured(db) is True
    db.close()


# --------------------------------------------------------------- VirusTotal gate (security)
class _FakeVT:
    def __init__(self, *a, **k):
        pass

    async def lookup(self, sha256):
        return _FakeVT.stats


def _staging_with_book(tmp_path):
    d = tmp_path / "Some.Book"
    d.mkdir()
    (d / "book.epub").write_bytes(b"hello epub")
    return str(d)


@pytest.mark.asyncio
async def test_vt_gate_blocks_malicious(tmp_path, monkeypatch):
    db = SessionLocal()
    cw = _cw(db)
    qb = _qb(db)
    _vt(db)
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                      status="downloading", storage_path=_staging_with_book(tmp_path))
    db.add(job); db.commit(); db.refresh(job)
    _FakeVT.stats = {"malicious": 7, "suspicious": 0}
    monkeypatch.setattr(torrent_scan, "VirusTotalClient", _FakeVT)
    blocked = await torrent_scan.scan_gate(db, job, qb)
    assert blocked is True
    assert job.status == "failed" and "VirusTotal" in (job.error or "")
    db.close()


@pytest.mark.asyncio
async def test_vt_gate_scans_fb2_djvu(tmp_path, monkeypatch):
    """H1 regression: formats verify can import (.fb2/.djvu) MUST be scanned, not slip past the gate."""
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db); _vt(db)
    d = tmp_path / "Book"; d.mkdir()
    (d / "book.fb2").write_bytes(b"malware payload")     # a format verify imports but the old gate skipped
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                      status="downloading", storage_path=str(d))
    db.add(job); db.commit(); db.refresh(job)
    assert any(p.endswith(".fb2") for p in torrent_scan._book_files(str(d)))   # the gate now collects it
    _FakeVT.stats = {"malicious": 9, "suspicious": 0}
    monkeypatch.setattr(torrent_scan, "VirusTotalClient", _FakeVT)
    assert await torrent_scan.scan_gate(db, job, qb) is True   # → blocked
    db.close()


@pytest.mark.asyncio
async def test_vt_gate_allows_clean(tmp_path, monkeypatch):
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db); _vt(db)
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                      status="downloading", storage_path=_staging_with_book(tmp_path))
    db.add(job); db.commit(); db.refresh(job)
    _FakeVT.stats = {"malicious": 0, "suspicious": 0}
    monkeypatch.setattr(torrent_scan, "VirusTotalClient", _FakeVT)
    assert await torrent_scan.scan_gate(db, job, qb) is False
    db.close()


@pytest.mark.asyncio
async def test_vt_gate_unknown_policy(tmp_path, monkeypatch):
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db)
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                      status="downloading", storage_path=_staging_with_book(tmp_path))
    db.add(job); db.commit(); db.refresh(job)
    _FakeVT.stats = None  # 404 → unknown
    monkeypatch.setattr(torrent_scan, "VirusTotalClient", _FakeVT)
    # default: unknown is allowed
    vt = _vt(db)
    assert await torrent_scan.scan_gate(db, job, qb) is False
    # vt_block_unknown=True: unknown is held (blocked)
    vt.config = {"vt_block_unknown": True}; db.commit()
    assert await torrent_scan.scan_gate(db, job, qb) is True
    db.close()


@pytest.mark.asyncio
async def test_vt_gate_fails_open_on_api_error(tmp_path, monkeypatch):
    """A VirusTotal outage must NOT strand every torrent — an API error fails OPEN (allow)."""
    from app.integrations import IntegrationError
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db); _vt(db)
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                      status="downloading", storage_path=_staging_with_book(tmp_path))
    db.add(job); db.commit(); db.refresh(job)

    class _ErrVT:
        def __init__(self, *a, **k): pass
        async def lookup(self, sha256):
            raise IntegrationError("virustotal: HTTP 429 rate limited")
    monkeypatch.setattr(torrent_scan, "VirusTotalClient", _ErrVT)
    assert await torrent_scan.scan_gate(db, job, qb) is False   # allowed (fail-open)
    db.close()


@pytest.mark.asyncio
async def test_poll_fails_stalled_dead_torrent(monkeypatch):
    """A 0-progress torrent past the stall window is failed + the release marked broken, so the next
    acquire attempt cascades to usenet/Anna's instead of being stuck on a dead (0-seeder) torrent."""
    from datetime import UTC, datetime, timedelta
    from app.ingestion import broken
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db)
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent", status="downloading",
                      nzo_id="dead1", sab_category="shelf", candidates=[{"title": "rel", "key": "k9"}])
    db.add(job); db.commit()
    job.created_at = datetime.now(UTC) - timedelta(hours=5)   # older than the 4h default stall window
    db.commit(); db.refresh(job)
    fake = _FakeQB()
    fake.torrents["dead1"] = TorrentInfo(hash="dead1", name="rel", state="stalledDL", progress=0.0,
                                         category="shelf", save_path="/dl", content_path=None, size=1)
    monkeypatch.setattr(torrents, "_client", lambda qb: fake)
    res = await torrents.torrent_poll_tick(db)
    assert res["failed"] == 1
    db.refresh(job)
    assert job.status == "failed" and "abandoned" in (job.error or "")
    assert "dead1" in fake.deleted
    assert broken.is_broken(db, {"title": "rel", "key": "k9"})   # won't be re-grabbed
    db.close()


@pytest.mark.asyncio
async def test_poll_blocks_and_deletes_malicious(monkeypatch):
    """A malicious completed torrent is deleted (with files) and never imported."""
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db)
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                      status="downloading", nzo_id="bad1", sab_category="shelf")
    db.add(job); db.commit(); db.refresh(job)
    fake = _FakeQB()
    fake.torrents["bad1"] = TorrentInfo(hash="bad1", name="rel", state="uploading", progress=1.0,
                                        category="shelf", save_path="/dl", content_path="/dl/rel", size=1)
    monkeypatch.setattr(torrents, "_client", lambda qb: fake)

    async def malicious(db, job, qb):
        job.status = "failed"; db.commit()
        return True
    monkeypatch.setattr(torrent_scan, "scan_gate", malicious)
    # _import_completed must NEVER be reached for a blocked file.
    def _boom(*a, **k):
        raise AssertionError("import must not run on a blocked file")
    monkeypatch.setattr(torrents.downloads, "_import_completed", _boom)

    res = await torrents.torrent_poll_tick(db)
    assert res["failed"] == 1 and res["imported"] == 0
    assert "bad1" in fake.deleted and "bad1" not in fake.torrents   # deleted with files
    db.close()


@pytest.mark.asyncio
async def test_vt_gate_noop_when_unconfigured(tmp_path):
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db)
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                      status="downloading", storage_path=_staging_with_book(tmp_path))
    db.add(job); db.commit(); db.refresh(job)
    assert await torrent_scan.scan_gate(db, job, qb) is False  # no VT integration → no-op
    db.close()


# --------------------------------------------------------------- grab + poll/import
class _FakeQB:
    """A scripted qBittorrent: add registers a torrent under our category; info/files/delete behave."""
    def __init__(self, *a, **k):
        self.torrents: dict[str, TorrentInfo] = {}
        self.deleted: list[str] = []
        self.resumed: list[str] = []

    async def torrents_info(self, *, category=None, hashes=None):
        vals = list(self.torrents.values())
        if hashes:
            vals = [t for t in vals if t.hash == hashes]
        if category:
            vals = [t for t in vals if t.category == category]
        return vals

    async def add_torrent(self, url, *, category=None, savepath=None, paused=True):
        from app.integrations.qbittorrent import magnet_hash
        h = magnet_hash(url) or ("abc123" + str(len(self.torrents)))   # qBit keys a magnet by infohash
        self.torrents[h] = TorrentInfo(hash=h, name="rel", state="metaDL", progress=0.0,
                                       category=category, save_path="/dl", content_path="/dl/rel",
                                       size=1)

    async def torrent_files(self, h):
        return [{"index": 0, "name": "rel/book.epub"}]

    async def set_file_priority(self, h, ids, prio):
        pass

    async def resume(self, h):
        self.resumed.append(h)

    async def delete(self, h, *, delete_files=False):
        self.deleted.append(h)
        self.torrents.pop(h, None)


@pytest.mark.asyncio
async def test_grab_creates_torrent_job(monkeypatch):
    db = SessionLocal()
    cw = _cw(db); _qb(db)
    fake = _FakeQB()
    monkeypatch.setattr(torrents, "_client", lambda qb: fake)

    async def fake_find(db, cw, *, context=None, protocols=None):
        assert protocols == ("torrent",)   # R22: torrent-protocol search
        return ["scored"]
    monkeypatch.setattr(torrents.rm, "find_releases", fake_find)
    monkeypatch.setattr(torrents.rm, "candidate_dicts",
                        lambda ranked, cap=6: [{"title": "rel", "download_url": "magnet:?xt=urn:btih:" + "a" * 40, "key": "k1"}])

    job = await torrents.grab(db, cw, user_id=1)
    assert job is not None and job.grab_kind == "torrent" and job.status == "downloading"
    assert job.nzo_id and job.nzo_id in fake.resumed     # hash stamped + torrent resumed
    db.close()


@pytest.mark.asyncio
async def test_grab_only_chosen_torrent_survives(monkeypatch):
    """After a grab, the chosen torrent is the ONLY new one in qBit — any other torrent this grab added
    to the category (a dead/late candidate) is swept as an orphan. Here a pre-existing unrelated torrent
    must be preserved, and a non-chosen one added mid-grab must be deleted."""
    db = SessionLocal()
    cw = _cw(db); _qb(db)

    class _SweepQB(_FakeQB):
        async def add_torrent(self, url, *, category=None, savepath=None, paused=True):
            from app.integrations.qbittorrent import magnet_hash
            h = magnet_hash(url)
            self.torrents[h] = TorrentInfo(hash=h, name="rel", state="metaDL", progress=0.0,
                                           category=category, save_path="/dl", content_path="/dl/rel", size=1)
    fake = _SweepQB()
    # pre-existing torrent (another grab's) — must NOT be touched (it's in `pre`).
    fake.torrents["keep0"] = TorrentInfo("keep0", "other", "downloading", 0.3, "shelf", "/dl", None, 1)
    # an orphan already sitting in the category from a late prior add — added "during" grab via this:
    monkeypatch.setattr(torrents, "_client", lambda qb: fake)
    async def fake_find(db, cw, *, context=None, protocols=None): return ["s"]
    monkeypatch.setattr(torrents.rm, "find_releases", fake_find)
    chosen = "magnet:?xt=urn:btih:" + "a" * 40
    monkeypatch.setattr(torrents.rm, "candidate_dicts", lambda ranked, cap=6: [
        {"title": "good", "download_url": chosen, "key": "k1"},
    ])
    # Inject an orphan that appears AFTER `pre` is snapshotted (a late candidate from this grab).
    orig_info = fake.torrents_info
    state = {"injected": False}
    async def info_with_late_orphan(*, category=None, hashes=None):
        if not state["injected"]:
            state["injected"] = True   # first call = `pre` snapshot; inject the orphan right after
            return await orig_info(category=category, hashes=hashes)
        fake.torrents.setdefault("orphan9", TorrentInfo("orphan9", "late", "metaDL", 0.0, "shelf", "/dl", None, 1))
        return await orig_info(category=category, hashes=hashes)
    monkeypatch.setattr(fake, "torrents_info", info_with_late_orphan)

    job = await torrents.grab(db, cw, user_id=1)
    assert job.nzo_id == "a" * 40
    assert "orphan9" in fake.deleted and "orphan9" not in fake.torrents   # late orphan swept
    assert "keep0" not in fake.deleted and "keep0" in fake.torrents       # pre-existing preserved
    db.close()


@pytest.mark.asyncio
async def test_grab_none_when_no_candidates(monkeypatch):
    db = SessionLocal()
    cw = _cw(db); _qb(db)
    monkeypatch.setattr(torrents, "_client", lambda qb: _FakeQB())
    async def fake_find(db, cw, *, context=None, protocols=None):
        return []
    monkeypatch.setattr(torrents.rm, "find_releases", fake_find)
    monkeypatch.setattr(torrents.rm, "candidate_dicts", lambda ranked, cap=6: [])
    assert await torrents.grab(db, cw, user_id=1) is None
    db.close()


@pytest.mark.asyncio
async def test_poll_imports_completed_torrent(monkeypatch):
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db)
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                      status="downloading", nzo_id="h1", sab_category="shelf")
    db.add(job); db.commit(); db.refresh(job)

    fake = _FakeQB()
    fake.torrents["h1"] = TorrentInfo(hash="h1", name="rel", state="uploading", progress=1.0,
                                      category="shelf", save_path="/dl", content_path="/dl/rel", size=1)
    monkeypatch.setattr(torrents, "_client", lambda qb: fake)
    # Stub the shared import + VT gate so we test the poll's orchestration, not the import internals.
    monkeypatch.setattr(torrents.downloads, "_import_completed", lambda db, job, integ: "imported")

    async def no_block(db, job, qb):
        return False
    monkeypatch.setattr(torrent_scan, "scan_gate", no_block)

    res = await torrents.torrent_poll_tick(db)
    assert res["imported"] == 1
    assert "h1" in fake.deleted   # default keep_after_import=False → torrent removed post-import
    db.close()
