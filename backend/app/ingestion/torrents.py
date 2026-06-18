"""Torrent acquisition: Prowlarr torrent-indexer search → qBittorrent → verify → import.

Mirrors the SABnzbd/usenet path but for torrents, and deliberately reuses the heavy lifting:
  * release_matcher  — find + score + gate candidate releases (R22: a torrent name alone never
                       authorizes an import; the same scoring/junk/boxset gates apply);
  * QBittorrentClient — the download backend (add paused → keep only the book files → resume);
  * downloads._import_completed — verify (embedded metadata + ISBN) → promote → import → link →
                       notify → ledger. It reads all config off the integration we pass, so handing
                       it the qBittorrent Integration just works.

Torrent state is kept OUT of downloads.poll_tick (which excludes grab_kind in {libgen, torrent}); this
module owns the grab_kind="torrent" jobs via its own ``torrent_poll_tick``.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..integrations import IntegrationError
from ..integrations.qbittorrent import QBittorrentClient, is_complete, magnet_hash
from ..models import CatalogWork, DownloadJob, Integration
from . import downloads, ledger
from . import release_matcher as rm

log = logging.getLogger("shelf.torrents")

GRAB_KIND = "torrent"
_GRAB_CASCADE = 4       # ranked candidates to try until one registers in qBittorrent (dead .torrents)
_REGISTER_POLLS = 12    # seconds to wait for qBit to fetch+register a .torrent before giving up on it
_MAX_AGE_MIN = 720      # hard cap: abandon a torrent stuck part-downloaded after 12h (failsafe)
# Serialize the add+hash-resolve cascade so two concurrent grabs into the same category can't
# cross-attribute each other's torrent via the before/after hash diff.
_grab_lock = asyncio.Lock()
# Importable book extensions — used to keep only the book file(s) of a multi-file torrent (pack). Same
# set the malware gate scans (everything verify can import) so no kept file goes unscanned.
from .verify import _BOOK_EXTS as _VERIFY_BOOK_EXTS  # noqa: E402
_BOOK_EXTS = tuple(set(_VERIFY_BOOK_EXTS) | {".md"})


def get_qbittorrent(db: Session) -> Integration | None:
    return db.scalar(select(Integration).where(
        Integration.kind == "qbittorrent", Integration.enabled.is_(True)))


def _prowlarr_enabled(db: Session) -> bool:
    return db.scalar(select(Integration.id).where(
        Integration.kind == "prowlarr", Integration.enabled.is_(True))) is not None


def configured(db: Session) -> bool:
    """The torrent route is available when qBittorrent AND a Prowlarr (for torrent search) are on."""
    return get_qbittorrent(db) is not None and _prowlarr_enabled(db)


def _category(qb: Integration) -> str:
    return ((qb.config or {}).get("category") or "shelf").strip() or "shelf"


def _save_path(qb: Integration) -> str | None:
    """Where qBittorrent should download to — MUST be a path on the shared filesystem that Shelf can
    also read (a category save-path is ignored for manually-added torrents, so we pass it explicitly).
    None falls back to qBittorrent's own default (only correct if that default is already shared)."""
    return ((qb.config or {}).get("save_path") or "").strip() or None


def _keep_after_import(qb: Integration) -> bool:
    """When True, leave the torrent in qBittorrent after import (operator seeds/manages it manually).
    Default False: the book file is promoted (moved) into the library, so we delete the torrent + its
    data — seeding the moved-out file isn't possible without a copy-not-move import."""
    return bool((qb.config or {}).get("keep_after_import"))


def _book_file_ids(files: list[dict]) -> tuple[list[int], int]:
    """(ids of the importable book files, total file count). Falls back to enumerate position when the
    qBittorrent file object carries no explicit ``index``."""
    book_ids: list[int] = []
    for pos, f in enumerate(files):
        fid = f.get("index", pos)
        if (f.get("name") or "").lower().endswith(_BOOK_EXTS):
            book_ids.append(int(fid))
    return book_ids, len(files)


def _client(qb: Integration) -> QBittorrentClient:
    return QBittorrentClient(qb.base_url, qb.api_key, kind="qbittorrent", config=qb.config)


async def grab(db: Session, cw: CatalogWork, *, user_id: int | None = None,
               shelf_id: int | None = None, context: dict | None = None) -> DownloadJob | None:
    """Find the best TORRENT release for `cw` and add it to qBittorrent (selective book-file download),
    recording a grab_kind='torrent' DownloadJob. Returns None when no torrent candidate cleared the
    matcher (a NO-RESULT, not an error). Idempotent per (book, user): an in-flight grab is reused."""
    if cw.hooked_work_id:
        raise IntegrationError("this title is already in the library")
    qb = get_qbittorrent(db)
    if qb is None:
        raise IntegrationError("no qBittorrent downloader is configured")

    # Dedup across the whole title cluster (same norm_key), like the usenet path.
    member_ids = list(db.scalars(select(CatalogWork.id).where(
        CatalogWork.norm_key == cw.norm_key))) if cw.norm_key else [cw.id]
    active = db.scalars(select(DownloadJob).where(
        DownloadJob.catalog_work_id.in_(member_ids), DownloadJob.grab_kind == GRAB_KIND,
        DownloadJob.status.in_(downloads.ACTIVE_STATUSES))).all()
    for j in active:
        if j.user_id == user_id:
            return j

    # R22: the SAME matching stack as usenet/AA — score every torrent release, gate, rank.
    ranked = await rm.find_releases(db, cw, context=context, protocols=("torrent",))
    cands = [c for c in rm.candidate_dicts(ranked, cap=downloads.CANDIDATE_CAP)
             if c.get("download_url")]
    if not cands:
        return None

    cat = _category(qb)
    client = _client(qb)
    # Persist the job BEFORE adding to qBit so a failure can't leave an untracked torrent running.
    job = DownloadJob(
        catalog_work_id=cw.id, user_id=user_id, target_shelf_id=shelf_id, title=cw.title,
        sab_category=cat, status="queued", grab_kind=GRAB_KIND, candidates=cands, attempt=0,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Cascade: try the ranked candidates until one actually REGISTERS in qBittorrent. A top-ranked
    # release whose .torrent URL is dead must not fail the grab — qBit fetches a .torrent lazily and
    # silently drops a bad one, so "added Ok." doesn't mean it took. Fall through to the next release.
    h = None
    chosen: dict | None = None
    # Serialized: the before/after category diff that resolves a .torrent's hash would otherwise let
    # two concurrent grabs into the same category cross-attribute each other's torrent.
    async with _grab_lock:
        pre = {t.hash for t in await client.torrents_info(category=cat)}
        for cand in cands[:_GRAB_CASCADE]:
            try:
                h = await _add_and_resolve(client, cat, qb, cand["download_url"])
            except IntegrationError as exc:
                log.info("torrent add failed for %r: %s", cand.get("title"), exc)
                h = None
            if h:
                chosen = cand
                break
        # Remove every torrent THIS grab added to qBit except the chosen one: dead candidates that never
        # resolved (and any that registered late) must not linger as orphans. Inside the lock, all new
        # torrents in the category are ours, so this can't touch a concurrent grab's torrent.
        try:
            for t in await client.torrents_info(category=cat):
                if t.hash not in pre and t.hash != h:
                    await client.delete(t.hash, delete_files=True)
        except IntegrationError as exc:
            log.info("torrent grab orphan cleanup failed (non-fatal): %s", exc)
    if not h or chosen is None:
        job.status = "failed"
        job.error = "no torrent candidate registered in qBittorrent"
        db.commit()
        raise IntegrationError(job.error)

    try:
        # Selective download: for a multi-file pack, keep only the book file(s).
        files = await client.torrent_files(h)
        book_ids, total = _book_file_ids(files)
        if book_ids and total and len(book_ids) < total:
            drop = [int(f.get("index", pos)) for pos, f in enumerate(files)
                    if int(f.get("index", pos)) not in set(book_ids)]
            await client.set_file_priority(h, drop, 0)
        await client.resume(h)
        job.nzo_id = h
        job.release_title = chosen.get("title")
        job.release_key = chosen.get("key")
        job.indexer = chosen.get("indexer")
        job.size = int(chosen.get("size") or 0)
        job.fmt = chosen.get("fmt")
        job.status = "downloading"
        db.commit()
        log.info("torrent grab: %r → qBit %s (cat=%s, tried %d cand(s))", job.title, h, cat, len(cands))
    except IntegrationError as exc:
        job.status = "failed"
        job.error = f"torrent grab failed: {exc}"
        db.commit()
        raise
    return job


async def _add_and_resolve(client: QBittorrentClient, cat: str, qb: Integration,
                           url: str) -> str | None:
    """Add one torrent (paused) and return its hash, or None if qBittorrent never registers it (a dead
    .torrent URL). A magnet carries the hash directly; a .torrent URL must be fetched + parsed by qBit
    first, so poll the category for the newly-present torrent."""
    before = {t.hash for t in await client.torrents_info(category=cat)}
    await client.add_torrent(url, category=cat, savepath=_save_path(qb), paused=True)
    h = magnet_hash(url)
    if h:
        return h
    for _ in range(_REGISTER_POLLS):
        await asyncio.sleep(1)
        fresh = [t for t in await client.torrents_info(category=cat) if t.hash not in before]
        if fresh:
            return fresh[0].hash
    return None


async def _finish(db: Session, client: QBittorrentClient, qb: Integration,
                  job: DownloadJob, t) -> str:
    """A completed torrent: stamp its path and run the shared verify→import. Applies the keep/delete
    policy. Returns the import verdict ('imported' | 'retry' | 'failed' | 'wait')."""
    job.storage_path = t.content_path or t.save_path
    db.commit()
    # NOTE (Batch G): the VirusTotal scan gate is inserted HERE — between completion and import —
    # so an infected file is deleted before it can ever enter the library.
    from . import torrent_scan
    blocked = await torrent_scan.scan_gate(db, job, qb)
    if blocked:
        await _remove(client, job, delete_files=True)
        return "failed"
    # Off the event loop: _import_completed does os.walk + full-file reads + zip/pdf parsing + an
    # ebook-convert subprocess (same reason the SAB poller wraps it in to_thread).
    verdict = await asyncio.to_thread(downloads._import_completed, db, job, qb)
    if verdict == "imported":
        await _remove(client, job, delete_files=not _keep_after_import(qb))
    elif verdict in ("retry", "failed"):
        # ponytail: torrents don't cascade to a next candidate (rare to have several healthy ones);
        # mark the release broken + ledger, and remove the bad download. Upgrade path: reuse the
        # usenet multi-candidate cascade if torrent precision proves to need it.
        from . import broken
        broken.mark_broken(db, (job.candidates or [{}])[0], reason="verify")
        cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
        if cw is not None:
            ledger.mark_unavailable(db, cw, reason="unverified", provider="torrent")
        job.status = "failed"
        db.commit()
        await _remove(client, job, delete_files=True)
    return verdict


async def _remove(client: QBittorrentClient, job: DownloadJob, *, delete_files: bool) -> None:
    if not job.nzo_id:
        return
    try:
        await client.delete(job.nzo_id, delete_files=delete_files)
    except IntegrationError as exc:
        log.info("torrent cleanup for %s failed (non-fatal): %s", job.nzo_id, exc)


async def torrent_poll_tick(db: Session) -> dict:
    """Advance active torrent grabs: reconcile against qBittorrent and import completions. Mirrors
    downloads.poll_tick but for grab_kind='torrent'."""
    jobs = db.scalars(select(DownloadJob).where(
        DownloadJob.status.in_(downloads.ACTIVE_STATUSES),
        DownloadJob.grab_kind == GRAB_KIND)).all()
    if not jobs:
        return {"active": 0}
    qb = get_qbittorrent(db)
    if qb is None:
        return {"active": len(jobs), "error": "no qbittorrent"}
    client = _client(qb)
    try:
        infos = {t.hash: t for t in await client.torrents_info(category=_category(qb))}
    except IntegrationError as exc:
        log.info("torrent poll: qBittorrent unreachable: %s", exc)
        return {"active": len(jobs), "error": str(exc)}

    imported = failed = 0
    for job in jobs:
        t = infos.get((job.nzo_id or "").lower())
        if t is None:
            job.status = "failed"
            job.error = "qBittorrent no longer tracks this torrent"
            db.commit()
            failed += 1
            continue
        if not (is_complete(t.state) or t.progress >= 1.0):
            # Failsafe: a dead/errored/wedged torrent must not block the (first-priority) title — fail
            # it + mark the release broken so the NEXT acquire attempt skips it and cascades to usenet /
            # Anna's. Triggered by an error state, OR no progress past the stall window, OR a hard age
            # cap (a torrent stuck part-downloaded). Prowlarr seeder counts are often stale, so many
            # "seeded" torrents never actually connect — the window keeps fall-through reasonably fast.
            stall_min = float((qb.config or {}).get("stall_minutes", 45) or 45)
            age_min = (downloads._utcnow() - downloads._aware(job.created_at)).total_seconds() / 60
            errored = t.state in ("error", "missingFiles")
            stalled = t.progress < 0.01 and age_min > stall_min
            too_old = age_min > _MAX_AGE_MIN
            if errored or stalled or too_old:
                why = ("error state" if errored
                       else f"no progress in {stall_min:g}m" if stalled else "exceeded max age")
                from . import broken
                broken.mark_broken(db, (job.candidates or [{}])[0], reason=f"torrent {why}")
                cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
                if cw is not None:
                    ledger.mark_unavailable(db, cw, reason="all_broken", provider="torrent")
                job.status = "failed"
                job.error = f"torrent abandoned — {why} (state={t.state})"
                db.commit()
                await _remove(client, job, delete_files=True)
                failed += 1
                continue
            if job.status != "downloading":
                job.status = "downloading"
                db.commit()
            continue
        verdict = await _finish(db, client, qb, job, t)
        if verdict == "imported":
            imported += 1
        elif verdict in ("retry", "failed"):
            failed += 1
    await _reap_orphans(db, client, qb, infos)
    return {"active": len(jobs), "imported": imported, "failed": failed}


async def _reap_orphans(db: Session, client: QBittorrentClient, qb: Integration, infos: dict) -> None:
    """Delete Shelf-category torrents that no active torrent job tracks — e.g. a cascade candidate that
    registered AFTER we moved on. keep_after_import survivors have no active job either, so only sweep
    when keep_after_import is off (the default); otherwise leave them for the operator."""
    if _keep_after_import(qb):
        return
    rows = db.scalars(select(DownloadJob.nzo_id).where(
        DownloadJob.grab_kind == GRAB_KIND,
        DownloadJob.status.in_(downloads.ACTIVE_STATUSES))).all()
    tracked = {(h or "").lower() for h in rows if h}
    for h, t in infos.items():
        if h not in tracked:
            try:
                await client.delete(h, delete_files=True)
                log.info("torrent reaper: removed orphan %s (%r)", h[:12], t.name[:40])
            except IntegrationError:
                pass


def _demo() -> None:
    """Self-check: book-file selection picks only importable files in a multi-file torrent."""
    files = [
        {"index": 0, "name": "Some.Book/cover.jpg"},
        {"index": 1, "name": "Some.Book/book.epub"},
        {"index": 2, "name": "Some.Book/readme.txt"},
        {"index": 3, "name": "Some.Book/sample.pdf"},
    ]
    ids, total = _book_file_ids(files)
    assert total == 4 and ids == [1, 2, 3], (ids, total)   # epub + txt + pdf, not the jpg
    assert _book_file_ids([{"name": "x.jpg"}, {"name": "y.mp3"}]) == ([], 2)  # no book → keep all
    print("torrents self-check ok")


if __name__ == "__main__":
    _demo()
