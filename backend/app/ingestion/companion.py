"""Push stocked content to companion reading apps (Audiobookshelf, Storyteller).

Audiobookshelf auto-scans the shared stock/audiobook folders, so its "push" is just a scan nudge so
new files appear promptly — Shelf never copies for ABS. Storyteller imports by server-local path and
writes metadata back into the files it imports, so Shelf pushes COPIES into a Storyteller import
directory (converting ebooks to EPUB on demand), then triggers read-along alignment once a title has
BOTH an ebook and an audiobook.

The wanted-PULL (apps → Shelf) lives in :mod:`companion_pull` (Phase 3).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..integrations import IntegrationError, client_for
from ..models import CatalogWork, CompanionPush, Integration, QueuedHook, StockItem, User, Work
from . import convert
from .extract import norm_title

log = logging.getLogger("shelf.companion")

_COMPANION_KINDS = ("audiobookshelf", "storyteller")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _safe(name: str) -> str:
    import re
    s = re.sub(r"[^\w .,'()\-]+", " ", (name or "")).strip()
    s = s[:120] or "book"
    return "book" if s in (".", "..") else s   # never a path-traversal component


async def push_tick() -> dict:
    """Scheduler entrypoint: push stocked content to every enabled companion app (best-effort)."""
    from ..db import SessionLocal

    db = SessionLocal()
    out: dict = {}
    try:
        integs = db.scalars(select(Integration).where(
            Integration.kind.in_(_COMPANION_KINDS), Integration.enabled.is_(True))).all()
        for integ in integs:
            try:
                if integ.kind == "audiobookshelf":
                    out[integ.id] = await _push_abs(db, integ)
                else:
                    out[integ.id] = await _push_storyteller(db, integ)
            except Exception as exc:  # noqa: BLE001 — one app's failure must not stop the others
                log.exception("companion push failed for integration %s", integ.id)
                integ.last_error = str(exc)
                db.commit()
    finally:
        db.close()
    return out


async def _push_abs(db: Session, integ: Integration) -> dict:
    """ABS watches the shared stock/audiobook folders — content appears on its own. Nudge a scan so
    newly-stocked files are ingested promptly; no copying. Warns if Shelf's audiobooks aren't under
    any ABS library folder (then they'd never show up in ABS)."""
    client = client_for(integ)
    libs = await client.book_libraries()
    scanned = 0
    for lib in libs:
        try:
            await client.scan(lib["id"])
            scanned += 1
        except IntegrationError as exc:
            log.info("ABS scan failed for library %s: %s", lib.get("id"), exc)
    warning = _abs_audiobook_coverage_warning(db, libs)
    integ.last_sync_at = _utcnow()
    integ.last_error = warning
    db.commit()
    return {"libraries_scanned": scanned, "warning": warning}


def _abs_audiobook_coverage_warning(db: Session, libs: list[dict]) -> str | None:
    """If Shelf has stocked audiobooks but NO ABS library folder contains them, ABS can't see them —
    surface that as a clear hint (ABS ingests ebooks from the stock folder automatically, but the
    audiobook path is separate and the operator must add an ABS library for it)."""
    audio_paths = db.scalars(select(Work.local_path).where(
        Work.media_kind == "audio", Work.local_path.is_not(None)).limit(50)).all()
    if not audio_paths:
        return None
    folders = [os.path.normpath(f) for lib in libs for f in lib["folders"]]

    def covered(p: str) -> bool:
        np = os.path.normpath(p)
        return any(np == f or np.startswith(f + os.sep) for f in folders)

    if not any(covered(p) for p in audio_paths):
        return ("Stocked audiobooks aren't under any Audiobookshelf library folder — add an ABS "
                "library pointing at Shelf's audiobook path so they appear in Audiobookshelf.")
    return None


def _stocked_works(db: Session) -> dict[str, dict[str, Work]]:
    """Stocked Works grouped by normalized title → {"ebook": Work, "audio": Work} (whichever exist),
    so a title's ebook + audiobook are paired for Storyteller's read-along. Ebooks come from the
    operator's stock (StockItem); audiobooks are the media_kind="audio" Works (StockItem.norm_key is
    UNIQUE, so the two halves can't both be stock items)."""
    groups: dict[str, dict[str, Work]] = {}
    ebook_ids = db.scalars(select(StockItem.work_id).where(
        StockItem.status == "stocked", StockItem.work_id.is_not(None))).all()
    for wid in ebook_ids:
        w = db.get(Work, wid)
        if w and w.local_path and (w.media_kind or "") != "audio":
            groups.setdefault(norm_title(w.title), {})["ebook"] = w
    for w in db.scalars(select(Work).where(
            Work.media_kind == "audio", Work.local_path.is_not(None))).all():
        groups.setdefault(norm_title(w.title), {})["audio"] = w
    return groups


_PUSH_CAP = 25  # per tick: bound how many new books we create on the remote in one pass


async def _push_storyteller(db: Session, integ: Integration) -> dict:
    """Copy each stocked Work (ebook→EPUB on demand) into the Storyteller import dir, create the book,
    and trigger alignment once a title has both halves. Idempotent via CompanionPush."""
    client = client_for(integ)
    import_root = ((integ.config or {}).get("import_path") or "").strip()
    if not import_root:
        integ.last_error = "set the Storyteller import path (the shared folder it reads) in the integration config"
        db.commit()
        return {"error": "no import_path configured"}
    os.makedirs(import_root, exist_ok=True)

    already = {(p.work_id, p.fmt): p for p in db.scalars(
        select(CompanionPush).where(CompanionPush.integration_id == integ.id)).all()}
    created = aligned = 0
    for nk, group in _stocked_works(db).items():
        book_dir = os.path.join(import_root, _safe(next(iter(group.values())).title))
        for fmt, work in group.items():
            if (work.id, fmt) in already or created >= _PUSH_CAP:
                continue
            try:
                # Copy/convert off the event loop — a multi-GB audiobook copy must not block the
                # scheduler (and the API process) for the whole transfer.
                paths = await asyncio.to_thread(_stage_for_storyteller, work, fmt, book_dir)
            except Exception as exc:  # noqa: BLE001 — TRANSIENT (IO error): don't record a blocking
                # CompanionPush, so it retries next tick (a permanent record would poison it forever).
                log.info("storyteller stage failed for work %s (will retry): %s", work.id, exc)
                continue
            if not paths:
                # Nothing stageable (e.g. an audiobook laid out only as nested sub-folders) — record a
                # failed push so it isn't retried forever.
                db.add(CompanionPush(integration_id=integ.id, work_id=work.id, fmt=fmt,
                                     status="failed", error="no stageable file"))
                db.commit()
                already[(work.id, fmt)] = db.scalar(select(CompanionPush).where(
                    CompanionPush.integration_id == integ.id, CompanionPush.work_id == work.id,
                    CompanionPush.fmt == fmt))
                continue
            try:
                res = await client.create_book(paths)
            except IntegrationError as exc:  # TRANSIENT (Storyteller down/timeout): retry next tick
                log.info("storyteller create_book failed for work %s (will retry): %s", work.id, exc)
                continue
            ref = res.get("uuid") or res.get("id") if isinstance(res, dict) else None
            if not ref:  # unexpected API shape — push recorded but can't align (logged for diagnosis)
                log.info("storyteller create_book returned no book id for work %s: %r", work.id, res)
            push = CompanionPush(integration_id=integ.id, work_id=work.id, fmt=fmt,
                                 external_ref=ref, status="pushed")
            db.add(push)
            db.commit()
            already[(work.id, fmt)] = push
            created += 1

    # Trigger alignment ONLY for titles that now have BOTH halves successfully pushed (a one-format
    # book has nothing to sync; aligning it early would mark it done and skip the second half forever).
    for nk, group in _stocked_works(db).items():
        if "ebook" not in group or "audio" not in group:
            continue
        refs = [already.get((group[f].id, f)) for f in ("ebook", "audio")]
        refs = [r for r in refs if r and r.external_ref and r.status == "pushed"]
        if len(refs) != 2:
            continue
        # Both halves SHOULD attach to one Storyteller book (it matches by path+metadata); if the two
        # create_book calls returned different uuids they weren't merged — log it (can't verify live).
        if refs[0].external_ref != refs[1].external_ref:
            log.info("storyteller: ebook/audio created as separate books (%s vs %s) for %r",
                     refs[0].external_ref, refs[1].external_ref, nk)
        try:
            await client.process(refs[0].external_ref)
            for r in refs:
                r.status = "aligned"
            db.commit()
            aligned += 1
        except IntegrationError as exc:
            log.info("storyteller process failed: %s", exc)
    integ.last_sync_at = _utcnow()
    integ.last_error = None
    db.commit()
    return {"created": created, "aligned": aligned}


# ------------------------------------------------------------------ wanted-pull (apps → Shelf)
_PULL_CAP = 10            # per integration per tick — trickle, never flood (an ABS library can have
                          # thousands of single-format items; missing-format = wanted is opt-in too).
_COMIC_EBOOK_FORMATS = {"cbz", "cbr"}


async def pull_tick() -> dict:
    """Scheduler entrypoint: for each companion app with ``pull_wanted`` enabled, queue Shelf fetches
    for items it has in only ONE format (the missing half). Heavily gated (opt-in + per-tick cap +
    must match a Shelf catalog title) so a large library can't flood the acquisition pipeline."""
    from ..db import SessionLocal

    db = SessionLocal()
    out: dict = {}
    try:
        integs = db.scalars(select(Integration).where(
            Integration.kind.in_(_COMPANION_KINDS), Integration.enabled.is_(True))).all()
        for integ in integs:
            if not (integ.config or {}).get("pull_wanted"):
                continue
            try:
                out[integ.id] = (await _pull_abs(db, integ) if integ.kind == "audiobookshelf"
                                 else await _pull_storyteller(db, integ))
            except Exception as exc:  # noqa: BLE001
                log.exception("companion pull failed for integration %s", integ.id)
                integ.last_error = str(exc)
                db.commit()
    finally:
        db.close()
    return out


def _missing_format(has_ebook: bool, has_audio: bool) -> str | None:
    if has_ebook and not has_audio:
        return "audiobook"
    if has_audio and not has_ebook:
        return "ebook"
    return None  # has both, or neither → nothing wanted


def _queue_abs_items(db: Session, integ: Integration, items: list, remaining: int) -> int:
    """Sync hot loop: scan a library's items for the FIRST ``remaining`` missing-format wants. Pure DB
    work (no awaits) — must run OFF the event loop (a big ABS library has thousands of items, each
    hitting the DB, and iterating them inline froze the loop for ~30s, stalling the reader + every
    tick). Stops as soon as the cap is reached."""
    queued = 0
    for item in items:
        if queued >= remaining:
            break
        want = _missing_format(item["has_ebook"], item["has_audio"])
        if want is None:
            continue
        # Comics have no audiobook; never want one (and an audio-only comic is nonsense).
        if (item.get("ebook_format") or "").lower() in _COMIC_EBOOK_FORMATS:
            continue
        if _queue_want(db, integ, item["title"], item["author"], want):
            queued += 1
    return queued


async def _pull_abs(db: Session, integ: Integration) -> dict:
    client = client_for(integ)
    queued = 0
    for lib in await client.book_libraries():
        if queued >= _PULL_CAP:
            break  # cap reached — don't paginate the remaining libraries this tick
        items = await client.iter_items(lib["id"])                       # network (async)
        queued += await asyncio.to_thread(_queue_abs_items, db, integ, items, _PULL_CAP - queued)
    integ.last_sync_at = _utcnow()
    integ.last_error = None
    db.commit()
    return {"queued": queued}


def _queue_storyteller_books(db: Session, integ: Integration, books: list, remaining: int) -> int:
    """Sync hot loop (see _queue_abs_items) — run OFF the loop so a large library can't freeze it."""
    queued = 0
    for b in books:
        if queued >= remaining:
            break
        want = _missing_format(b["has_ebook"], b["has_audio"])
        if want and _queue_want(db, integ, b["title"], b["author"], want):
            queued += 1
    return queued


async def _pull_storyteller(db: Session, integ: Integration) -> dict:
    client = client_for(integ)
    books = await client.list_books()                                    # network (async)
    queued = await asyncio.to_thread(_queue_storyteller_books, db, integ, books, _PULL_CAP)
    integ.last_sync_at = _utcnow()
    integ.last_error = None
    db.commit()
    return {"queued": queued}


def _queue_want(db: Session, integ: Integration, title: str | None, author: str | None,
                want: str) -> bool:
    """Queue a missing-format fetch for ``title`` as a QueuedHook (the existing auto-fetch flow grabs
    it, variant-aware). Returns True if newly queued. Skips when already queued or unmatchable."""
    nk = norm_title(title or "")
    if not nk:
        return False
    # Dedup: already queued / in-flight / hooked / failed for this title + format. "failed" is
    # included so an unobtainable want isn't re-queued every tick (which would re-hammer Prowlarr and
    # grow rows unbounded). The operator can clear a failed want to retry it.
    if db.scalar(select(QueuedHook.id).where(
            QueuedHook.norm_key == nk, QueuedHook.variant == want,
            QueuedHook.status.in_(("pending", "downloading", "hooked", "failed")))):
        return False
    # Require a Shelf catalog match — only fetch titles Shelf actually knows (bounds the volume; an
    # unknown ABS item won't flood the pipeline with a doomed search).
    if db.scalar(select(CatalogWork.id).where(CatalogWork.norm_key == nk).limit(1)) is None:
        return False
    owner = db.scalar(select(User.id).where(
        User.role == "admin", User.is_active.is_(True)).order_by(User.id))
    db.add(QueuedHook(title=(title or "")[:512], norm_key=nk, author=author,
                      media_kind=("audio" if want == "audiobook" else "text"),
                      variant=want, reason=integ.kind, source=integ.kind, user_id=owner,
                      status="pending"))
    db.commit()
    return True


def _stage_for_storyteller(work: Work, fmt: str, book_dir: str) -> list[str]:
    """Copy a Work's file(s) into ``book_dir`` (one folder per book, so Storyteller groups them),
    converting an ebook to EPUB on demand. Returns the staged path(s) to hand to create_book."""
    os.makedirs(book_dir, exist_ok=True)
    src = work.local_path
    out: list[str] = []
    if fmt == "audio":
        # local_path is the audio file or a folder of audio files; copy them in.
        if os.path.isdir(src):
            for name in os.listdir(src):
                sp = os.path.join(src, name)
                if os.path.isfile(sp):
                    dp = os.path.join(book_dir, name)
                    shutil.copy2(sp, dp)
                    out.append(dp)
        elif os.path.isfile(src):
            dp = os.path.join(book_dir, os.path.basename(src))
            shutil.copy2(src, dp)
            out.append(dp)
        return out
    # ebook: Storyteller needs EPUB. Copy as-is if already EPUB, else convert on demand.
    if not os.path.isfile(src):
        return out
    if os.path.splitext(src)[1].lower() == ".epub":
        dp = os.path.join(book_dir, os.path.basename(src))
        shutil.copy2(src, dp)
        return [dp]
    dp = os.path.join(book_dir, _safe(work.title) + ".epub")
    converted = convert.to_epub_from(src, dp)
    return [converted] if converted else []
