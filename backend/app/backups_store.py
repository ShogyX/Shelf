"""On-disk store of instance backups, so a backup is a selectable object in the Backups tab —
whether the app created it or an admin uploaded it from another VM.

Backups live as ``.zip`` files under :func:`backups_dir` (kept OUTSIDE media_dir so a ``full``
backup doesn't recurse into the store). A build in progress is a ``<name>.partial`` file that's
atomically renamed to ``<name>.zip`` on success; the listing surfaces in-progress and failed
builds so the UI can show progress without holding a multi-minute HTTP request open (a 36 GB
``full`` build would otherwise blow past a reverse-proxy/tunnel idle timeout).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from .config import get_settings

log = logging.getLogger("shelf.backup")

# Only ever heavy-mutate the store from one operation at a time (create OR restore). These walk the
# whole media tree / rewrite the whole DB; overlapping them would thrash disk + the SQLite writer.
OP_LOCK = threading.Lock()

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.zip$")
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# In-memory status for builds (name -> {"status","error","level","started"}). Ephemeral: a build
# that was running at a crash leaves only its .partial behind, which the listing reports as failed.
_BUILDS: dict[str, dict] = {}
_BUILDS_LOCK = threading.Lock()


def backups_dir() -> Path:
    s = get_settings()
    d = (Path(s.backup_dir) if getattr(s, "backup_dir", "")
         else (Path(__file__).resolve().parent.parent / "backups"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_path(name: str) -> Path:
    """Resolve a backup file name to a path INSIDE the store, rejecting traversal/odd names."""
    if not name or not _NAME_RE.match(name):
        raise ValueError("invalid backup name")
    root = backups_dir().resolve()
    p = (root / name).resolve()
    if p.parent != root:
        raise ValueError("invalid backup name")
    return p


def new_internal_name(level: str, *, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"backup-{level}-{stamp}.zip"


def sanitized_upload_name(original: str, *, now: datetime | None = None) -> str:
    """A safe, collision-resistant store name for an uploaded file, keeping a hint of its origin."""
    stem = Path(original or "backup").name
    if stem.lower().endswith(".zip"):
        stem = stem[:-4]
    stem = _SAFE_CHARS.sub("-", stem).strip("-._") or "backup"
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"upload-{stem[:80]}-{stamp}.zip"


def free_bytes() -> int:
    return shutil.disk_usage(backups_dir()).free


def read_manifest(path: Path) -> dict | None:
    """The backup's manifest (level/counts/…) — read from the zip's central directory only, so it's
    cheap even for a 36 GB archive. None if the file isn't a readable Shelf backup."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            return json.loads(zf.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, OSError, ValueError):
        return None


def entry(path: Path, *, schema_version: int) -> dict:
    st = path.stat()
    manifest = read_manifest(path)
    name = path.name
    origin = "uploaded" if name.startswith("upload-") else "internal"
    ok = manifest is not None
    mver = int((manifest or {}).get("schema_version", 0)) if ok else 0
    return {
        "name": name,
        "size_bytes": st.st_size,
        "created_at": (manifest or {}).get("created_at")
        or datetime.utcfromtimestamp(st.st_mtime).isoformat() + "Z",
        "origin": origin,
        "status": "ready",
        "valid": ok,
        "level": (manifest or {}).get("level"),
        "schema_version": mver,
        "media_files": int((manifest or {}).get("media_files", 0)),
        # A newer-schema backup can't be safely imported by this build.
        "restorable": ok and mver <= schema_version,
    }


def list_backups(schema_version: int) -> list[dict]:
    """Every backup in the store (newest first), plus any in-progress / failed builds."""
    root = backups_dir()
    out: list[dict] = []
    for p in root.glob("*.zip"):
        try:
            out.append(entry(p, schema_version=schema_version))
        except OSError:
            continue
    # In-progress / failed builds (a .partial file with no finished .zip yet).
    with _BUILDS_LOCK:
        builds = dict(_BUILDS)
    for p in root.glob("*.partial"):
        name = p.name[:-len(".partial")]
        info = builds.get(name, {})
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        out.append({
            "name": name, "size_bytes": size,
            "created_at": info.get("started"), "origin": "internal",
            "status": "failed" if info.get("error") else "building",
            "error": info.get("error"), "valid": False, "level": info.get("level"),
            "schema_version": 0, "media_files": 0, "restorable": False,
        })
    out.sort(key=lambda e: (e.get("status") != "building", e.get("created_at") or ""), reverse=True)
    return out


def delete_backup(name: str) -> None:
    p = safe_path(name)
    p.unlink(missing_ok=True)
    (p.parent / f"{name}.partial").unlink(missing_ok=True)  # also clear a failed build's leftovers
    with _BUILDS_LOCK:
        _BUILDS.pop(name, None)


# Internal (app-created) backups are named "backup-<level>-<stamp>.zip"; uploads ("upload-…") are
# the operator's own and NEVER auto-pruned.
_INTERNAL_RE = re.compile(r"^backup-[a-z]+-\d{8}-\d{6}\.zip$")


def prune_internal_backups(keep: int) -> int:
    """Delete the oldest app-created backups beyond the newest ``keep`` (uploads untouched). Returns
    the number removed. Without this, scheduled builds accumulate forever and fill the disk (a
    ``full`` backup can be tens of GB)."""
    if keep < 0:
        return 0
    root = backups_dir()
    internal = sorted(
        (p for p in root.glob("backup-*.zip") if _INTERNAL_RE.match(p.name)),
        key=lambda p: p.name, reverse=True,   # name embeds the timestamp → newest first
    )
    removed = 0
    for p in internal[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            log.warning("backup prune: could not remove %s", p.name)
    if removed:
        log.info("backup prune: removed %d old backup(s), kept %d", removed, min(keep, len(internal)))
    return removed


def start_build(level: str) -> str:
    """Kick off an app-created backup in the background (writing into the store). Returns the name
    the finished file will have; the listing reports its progress. Raises if one is already running
    or there isn't plausibly enough free space."""
    from . import backup as backup_mod
    if not OP_LOCK.acquire(blocking=False):
        raise RuntimeError("Another backup or restore is already running.")
    try:
        name = new_internal_name(level)
        final = safe_path(name)
        partial = final.parent / f"{name}.partial"
        with _BUILDS_LOCK:
            _BUILDS[name] = {"status": "building", "level": level, "error": None,
                             "started": datetime.now(UTC).isoformat()}

        def _run() -> None:
            from .db import SessionLocal
            db = SessionLocal()
            try:
                with open(partial, "wb") as fh:
                    backup_mod._write_archive(db, level, fh)
                partial.replace(final)  # atomic publish
                with _BUILDS_LOCK:
                    _BUILDS.pop(name, None)
                log.info("backup store: built %s", name)
                try:                                  # retention: keep the N newest app-created
                    prune_internal_backups(get_settings().auto_backup_keep)
                except Exception:  # noqa: BLE001 — pruning must never fail the build
                    log.exception("backup store: prune after build failed")
            except BaseException as exc:  # noqa: BLE001
                log.exception("backup store: build %s failed", name)
                partial.unlink(missing_ok=True)
                with _BUILDS_LOCK:
                    _BUILDS[name] = {**_BUILDS.get(name, {}), "status": "failed", "error": str(exc)}
            finally:
                db.close()
                OP_LOCK.release()

        threading.Thread(target=_run, name=f"backup-build-{level}", daemon=True).start()
        return name
    except BaseException:
        OP_LOCK.release()
        raise
