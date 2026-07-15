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
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..integrations import IntegrationError
from ..integrations.virustotal import VirusTotalClient, VTUnavailable
from ..models import CatalogWork, DownloadJob, Integration, VtSubmission
from . import import_core

log = logging.getLogger("shelf.security")

# VirusTotal free-tier quota (4 requests/min, 500/day). Enforced DURABLY by counting VtSubmission
# rows (the in-memory ratelimit spacer can't back a 500/day counter across restarts). Operator-
# overridable via the VT integration config for a paid key.
_VT_PER_MIN = 4
_VT_PER_DAY = 500
_VT_MIN_WINDOW = timedelta(minutes=1)
_VT_DAY_WINDOW = timedelta(days=1)

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


def _cap(vt: Integration | None, key: str, default: int) -> int:
    try:
        return max(1, int(((vt.config if vt else None) or {}).get(key, default)))
    except (TypeError, ValueError):
        return default


def vt_blocked_until(db: Session, vt: Integration | None = None) -> datetime | None:
    """If a VirusTotal lookup would now exceed the free-tier quota, return the time the oldest
    in-window submission ages out (when a slot frees). Otherwise None (a lookup is allowed now).
    Enforces BOTH the per-minute and per-day caps durably (counting VtSubmission rows) and returns
    the LATER block time — a clone of downloads._grab_blocked_until, but over two windows and a single
    global counter rather than per-release. Caps overridable via vt.config (vt_per_min/vt_per_day)."""
    per_min = _cap(vt, "vt_per_min", _VT_PER_MIN)
    per_day = _cap(vt, "vt_per_day", _VT_PER_DAY)
    blocked: datetime | None = None
    for limit, window in ((per_min, _VT_MIN_WINDOW), (per_day, _VT_DAY_WINDOW)):
        since = import_core._utcnow() - window
        times = db.scalars(
            select(VtSubmission.created_at)
            # strict ">": a submission exactly one window old has aged out, so a job deferred to that
            # instant is never immediately re-deferred at the boundary.
            .where(VtSubmission.created_at > since).order_by(VtSubmission.created_at)
        ).all()
        if len(times) < limit:
            continue
        # Need enough of the oldest to expire that the in-window count drops below `limit`.
        free_at = import_core._aware(times[len(times) - limit]) + window
        blocked = free_at if blocked is None else max(blocked, free_at)
    return blocked


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


async def scan_gate(db: Session, job: DownloadJob, qb: Integration) -> str:
    """Scan the completed torrent's book file(s) against VirusTotal. Returns one of:
      * "block" — infected / held: the job has been failed + the block recorded (caller deletes).
      * "allow" — clean (or scanning disabled / nothing to scan): the file may be imported.
      * "park"  — VT quota exhausted OR a transient VT outage (429/503/connection): DON'T import and
                  DON'T fail-open; the caller pauses the torrent and re-checks later.

    A malicious VERDICT always blocks. Parking (replacing the old fail-open) applies ONLY to VT
    rate-limit/outage — an "unknown" (404) hash stays a hard block-or-allow per ``vt_block_unknown``,
    never a wait-for-analysis state. Lookup-only: hashes are looked up, files are never uploaded."""
    vt = get_virustotal(db)
    if vt is None:
        return "allow"  # scanning disabled
    staging = import_core._job_dir(import_core.map_path(job.storage_path, import_core._path_mappings(qb)))
    if not staging:
        return "allow"  # not visible yet — let _import_completed handle the visibility wait
    files = _book_files(staging)
    if not files:
        return "allow"  # nothing importable to scan; verify will reject it anyway
    # Pre-flight the quota BEFORE hashing: if a lookup would exceed the free-tier cap, park the whole
    # job (all-or-nothing per tick) rather than burn a partial scan — resume re-hashes all files.
    if vt_blocked_until(db, vt) is not None:
        log.info("VirusTotal quota reached — parking job=%s %r", job.id, job.title)
        return "park"
    client = VirusTotalClient(vt.api_key, config=vt.config)
    block_unknown = bool((vt.config or {}).get("vt_block_unknown"))
    susp_max = _suspicious_threshold(vt)
    for path in files:
        sha = await asyncio.to_thread(_sha256, path)   # full-file read — keep off the event loop
        try:
            stats = await client.lookup(sha)
        except VTUnavailable as exc:
            # Rate-limit / outage → PARK (never fail-open). The local ledger may drift from VT's real
            # counter; catching the API's own 429 here reconciles us (max-park-age is the backstop).
            log.info("VirusTotal unavailable for %r — parking job=%s: %s",
                     os.path.basename(path), job.id, exc)
            return "park"
        except IntegrationError as exc:
            # Any OTHER VT API error (revoked/invalid key → 401, a 5xx, a malformed response) must
            # NOT propagate: uncaught it aborts the whole poll tick and blocks EVERY other job from
            # importing. Treat it like an outage — park + let the operator fix the key;
            # max-park-age still backstops a permanently-broken VT config.
            log.warning("VirusTotal error for %r — parking job=%s: %s",
                        os.path.basename(path), job.id, exc)
            return "park"
        # A successful lookup (incl. a 404 returning None) counts against the durable quota — record
        # ONE ledger row per lookup that actually returned (NOT on raise).
        db.add(VtSubmission())
        db.commit()
        if stats is None:  # 404 — unknown to VT
            if block_unknown:
                _block(db, job, detail=f"{os.path.basename(path)} unknown to VirusTotal (held)")
                return "block"
            continue
        mal = int(stats.get("malicious") or 0)
        susp = int(stats.get("suspicious") or 0)
        if mal > 0 or susp > susp_max:
            _block(db, job,
                   detail=f"{os.path.basename(path)} flagged ({mal} malicious, {susp} suspicious)")
            return "block"
    return "allow"


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
