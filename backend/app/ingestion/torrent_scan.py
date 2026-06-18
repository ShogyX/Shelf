"""VirusTotal gate for torrent-grabbed files (Batch G).

Torrents are untrusted, so before a completed torrent's file enters the library we SHA-256 each book
file and look the hash up on VirusTotal (DB-lookup only — we never UPLOAD files: privacy + quota).
  * clean   (malicious == 0, suspicious within threshold) → allow import.
  * not clean (malicious > 0, or suspicious over threshold) → block: delete + notify + log + ledger.
  * unknown (404 — not in VT's DB) → configurable: allow (default) or hold (``vt_block_unknown``).

If no VirusTotal integration is configured, the gate is a no-op (scanning disabled → allow).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..integrations import IntegrationError
from ..integrations.virustotal import VirusTotalClient
from ..models import CatalogWork, DownloadJob, Integration
from . import downloads

log = logging.getLogger("shelf.security")

# The malware gate MUST scan a SUPERSET of every format verify can import — otherwise a format verify
# accepts but the gate skips (e.g. .fb2/.djvu) would slip into the library unscanned. Derive from
# verify's set so a future verify-format addition can't silently reopen a gate hole.
from .verify import _BOOK_EXTS as _VERIFY_BOOK_EXTS  # noqa: E402
_BOOK_EXTS = tuple(set(_VERIFY_BOOK_EXTS) | {".md"})


def get_virustotal(db: Session) -> Integration | None:
    return db.scalar(select(Integration).where(
        Integration.kind == "virustotal", Integration.enabled.is_(True)))


def _suspicious_threshold(vt: Integration) -> int:
    try:
        return int((vt.config or {}).get("vt_suspicious_threshold", 0))
    except (TypeError, ValueError):
        return 0


def _book_files(staging_dir: str) -> list[str]:
    out: list[str] = []
    for root, _dirs, files in os.walk(staging_dir):
        for f in files:
            if f.lower().endswith(_BOOK_EXTS):
                out.append(os.path.join(root, f))
    return out


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _block(db: Session, job: DownloadJob, *, detail: str) -> None:
    """Record + surface a malware block: job error, security log, admin notification, ledger."""
    from .. import notifications as notif
    from . import ledger
    job.status = "failed"
    job.error = f"VirusTotal: {detail}"
    db.commit()
    log.warning("MALWARE BLOCKED job=%s title=%r: %s", job.id, job.title, detail)
    notif.dispatch_soon(db, "security.malware", title="Malware blocked",
                        body=f"{job.title}: {detail} — deleted before import.")
    cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
    if cw is not None:
        ledger.mark_unavailable(db, cw, reason="blocked", provider="torrent")


async def scan_gate(db: Session, job: DownloadJob, qb: Integration) -> bool:
    """Return True if the download must be BLOCKED (infected / held). False = allowed to import.

    Scans the completed torrent's book file(s) against VirusTotal. No VT integration → no-op (False).
    On VT API errors we FAIL OPEN (allow) so an outage doesn't strand every torrent — except an
    actual malicious verdict, which always blocks."""
    vt = get_virustotal(db)
    if vt is None:
        return False  # scanning disabled
    staging = downloads._job_dir(downloads.map_path(job.storage_path, downloads._path_mappings(qb)))
    if not staging:
        return False  # not visible yet — let _import_completed handle the visibility wait
    files = _book_files(staging)
    if not files:
        return False  # nothing importable to scan; verify will reject it anyway
    client = VirusTotalClient(vt.api_key, config=vt.config)
    block_unknown = bool((vt.config or {}).get("vt_block_unknown"))
    susp_max = _suspicious_threshold(vt)
    for path in files:
        try:
            sha = await asyncio.to_thread(_sha256, path)   # full-file read — keep off the event loop
            stats = await client.lookup(sha)
        except IntegrationError as exc:
            log.info("VirusTotal lookup failed for %r (allowing — fail-open): %s",
                     os.path.basename(path), exc)
            continue
        if stats is None:  # 404 — unknown to VT
            if block_unknown:
                _block(db, job, detail=f"{os.path.basename(path)} unknown to VirusTotal (held)")
                return True
            continue
        mal = int(stats.get("malicious") or 0)
        susp = int(stats.get("suspicious") or 0)
        if mal > 0 or susp > susp_max:
            _block(db, job,
                   detail=f"{os.path.basename(path)} flagged ({mal} malicious, {susp} suspicious)")
            return True
    return False


def _demo() -> None:
    """Self-check: book-file discovery + sha256 of a known input (no network)."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        sub = os.path.join(d, "Book")
        os.makedirs(sub)
        with open(os.path.join(sub, "a.epub"), "wb") as f:
            f.write(b"")
        with open(os.path.join(sub, "cover.jpg"), "wb") as f:
            f.write(b"x")
        found = _book_files(d)
        assert len(found) == 1 and found[0].endswith("a.epub"), found
        # sha256 of the empty file — the hash VirusTotal knows as harmless.
        assert _sha256(found[0]) == \
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    print("torrent_scan self-check ok")


if __name__ == "__main__":
    _demo()
