"""Scan the library for corrupt / unplayable audiobook and ebook files.

Audio uses **ffprobe as ground truth** (returncode 0 + a real duration). This cleanly separates real
corruption from container variety: a byte-sniff heuristic (look for an ID3 tag or an MPEG frame sync)
false-flags every healthy ``.m4b``/``.m4a``/``.wma`` file, since MP4/ASF headers contain neither — so
"239 corrupt audiobooks" turned out to be ~0 corrupt and ~229 perfectly playable MP4/ASF containers.

Ebooks/comics are validated by container: ``.epub``/``.cbz``/``.zip`` must open as an archive; ``.pdf``
must start with ``%PDF``; ``.cbr`` needs the ``Rar!`` signature; anything else must be non-empty and
not an all-zero blob.

Read-only: it only REPORTS (id / title / path / reason). Nothing is modified or re-acquired.

Usage (run from /root/Shelf/backend):
    python scripts/scan_corrupt_media.py                 # all kinds
    python scripts/scan_corrupt_media.py --kind audio    # audiobooks only (audio|text|comic|all)
    python scripts/scan_corrupt_media.py --json          # machine-readable
    python scripts/scan_corrupt_media.py --limit 50      # cap works scanned (quick sample)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import zipfile

# Guard: run from the backend dir against the real DB (no relative ./shelf.db surprises).
DB = os.path.abspath("shelf.db")
assert os.path.basename(os.getcwd()) == "backend" and os.path.exists(DB), (
    f"run from /root/Shelf/backend; shelf.db not found at {DB}")

from app.db import SessionLocal  # noqa: E402
from app.models import Work  # noqa: E402
from sqlalchemy import select  # noqa: E402

_AUDIO_EXT = (".mp3", ".m4a", ".m4b", ".m4v", ".aac", ".ogg", ".opus", ".flac", ".wma", ".wav")
_MIN_AUDIO_S = 30.0   # a real audiobook track runs longer than this; guards against empty/stub files


def _first_audio_file(path: str) -> str | None:
    """A single-file audiobook is ``path`` itself; a folder audiobook → its first audio file (sorted).
    We probe the first file as a representative sample rather than every track (which would be slow)."""
    if os.path.isfile(path):
        return path
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.lower().endswith(_AUDIO_EXT):
                return os.path.join(path, name)
    return None


def _ffprobe_duration(fp: str) -> float | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", fp],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        return None


def check_audio(path: str) -> str | None:
    """None = OK; else a human reason string."""
    fp = _first_audio_file(path)
    if fp is None:
        return "no audio file in folder" if os.path.isdir(path) else "file missing"
    if not os.path.isfile(fp):
        return "file missing"
    if os.path.getsize(fp) == 0:
        return "empty file (0 bytes)"
    dur = _ffprobe_duration(fp)
    if dur is None:
        return "ffprobe cannot decode (not valid audio)"
    # A multi-file audiobook's FIRST track is often a short intro/credits, so its length isn't
    # representative — only a SINGLE-file audiobook this short is genuinely suspect (truncated/stub).
    if os.path.isfile(path) and dur < _MIN_AUDIO_S:
        return f"suspiciously short ({dur:.0f}s)"
    # Leading zero-fill gap: a real audio file has header bytes within its first 64 KB (mp3 ID3/frame
    # sync, mp4 ftyp box — whose non-zero brand appears at byte 4). A big zero run at the start is a
    # partial write that ffprobe RESYNCS past but a strict player (iOS AVPlayer) refuses — this is the
    # "plays in the browser, silent in Still" case (e.g. a ~1.9 MB zero prefix).
    with open(fp, "rb") as f:
        head = f.read(65536)
    if head and not any(head):
        return "leading zero-fill (strict players fail; ffprobe recovers)"
    return None


def _zip_ok(fp: str) -> bool:
    try:
        with zipfile.ZipFile(fp) as z:
            return z.testzip() is None
    except Exception:  # noqa: BLE001
        return False


def check_ebook(path: str) -> str | None:
    if not os.path.isfile(path):
        return "file missing"
    sz = os.path.getsize(path)
    if sz == 0:
        return "empty file (0 bytes)"
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        head = f.read(8)
    if ext in (".epub", ".cbz", ".zip"):
        return None if _zip_ok(path) else "not a valid zip archive"
    if ext == ".pdf":
        return None if head.startswith(b"%PDF") else "missing %PDF header"
    if ext == ".cbr":
        return None if head[:4] == b"Rar!" else "missing RAR signature"
    # mobi/azw3/txt/other: just require it isn't an all-zero blob.
    with open(path, "rb") as f:
        chunk = f.read(min(sz, 1 << 20))
    return None if any(chunk) else "all-zero content"


def scan(kind: str, limit: int | None):
    kinds = {"audio": ["audio"], "text": ["text"], "comic": ["comic"],
             "all": ["audio", "text", "comic"]}[kind]
    db = SessionLocal()
    rows = db.execute(
        select(Work.id, Work.title, Work.media_kind, Work.local_path)
        .where(Work.media_kind.in_(kinds), Work.local_path.is_not(None), Work.local_path != "")
        .order_by(Work.id)).all()
    db.close()
    bad = []
    for i, (wid, title, mk, path) in enumerate(rows):
        if limit and i >= limit:
            break
        reason = check_audio(path) if mk == "audio" else check_ebook(path)
        if reason:
            bad.append({"id": wid, "title": title, "media_kind": mk, "path": path, "reason": reason})
    scanned = min(len(rows), limit) if limit else len(rows)
    return scanned, bad


def main():
    ap = argparse.ArgumentParser(description="Scan for corrupt/unplayable audiobook & ebook files.")
    ap.add_argument("--kind", choices=["audio", "text", "comic", "all"], default="all")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--limit", type=int, default=0, help="cap works scanned per run (0 = no cap)")
    args = ap.parse_args()
    scanned, bad = scan(args.kind, args.limit or None)
    if args.json:
        print(json.dumps({"scanned": scanned, "corrupt_count": len(bad), "corrupt": bad}, indent=1))
        return
    print(f"Scanned {scanned} works ({args.kind}); {len(bad)} corrupt/unplayable:")
    for b in bad:
        print(f"  [{b['id']}] {b['media_kind']:5} {b['reason']:36} {(b['title'] or '')[:42]!r}")
        print(f"        {b['path']}")
    if not bad:
        print("  (none — every file is valid)")


if __name__ == "__main__":
    main()
