"""Torrent acquisition: Prowlarr torrent-indexer search → qBittorrent → verify → import.

Mirrors the SABnzbd/usenet path but for torrents, and deliberately reuses the heavy lifting:
  * release_matcher  — find + score + gate candidate releases (R22: a torrent name alone never
                       authorizes an import; the same scoring/junk/boxset gates apply);
  * QBittorrentClient — the download backend (add paused → keep only the book files → resume);
  * import_core.import_completed — verify (embedded metadata + ISBN) → promote → import → link →
                       notify → ledger. It reads all config off the integration we pass, so handing
                       it the qBittorrent Integration just works.

Torrent state is kept OUT of downloads.poll_tick (which excludes grab_kind in {libgen, torrent}); this
module owns the grab_kind="torrent" jobs via its own ``torrent_poll_tick``.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..integrations import IntegrationError
from ..integrations.qbittorrent import QBittorrentClient, is_complete, magnet_hash
from ..models import CatalogWork, DownloadJob, Integration, VtSubmission
from . import import_core, ledger
from . import release_matcher as rm

log = logging.getLogger("shelf.torrents")

GRAB_KIND = "torrent"
_GRAB_CASCADE = 4       # ranked candidates to try until one registers in qBittorrent (dead .torrents)
_REGISTER_POLLS = 12    # seconds to wait for qBit to fetch+register a .torrent before giving up on it
_MAX_AGE_MIN = 720      # hard cap: abandon a torrent stuck part-downloaded after 12h (failsafe)
# How long a torrent may sit parked waiting on VirusTotal quota/outage before we give up: fail +
# delete + notify. The backstop against the local quota ledger drifting from VT's real counter (a
# torrent would otherwise park forever). Re-checked every tick while parked.
VT_MAX_PARK = timedelta(hours=24)
# Re-check spacing for a parked torrent: how far in the future not_before is set when we park (so the
# drain doesn't busy-loop a job the quota check will just re-park).
_VT_PARK_RETRY = timedelta(minutes=5)
# Serialize the add+hash-resolve cascade so two concurrent grabs into the same category can't
# cross-attribute each other's torrent via the before/after hash diff.
_grab_lock = asyncio.Lock()
# Serialize the poll/import tick (scheduled + manual). MANDATORY: the VT resume path resumes a paused
# torrent + imports, so two overlapping ticks could double-import or race a pause against import.
_poll_lock = threading.Lock()
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
               shelf_id: int | None = None, context: dict | None = None,
               variant: str = "ebook") -> DownloadJob | None:
    """Find the best TORRENT release for `cw` and add it to qBittorrent (selective book-file download),
    recording a grab_kind='torrent' DownloadJob. Returns None when no torrent candidate cleared the
    matcher (a NO-RESULT, not an error). Idempotent per (book, user): an in-flight grab is reused."""
    is_audio = variant == "audiobook"
    # An audiobook is a separate Work; a hooked ebook must not block fetching its audiobook.
    if cw.hooked_work_id and not is_audio:
        raise IntegrationError("this title is already in the library")
    qb = get_qbittorrent(db)
    if qb is None:
        raise IntegrationError("no qBittorrent downloader is configured")

    # Dedup across the whole title cluster (same norm_key), like the usenet path — but per-variant, so
    # an audiobook grab and an ebook grab for the same title are independent ('Both' = two jobs).
    # LANGUAGE-scoped too: EN and NO editions are distinct downloads (variant expansion fires one
    # grab per configured language), so a Norwegian grab must not dedup against the English one.
    from . import language as _lang
    if cw.norm_key:
        _rows = db.execute(select(CatalogWork.id, CatalogWork.language)
                           .where(CatalogWork.norm_key == cw.norm_key)).all()
        _want = _lang.bucket(cw.language)
        member_ids = [mid for mid, mlang in _rows if _lang.bucket(mlang) == _want] or [cw.id]
    else:
        member_ids = [cw.id]
    active = db.scalars(select(DownloadJob).where(
        DownloadJob.catalog_work_id.in_(member_ids), DownloadJob.grab_kind == GRAB_KIND,
        DownloadJob.status.in_(import_core.ACTIVE_STATUSES))).all()
    for j in active:
        if j.user_id == user_id and ((j.fmt or "") == "audio") == is_audio:
            return j

    # R22: the SAME matching stack as usenet/AA — score every torrent release, gate, rank.
    ranked = await rm.find_releases(db, cw, context=context, protocols=("torrent",), variant=variant)
    cands = [c for c in rm.candidate_dicts(ranked, cap=import_core.CANDIDATE_CAP)
             if c.get("download_url")]
    if not cands:
        return None

    cat = _category(qb)
    client = _client(qb)
    # Persist the job BEFORE adding to qBit so a failure can't leave an untracked torrent running.
    job = DownloadJob(
        catalog_work_id=cw.id, user_id=user_id, target_shelf_id=shelf_id, title=cw.title,
        sab_category=cat, status="queued", grab_kind=GRAB_KIND, candidates=cands, attempt=0,
        fmt=("audio" if is_audio else None),  # audio marker for import routing
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


async def _fail_park_expired(db: Session, client: QBittorrentClient, job: DownloadJob) -> bool:
    """Backstop: if a torrent has been parked on VT longer than VT_MAX_PARK, fail + delete + notify
    (the local quota ledger may have drifted from VT's real counter, so a parked torrent must not
    wait forever). Returns True when it expired+failed, False when still within the park window."""
    if import_core._utcnow() - import_core._aware(job.created_at) <= VT_MAX_PARK:
        return False
    from .. import notifications as notif
    hours = VT_MAX_PARK.total_seconds() / 3600
    job.status = "failed"
    job.error = f"VirusTotal unavailable for over {hours:.0f}h — gave up scanning"
    db.commit()
    log.warning("VT park expired for job=%s %r — failing + deleting unscanned", job.id, job.title)
    notif.dispatch_soon(db, "ops.download_failed", audience="admin", title="Torrent scan gave up",
                        body=f"{job.title}: {job.error}", level="warn",
                        dedup_key="ops.download_failed")
    await _remove(client, job, delete_files=True)
    return True


async def _park_for_vt(db: Session, client: QBittorrentClient, job: DownloadJob) -> str:
    """Hold a completed-but-unscanned torrent because VirusTotal is over-quota / unavailable: pause
    the torrent (stop it seeding an unscanned file), set status=vt_pending + not_before, and enforce
    the max-park-age backstop. Returns "wait" (parked, re-check later) or "failed" (parked too long →
    the caller's verdict path deletes it). Durability is pause-not-delete: the file stays on qBit's
    save_path; we never delete it while parked."""
    if await _fail_park_expired(db, client, job):
        return "failed"
    # Pause so an unscanned file isn't seeded while we wait. A pause FAILURE still parks (the age
    # clock runs) but is logged loudly — we never import unscanned; the backstop eventually deletes.
    try:
        await client.pause(job.nzo_id)
    except IntegrationError as exc:
        log.warning("VT park: pause FAILED for job=%s %r — unscanned file may seed: %s",
                    job.id, job.title, exc)
    job.status = "vt_pending"
    job.not_before = import_core._utcnow() + _VT_PARK_RETRY
    job.error = "Held — VirusTotal quota reached / unavailable; re-scans when a slot frees."
    db.commit()
    log.info("VT park: job=%s %r held until %s", job.id, job.title, job.not_before)
    return "wait"


async def _finish(db: Session, client: QBittorrentClient, qb: Integration,
                  job: DownloadJob, t, *, skip_gate: bool = False) -> str:
    """A completed torrent: stamp its path and run the shared verify→import. Applies the keep/delete
    policy. Returns the import verdict ('imported' | 'retry' | 'failed' | 'wait').

    ``skip_gate=True``: the VT gate was JUST run by the resume drain and returned "allow" — don't
    re-scan (it would burn a second VT lookup against the 4/min·500/day quota for the same file)."""
    job.storage_path = t.content_path or t.save_path
    db.commit()
    # NOTE (Batch G): the VirusTotal scan gate is inserted HERE — between completion and import —
    # so an infected file is deleted before it can ever enter the library.
    from . import torrent_scan
    verdict = "allow" if skip_gate else await torrent_scan.scan_gate(db, job, qb)  # block|allow|park
    if verdict == "block":
        await _remove(client, job, delete_files=True)
        return "failed"
    if verdict == "park":
        return await _park_for_vt(db, client, job)
    # "allow" → import. Off the event loop: import_completed does os.walk + full-file reads + zip/pdf
    # parsing + an ebook-convert subprocess (same reason the SAB poller wraps it in to_thread).
    verdict = await asyncio.to_thread(import_core.import_completed, db, job, qb)
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
            from . import downloads as _dl
            _dl._record_source_exhausted(db, cw, job, "torrent")
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


async def _resume_vt_pending(db: Session, client: QBittorrentClient, qb: Integration,
                             infos: dict) -> tuple[int, int, int]:
    """Drain torrents parked on VirusTotal: re-check each due vt_pending job's age + quota, then
    re-run the gate. Returns (imported, failed, parked). A genuinely-new torrent resume path — only
    the not_before+due-query PATTERN is shared with the usenet deferred drain (the deferred machinery is
    NOT reused). Re-running scan_gate is idempotent: it re-hashes all files and re-checks quota, so
    block/re-park/allow all resolve correctly on resume."""
    from . import torrent_scan
    now = import_core._utcnow()
    due = db.scalars(select(DownloadJob).where(
        DownloadJob.status == "vt_pending", DownloadJob.grab_kind == GRAB_KIND,
        DownloadJob.not_before.is_not(None), DownloadJob.not_before <= now)).all()
    imported = failed = parked = 0
    for job in due:
        # Max-park-age backstop: fail + delete a torrent parked too long (the ledger-drift guard).
        if await _fail_park_expired(db, client, job):
            failed += 1
            continue
        t = infos.get((job.nzo_id or "").lower())
        if t is None:
            job.status = "failed"
            job.error = "qBittorrent no longer tracks this torrent"
            db.commit()
            failed += 1
            continue
        verdict = await torrent_scan.scan_gate(db, job, qb)   # re-hash + re-check quota (idempotent)
        if verdict == "block":
            await _remove(client, job, delete_files=True)
            failed += 1
            continue
        if verdict == "park":
            # Still over quota / unavailable → re-park (bump not_before). Stays paused already.
            job.not_before = now + _VT_PARK_RETRY
            db.commit()
            parked += 1
            continue
        # "allow": resume the paused torrent so its file is visible, then import.
        try:
            await client.resume(job.nzo_id)
        except IntegrationError as exc:
            log.info("VT resume: failed to start torrent for job=%s (continuing to import): %s",
                     job.id, exc)
        job.status = "downloading"  # back to the normal completed-import path
        db.commit()
        iv = await _finish(db, client, qb, job, t, skip_gate=True)  # already gated above — don't re-scan
        if iv == "imported":
            imported += 1
        elif iv == "wait":
            parked += 1   # re-parked inside _finish (quota still hit)
        elif iv in ("retry", "failed"):
            failed += 1
    return imported, failed, parked


async def torrent_poll_tick(db: Session) -> dict:
    """Advance active torrent grabs: reconcile against qBittorrent and import completions. Mirrors
    downloads.poll_tick but for grab_kind='torrent'. Serialized by _poll_lock — the VT resume path
    resumes paused torrents + imports, so overlapping ticks would race/double-import."""
    if not _poll_lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        return await _torrent_poll_tick(db)
    finally:
        _poll_lock.release()


async def _torrent_poll_tick(db: Session) -> dict:
    # Prune the VT quota ledger well past the day window so it can't grow unbounded (only in-window
    # rows affect the cap; keep a few days' margin).
    from sqlalchemy import delete as _delete

    from . import torrent_scan
    db.execute(_delete(VtSubmission).where(
        VtSubmission.created_at < import_core._utcnow() - 3 * torrent_scan._VT_DAY_WINDOW))
    db.commit()
    jobs = db.scalars(select(DownloadJob).where(
        DownloadJob.status.in_(import_core.ACTIVE_STATUSES),
        DownloadJob.grab_kind == GRAB_KIND)).all()
    # Torrents parked on VT whose re-check time has arrived need draining even when nothing else is
    # active. Skip the qBit round-trip only when there's neither.
    due_parked = db.scalar(select(DownloadJob.id).where(
        DownloadJob.status == "vt_pending", DownloadJob.grab_kind == GRAB_KIND,
        DownloadJob.not_before <= import_core._utcnow()).limit(1))
    if not jobs and not due_parked:
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

    imported = failed = parked = 0
    if due_parked:
        p_imp, p_fail, p_park = await _resume_vt_pending(db, client, qb, infos)
        imported += p_imp
        failed += p_fail
        parked += p_park
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
            age_min = (import_core._utcnow() - import_core._aware(job.created_at)).total_seconds() / 60
            errored = t.state in ("error", "missingFiles")
            # `stalledDL` is qBit's own signal for "in download mode but NO peer/seed is serving data"
            # — a wedge at ANY percent. The old check only looked at progress < 0.01, so a torrent that
            # pulled most of the file and then lost its peers (e.g. stalled at 92%) slipped through and
            # sat until the 12h age cap. Treat either as stalled once past the window.
            stalled = (t.state == "stalledDL" or t.progress < 0.01) and age_min > stall_min
            too_old = age_min > _MAX_AGE_MIN
            if errored or stalled or too_old:
                why = ("error state" if errored
                       else f"no progress in {stall_min:g}m" if stalled else "exceeded max age")
                from . import broken
                broken.mark_broken(db, (job.candidates or [{}])[0], reason=f"torrent {why}")
                cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
                if cw is not None:
                    ledger.mark_unavailable(db, cw, reason="all_broken", provider="torrent")
                    from . import downloads as _dl
                    _dl._record_source_exhausted(db, cw, job, "torrent")
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
        elif verdict == "wait":
            parked += 1   # parked on VT (paused, status=vt_pending) — re-checked by the drain
        elif verdict in ("retry", "failed"):
            failed += 1
    await _reap_orphans(db, client, qb, infos)
    return {"active": len(jobs), "imported": imported, "failed": failed, "parked": parked}


async def _reap_orphans(db: Session, client: QBittorrentClient, qb: Integration, infos: dict) -> None:
    """Delete Shelf-category torrents that no active torrent job tracks — e.g. a cascade candidate that
    registered AFTER we moved on. keep_after_import survivors have no active job either, so only sweep
    when keep_after_import is off (the default); otherwise leave them for the operator."""
    if _keep_after_import(qb):
        return
    # vt_pending jobs are deliberately OUT of ACTIVE_STATUSES (drained only by the not_before query),
    # but their PAUSED torrent must not be reaped as an orphan — track them explicitly here too.
    rows = db.scalars(select(DownloadJob.nzo_id).where(
        DownloadJob.grab_kind == GRAB_KIND,
        DownloadJob.status.in_(import_core.ACTIVE_STATUSES + ("vt_pending",)))).all()
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
