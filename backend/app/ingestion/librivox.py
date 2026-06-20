"""LibriVox public-domain audiobook fetcher.

LibriVox (librivox.org) hosts free, PUBLIC-DOMAIN audiobooks (classics) read by volunteers, served as
a zip of MP3s from the Internet Archive. Unlike the Prowlarr pipeline this needs no indexer or
credentials — it's a direct API search + HTTP download, used as the audiobook cascade's public-domain
FALLBACK (tried after the pipelines). It only matches public-domain titles, so it naturally returns
nothing for modern books.

The fetched zip is extracted and imported via the shared audiobook import core (a media_kind="audio"
Work on the separate audiobook library path).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import zipfile

from sqlalchemy.orm import Session

from .. import telemetry
from ..models import CatalogWork, DownloadJob
from . import import_core
from .extract import norm_title
from .fuzzy import ratio

log = logging.getLogger("shelf.librivox")

_API = "https://librivox.org/api/feed/audiobooks/"
_UA = "Mozilla/5.0 (Shelf audiobook fetch)"
_MATCH_MIN = 0.8                       # title-similarity floor for a confident public-domain match
_AUDIO_IN_ZIP = (".mp3", ".m4b", ".m4a", ".ogg", ".flac", ".opus")
_MAX_EXTRACT_BYTES = 8 * 1024**3       # abort an absurdly large (zip-bomb) extract at ~8 GB
# Strong refs to in-flight background tasks: asyncio keeps only a WEAK reference to a bare
# create_task(), so without this the download/import task can be GC'd mid-flight (job stuck
# 'downloading'). Discarded on completion.
_TASKS: set[asyncio.Task] = set()
# LibriVox 'language' is a full English name; map the common ones to the catalog's ISO-639-1 code so a
# request in one language doesn't grab another language's reading. Unknown names never block a match.
_LANG_NAME = {
    "english": "en", "french": "fr", "german": "de", "spanish": "es", "italian": "it",
    "portuguese": "pt", "dutch": "nl", "russian": "ru", "japanese": "ja", "chinese": "zh",
    "latin": "la", "greek": "el", "polish": "pl", "swedish": "sv",
}


def configured(db: Session) -> bool:
    """LibriVox needs no credentials, so it's always available as the public-domain audiobook route."""
    return True


def _author_str(book: dict) -> str:
    a = (book.get("authors") or [{}])[0] or {}
    return " ".join(x for x in (a.get("first_name"), a.get("last_name")) if x).strip()


async def _search(title: str, author: str | None) -> list[dict]:
    params = {"title": title, "format": "json", "limit": 5, "extended": 1}
    try:
        async with telemetry.instrument("integration", timeout=20, follow_redirects=True) as c:
            r = await c.get(_API, params=params, headers={"User-Agent": _UA})
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("books") or []
    except Exception as exc:  # noqa: BLE001 — a search failure is a no-result, not a hard error
        log.info("librivox search failed for %r: %s", title, exc)
        return []


def _pick(books: list[dict], title: str, author: str | None, want_lang: str | None) -> dict | None:
    """The best public-domain match: title similarity (+ a small author bonus), language-gated, above
    the confidence floor and carrying a downloadable zip."""
    nt = norm_title(title)
    na = norm_title(author or "")
    best, best_score = None, 0.0
    for b in books:
        if not b.get("url_zip_file"):
            continue
        if want_lang:
            blang = _LANG_NAME.get((b.get("language") or "").strip().lower())
            if blang and blang != want_lang:
                continue
        s = ratio(nt, norm_title(b.get("title") or "")) / 100.0
        if na and _author_str(b) and ratio(na, norm_title(_author_str(b))) >= 80:
            s += 0.1
        if s > best_score:
            best, best_score = b, s
    return best if best_score >= _MATCH_MIN else None


async def grab(db: Session, cw: CatalogWork, *, user_id: int | None = None,
               shelf_id: int | None = None, context: dict | None = None) -> DownloadJob | None:
    """Find `cw` on LibriVox and, on a confident public-domain match, fetch + import its audiobook.
    Returns a DownloadJob (status 'downloading'; the background task flips it to imported/failed), or
    None when there's no public-domain match."""
    from . import language

    want_lang = language.canonicalize(cw.language) if cw.language else None
    best = _pick(await _search(cw.title, cw.author), cw.title, cw.author, want_lang)
    if best is None:
        return None
    job = DownloadJob(
        catalog_work_id=cw.id, user_id=user_id, target_shelf_id=shelf_id, title=cw.title,
        fmt="audio", grab_kind="librivox", status="downloading", indexer="LibriVox",
        release_title=best.get("title"),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    # Download + import OFF the request: a LibriVox zip is hundreds of MB. ponytail: an in-process task
    # (no cross-restart resume) is fine for an optional public-domain fallback — a restart mid-download
    # just leaves the job 'downloading'; upgrade to a scheduler tick if that becomes a problem.
    t = asyncio.create_task(_download_and_import(job.id, best["url_zip_file"]))
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
    return job


async def _download_and_import(job_id: int, url: str) -> None:
    from ..db import SessionLocal

    db = SessionLocal()
    staging = tempfile.mkdtemp(prefix="librivox-")
    try:
        job = db.get(DownloadJob, job_id)
        if job is None:
            return
        cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
        if not await _download_zip(url, staging):
            job.status = "failed"
            job.error = "LibriVox download failed"
            db.commit()
            return
        verdict = import_core._import_audiobook(
            db, job, None, cw, (cw.title if cw else job.title),
            (cw.author if cw else None), staging)
        log.info("librivox job %s → %s", job_id, verdict)
    except Exception:  # noqa: BLE001
        log.exception("librivox download/import failed for job %s", job_id)
        try:
            job = db.get(DownloadJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error = "LibriVox import error"
                db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        db.close()


async def _download_zip(url: str, staging: str) -> bool:
    """Stream the archive.org zip to disk and extract its audio files (flattened) into ``staging``.
    Returns False on a transport error or a non-audio/corrupt zip."""
    # SSRF guard: the zip URL comes from the LibriVox API response — only fetch http(s) on the
    # expected public hosts (archive.org / librivox.org), never an internal/file URL.
    from urllib.parse import urlparse
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not (
            host == "archive.org" or host.endswith(".archive.org")
            or host == "librivox.org" or host.endswith(".librivox.org")):
        log.info("librivox: refusing non-allowlisted zip URL %r", url)
        return False
    zip_path = os.path.join(staging, "audiobook.zip")
    try:
        async with telemetry.instrument("integration", timeout=900, follow_redirects=True) as c:
            async with c.stream("GET", url, headers={"User-Agent": _UA}) as r:
                if r.status_code != 200:
                    return False
                with open(zip_path, "wb") as fh:
                    async for chunk in r.aiter_bytes(65536):
                        fh.write(chunk)
    except Exception as exc:  # noqa: BLE001
        log.info("librivox zip download failed: %s", exc)
        return False
    extracted = total = 0
    used: set[str] = set()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for n in zf.namelist():
                if n.endswith("/") or not n.lower().endswith(_AUDIO_IN_ZIP):
                    continue
                total += zf.getinfo(n).file_size
                if total > _MAX_EXTRACT_BYTES:
                    log.warning("librivox extract aborted: archive exceeds %d bytes", _MAX_EXTRACT_BYTES)
                    return False
                # Distinct tracks can share a sanitized basename — disambiguate so none is overwritten.
                name = _safe_member(os.path.basename(n))
                stem, ext = os.path.splitext(name)
                i = 1
                while name in used:
                    name = f"{stem}_{i}{ext}"
                    i += 1
                used.add(name)
                with zf.open(n) as src, open(os.path.join(staging, name), "wb") as out:
                    shutil.copyfileobj(src, out)
                extracted += 1
    except (zipfile.BadZipFile, OSError) as exc:
        log.info("librivox zip extract failed: %s", exc)
        return False
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass
    return extracted > 0


def _safe_member(name: str) -> str:
    """A safe flattened filename for an extracted zip member (no path components/traversal)."""
    return re.sub(r"[^\w .,'()\-]+", "_", name)[:150] or "track.mp3"
