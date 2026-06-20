"""Torrent route (Batch F) + VirusTotal gate (Batch G): the grab path, the qBittorrent poll/import,
and the malware gate — all with mocked qBittorrent / VirusTotal / matcher (no network)."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.ingestion import torrent_scan, torrents
from app.integrations.qbittorrent import TorrentInfo
from app.models import (
    CatalogWork,
    ContentRequest,
    DownloadJob,
    Integration,
    VtSubmission,
    Work,
    WorkSourceSearch,
)


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for m in (WorkSourceSearch, ContentRequest, DownloadJob, CatalogWork, Integration,
              VtSubmission, Work):
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
    verdict = await torrent_scan.scan_gate(db, job, qb)
    assert verdict == "block"
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
    assert await torrent_scan.scan_gate(db, job, qb) == "block"
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
    assert await torrent_scan.scan_gate(db, job, qb) == "allow"
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
    assert await torrent_scan.scan_gate(db, job, qb) == "allow"
    # vt_block_unknown=True: unknown is held (blocked)
    vt.config = {"vt_block_unknown": True}; db.commit()
    assert await torrent_scan.scan_gate(db, job, qb) == "block"
    db.close()


@pytest.mark.asyncio
async def test_vt_gate_parks_on_rate_limit(tmp_path, monkeypatch):
    """A VirusTotal outage / rate-limit must NOT fail-open: the gate PARKS (hard gate), so an
    unscanned torrent is never imported during an outage."""
    from app.integrations.virustotal import VTUnavailable
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db); _vt(db)
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                      status="downloading", storage_path=_staging_with_book(tmp_path))
    db.add(job); db.commit(); db.refresh(job)

    class _ErrVT:
        def __init__(self, *a, **k): pass
        async def lookup(self, sha256):
            raise VTUnavailable("virustotal: HTTP 429 rate limited")
    monkeypatch.setattr(torrent_scan, "VirusTotalClient", _ErrVT)
    assert await torrent_scan.scan_gate(db, job, qb) == "park"   # parked, NOT allowed
    db.close()


def test_vtunavailable_mapping():
    """lookup maps 429/503/connection → VTUnavailable (park); 404 → None; 401/other → hard
    IntegrationError (never park). Lookup-only: no upload/analysis path is ever taken."""
    import asyncio

    from app.integrations import IntegrationError
    from app.integrations.virustotal import VTUnavailable, VirusTotalClient

    c = VirusTotalClient("k")

    async def run(raise_msg):
        async def fake_get(*a, **k):
            raise IntegrationError(raise_msg)
        c._get = fake_get  # type: ignore[method-assign]
        return await c.lookup("a" * 64)

    # 404 → unknown (None)
    assert asyncio.run(run("virustotal: HTTP 404 from /x: not found")) is None
    # transient → VTUnavailable (park)
    for msg in ("virustotal: HTTP 429 from /x", "virustotal: HTTP 503 from /x",
                "virustotal: cannot reach https://www.virustotal.com (timeout)"):
        with pytest.raises(VTUnavailable):
            asyncio.run(run(msg))
    # 401 / other → HARD error (plain IntegrationError, NOT VTUnavailable → never parks)
    with pytest.raises(IntegrationError) as ei:
        asyncio.run(run("virustotal: unauthorized — check the API key"))
    assert not isinstance(ei.value, VTUnavailable)


@pytest.mark.asyncio
async def test_vt_gate_day_cap_throttles(tmp_path, monkeypatch):
    """The durable per-day quota parks pre-flight (before hashing) once the ledger hits the cap."""
    from app.models import VtSubmission
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db)
    _vt(db, vt_per_day=2, vt_per_min=999)   # tiny day cap, min cap out of the way
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                      status="downloading", storage_path=_staging_with_book(tmp_path))
    db.add(job)
    for _ in range(2):                       # fill the day quota
        db.add(VtSubmission())
    db.commit(); db.refresh(job)
    _FakeVT.stats = {"malicious": 0, "suspicious": 0}
    monkeypatch.setattr(torrent_scan, "VirusTotalClient", _FakeVT)
    assert await torrent_scan.scan_gate(db, job, qb) == "park"   # over the day cap → park, no lookup
    db.close()


@pytest.mark.asyncio
async def test_vt_gate_ledger_records_one_per_lookup(tmp_path, monkeypatch):
    """A successful lookup records exactly ONE VtSubmission; a raise records NONE; a 2-file torrent
    records TWO. (Durable quota accounting.)"""
    from sqlalchemy import func, select as _select

    from app.integrations.virustotal import VTUnavailable
    from app.models import VtSubmission
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db); _vt(db)

    def _count():
        return db.scalar(_select(func.count(VtSubmission.id)))

    # one clean file → one row
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                      status="downloading", storage_path=_staging_with_book(tmp_path))
    db.add(job); db.commit(); db.refresh(job)
    _FakeVT.stats = {"malicious": 0, "suspicious": 0}
    monkeypatch.setattr(torrent_scan, "VirusTotalClient", _FakeVT)
    assert await torrent_scan.scan_gate(db, job, qb) == "allow"
    assert _count() == 1

    # a raise records NOTHING
    db.execute(delete(VtSubmission)); db.commit()
    d2 = tmp_path / "T2"; d2.mkdir(); (d2 / "b.epub").write_bytes(b"x")
    job2 = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                       status="downloading", storage_path=str(d2))
    db.add(job2); db.commit(); db.refresh(job2)

    class _ErrVT:
        def __init__(self, *a, **k): pass
        async def lookup(self, sha256): raise VTUnavailable("HTTP 429")
    monkeypatch.setattr(torrent_scan, "VirusTotalClient", _ErrVT)
    assert await torrent_scan.scan_gate(db, job2, qb) == "park"
    assert _count() == 0

    # a 2-file torrent → two rows
    db.execute(delete(VtSubmission)); db.commit()
    d3 = tmp_path / "T3"; d3.mkdir()
    (d3 / "a.epub").write_bytes(b"a"); (d3 / "b.epub").write_bytes(b"bb")
    job3 = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                       status="downloading", storage_path=str(d3))
    db.add(job3); db.commit(); db.refresh(job3)
    _FakeVT.stats = {"malicious": 0, "suspicious": 0}
    monkeypatch.setattr(torrent_scan, "VirusTotalClient", _FakeVT)
    assert await torrent_scan.scan_gate(db, job3, qb) == "allow"
    assert _count() == 2
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
async def test_poll_abandon_marks_torrent_source_exhausted(monkeypatch):
    """Wave B additive: abandoning a dead torrent sets the per-(work, torrent) source row TERMINAL
    ('exhausted'), alongside the existing title-level mark_unavailable."""
    from datetime import UTC, datetime, timedelta
    from app.ingestion import ledger, source_state
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db)
    req = ledger._upsert(db, cw)
    source_state.ensure_rows(db, req, ["torrent"])
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent", status="downloading",
                      nzo_id="dead2", sab_category="shelf", candidates=[{"title": "rel", "key": "k7"}])
    db.add(job); db.commit()
    job.created_at = datetime.now(UTC) - timedelta(hours=5)
    db.commit(); db.refresh(job)
    fake = _FakeQB()
    fake.torrents["dead2"] = TorrentInfo(hash="dead2", name="rel", state="stalledDL", progress=0.0,
                                         category="shelf", save_path="/dl", content_path=None, size=1)
    monkeypatch.setattr(torrents, "_client", lambda qb: fake)
    await torrents.torrent_poll_tick(db)
    db.refresh(job)
    assert job.status == "failed"
    row = db.scalar(__import__("sqlalchemy").select(WorkSourceSearch).where(
        WorkSourceSearch.content_request_id == req.id, WorkSourceSearch.source == "torrent"))
    assert row.status == "exhausted"
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
        return "block"
    monkeypatch.setattr(torrent_scan, "scan_gate", malicious)
    # import_completed must NEVER be reached for a blocked file.
    def _boom(*a, **k):
        raise AssertionError("import must not run on a blocked file")
    monkeypatch.setattr(torrents.import_core, "import_completed", _boom)

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
    assert await torrent_scan.scan_gate(db, job, qb) == "allow"  # no VT integration → no-op
    db.close()


# --------------------------------------------------------------- grab + poll/import
class _FakeQB:
    """A scripted qBittorrent: add registers a torrent under our category; info/files/delete behave."""
    def __init__(self, *a, **k):
        self.torrents: dict[str, TorrentInfo] = {}
        self.deleted: list[str] = []
        self.resumed: list[str] = []
        self.paused: list[str] = []

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

    async def pause(self, h):
        self.paused.append(h)

    async def delete(self, h, *, delete_files=False):
        self.deleted.append(h)
        self.torrents.pop(h, None)


@pytest.mark.asyncio
async def test_grab_creates_torrent_job(monkeypatch):
    db = SessionLocal()
    cw = _cw(db); _qb(db)
    fake = _FakeQB()
    monkeypatch.setattr(torrents, "_client", lambda qb: fake)

    async def fake_find(db, cw, *, context=None, protocols=None, variant="ebook"):
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
    async def fake_find(db, cw, *, context=None, protocols=None, variant="ebook"): return ["s"]
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
    async def fake_find(db, cw, *, context=None, protocols=None, variant="ebook"):
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
    monkeypatch.setattr(torrents.import_core, "import_completed", lambda db, job, integ: "imported")

    async def no_block(db, job, qb):
        return "allow"
    monkeypatch.setattr(torrent_scan, "scan_gate", no_block)

    res = await torrents.torrent_poll_tick(db)
    assert res["imported"] == 1
    assert "h1" in fake.deleted   # default keep_after_import=False → torrent removed post-import
    db.close()


# --------------------------------------------------------------- VT hard gate: park / resume (Wave C)
def _completed_torrent_job(db, cw, h="hpark"):
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, grab_kind="torrent",
                      status="downloading", nzo_id=h, sab_category="shelf")
    db.add(job); db.commit(); db.refresh(job)
    return job


def _done_info(h="hpark"):
    return TorrentInfo(hash=h, name="rel", state="uploading", progress=1.0,
                       category="shelf", save_path="/dl", content_path="/dl/rel", size=42)


@pytest.mark.asyncio
async def test_poll_parks_not_imports_on_rate_limit(monkeypatch):
    """A completed torrent whose scan PARKS is paused, set vt_pending, and NOT imported."""
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db)
    job = _completed_torrent_job(db, cw)
    fake = _FakeQB(); fake.torrents["hpark"] = _done_info()
    monkeypatch.setattr(torrents, "_client", lambda qb: fake)

    async def park(db, job, qb): return "park"
    monkeypatch.setattr(torrent_scan, "scan_gate", park)
    def _boom(*a, **k): raise AssertionError("import must not run on a parked file")
    monkeypatch.setattr(torrents.import_core, "import_completed", _boom)

    res = await torrents.torrent_poll_tick(db)
    db.refresh(job)
    assert res.get("parked") == 1 and res["imported"] == 0
    assert job.status == "vt_pending" and job.not_before is not None
    assert "hpark" in fake.paused            # pause-on-park asserted
    assert "hpark" not in fake.deleted       # durable: kept, not deleted
    db.close()


@pytest.mark.asyncio
async def test_resume_vt_pending_clean_releases(monkeypatch):
    """A parked torrent whose re-scan now comes back CLEAN is resumed + imported."""
    from datetime import UTC, datetime
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db)
    job = _completed_torrent_job(db, cw)
    job.status = "vt_pending"; job.not_before = datetime.now(UTC)  # due now
    db.commit(); db.refresh(job)
    fake = _FakeQB(); fake.torrents["hpark"] = _done_info()
    monkeypatch.setattr(torrents, "_client", lambda qb: fake)

    async def allow(db, job, qb): return "allow"
    monkeypatch.setattr(torrent_scan, "scan_gate", allow)
    monkeypatch.setattr(torrents.import_core, "import_completed", lambda db, job, integ: "imported")

    res = await torrents.torrent_poll_tick(db)
    db.refresh(job)
    assert res["imported"] == 1
    assert "hpark" in fake.resumed           # paused torrent restarted before import
    db.close()


@pytest.mark.asyncio
async def test_resume_vt_pending_max_park_age_fails_and_deletes(monkeypatch):
    """A torrent parked longer than VT_MAX_PARK is failed + deleted (the drift backstop)."""
    from datetime import UTC, datetime, timedelta
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db)
    job = _completed_torrent_job(db, cw)
    job.status = "vt_pending"; job.not_before = datetime.now(UTC)
    db.commit()
    job.created_at = datetime.now(UTC) - (torrents.VT_MAX_PARK + timedelta(hours=1))
    db.commit(); db.refresh(job)
    fake = _FakeQB(); fake.torrents["hpark"] = _done_info()
    monkeypatch.setattr(torrents, "_client", lambda qb: fake)

    async def park(db, job, qb): raise AssertionError("max-age must fail before re-scanning")
    monkeypatch.setattr(torrent_scan, "scan_gate", park)

    res = await torrents.torrent_poll_tick(db)
    db.refresh(job)
    assert res["failed"] == 1
    assert job.status == "failed" and "VirusTotal" in (job.error or "")
    assert "hpark" in fake.deleted
    db.close()


@pytest.mark.asyncio
async def test_poll_api_429_safety_net_parks(monkeypatch):
    """End-to-end safety net: even with the REAL scan_gate, a lookup raising VTUnavailable (the API's
    own 429) parks the job rather than importing it."""
    from app.integrations.virustotal import VTUnavailable
    db = SessionLocal()
    cw = _cw(db); qb = _qb(db); _vt(db)
    import os as _os
    staging = "/dl/rel"
    # Make the storage path resolve to a real dir with a book so the gate reaches the lookup.
    job = _completed_torrent_job(db, cw)

    fake = _FakeQB()
    info = _done_info()
    monkeypatch.setattr(torrents, "_client", lambda qb: fake)

    class _ErrVT:
        def __init__(self, *a, **k): pass
        async def lookup(self, sha256): raise VTUnavailable("virustotal: HTTP 429")
    monkeypatch.setattr(torrent_scan, "VirusTotalClient", _ErrVT)
    # Point the gate at a staging dir with one book file (so it hashes + looks up → raises → park).
    import tempfile
    d = tempfile.mkdtemp()
    with open(_os.path.join(d, "b.epub"), "wb") as f:
        f.write(b"hello")
    info = TorrentInfo(hash="hpark", name="rel", state="uploading", progress=1.0,
                       category="shelf", save_path=d, content_path=d, size=5)
    fake.torrents["hpark"] = info
    def _boom(*a, **k): raise AssertionError("import must not run when VT is unavailable")
    monkeypatch.setattr(torrents.import_core, "import_completed", _boom)

    res = await torrents.torrent_poll_tick(db)
    db.refresh(job)
    assert res.get("parked") == 1 and res["imported"] == 0
    assert job.status == "vt_pending"
    assert "hpark" in fake.paused
    db.close()
