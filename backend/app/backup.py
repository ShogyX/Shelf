"""Tiered, compressed backup & restore.

A Shelf instance is expensive to rebuild from scratch — indexing a source can take hours and
re-downloading content is slow — so this module exports the database (and optionally the media
files) into a single ``.zip`` that a fresh install can import to resume from where the export left
off, without re-gathering what it doesn't have to.

Three levels trade size for how much is re-gathered on restore:

* ``settings`` — config + library structure + reading progress + the crawl frontier only. The
  discovery catalog is rebuilt by re-indexing and chapter CONTENT is re-downloaded from the
  sources. Smallest (a few MB); use it to clone configuration onto a new box.
* ``data`` — the entire DATABASE (settings + chapter text content + the discovery catalog + the
  raw crawled index pages). No re-crawl / re-index needed; only binary media (comic page images,
  cached covers) is re-fetched. Large but text-complete.
* ``full`` — ``data`` plus every media file. A complete clone; nothing is re-gathered.

The archive is JSONL-per-table (streamed, so a 100k-row table never has to fit in memory) under
``data/<table>.jsonl`` with a ``manifest.json`` describing the level, table list and counts.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import zipfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import IO

from sqlalchemy import insert
from sqlalchemy.orm import Session

from . import models as M
from .db import Base
from .media import media_dir

log = logging.getLogger("shelf.backup")

# Bump when the on-disk format changes incompatibly. Import refuses a newer major than it knows.
SCHEMA_VERSION = 1

# Tables in FK-safe insertion order (parents before children). user_sessions is deliberately
# omitted — login sessions are ephemeral and must not survive a restore onto another box.
_ORDER: list[type] = [
    M.User, M.AppSetting, M.Source, M.UserSettings, M.Integration, M.WatchedFolder,
    M.IndexSite, M.IndexBlock, M.BrokenRelease, M.UsenetGrab, M.Work, M.Bookshelf,
    M.CatalogGroup, M.ChapterContent, M.Chapter, M.IndexedPage, M.CatalogWork, M.CatalogTag,
    M.CatalogCategory, M.DownloadJob, M.StockJob, M.StockItem, M.ReadingState, M.MetadataLink,
    M.CrawlJob, M.QueuedHook, M.BookshelfItem, M.LibraryItem,
]

# What each level carries (by table). "settings" is the floor; richer levels ADD tables.
# Everything not listed for a level is re-gathered on the target (re-index / re-crawl).
_SETTINGS_TABLES = {
    "users", "app_settings", "sources", "user_settings", "integrations", "watched_folders",
    "index_sites", "index_blocks", "works", "bookshelves", "chapters", "reading_states",
    "metadata_links", "crawl_jobs", "queued_hooks", "bookshelf_items", "library_items",
}
# "data" adds the heavy DB content the floor omits: text content, the discovery catalog, the
# raw crawled index pages, and the acquisition-pipeline state (downloads + release registry) —
# so a restore needs no re-crawl / re-index and resumes in-flight downloads.
_DATA_ONLY_TABLES = {
    "chapter_contents", "indexed_pages", "catalog_works", "catalog_groups",
    "catalog_tags", "catalog_categories", "download_jobs", "stock_jobs", "stock_items",
    "usenet_grabs", "broken_releases",
}
LEVELS = ("settings", "data", "full")

# Restore SECTIONS — user-meaningful groups the interactive restore lets an admin include/exclude
# independently, so a migration can bring over (say) the library WITHOUT clobbering the target's
# integrations and notification config. Every exportable table must belong to exactly one section
# (enforced by test_restore_sections_cover_every_table). Order = parents-before-children so a
# "replace" of a whole set inserts FK-safely. The synthetic "media" section covers the media files
# in a full backup. user_sessions is intentionally never exported, so it's not a section.
SECTIONS: list[dict] = [
    {"key": "accounts", "label": "User accounts",
     "description": "Login accounts & passwords, roles, per-user category/permission caps.",
     "tables": ["users"]},
    {"key": "settings", "label": "Settings & notifications",
     "description": "App settings and per-user preferences: SMTP server, Apprise URL, Kindle "
                    "email, crawl tuning, fetch priority, theme, adult-content gate.",
     "tables": ["app_settings", "user_settings"]},
    {"key": "integrations", "label": "Integrations",
     "description": "Readarr, Kapowarr, metadata providers, SABnzbd/Prowlarr — including their "
                    "API keys and base URLs.",
     "tables": ["integrations"]},
    {"key": "sources", "label": "Sources & blocklist",
     "description": "Content sources (with credentials), index sites, the URL/domain blocklist "
                    "and watched folders.",
     "tables": ["sources", "index_sites", "index_blocks", "watched_folders"]},
    {"key": "library", "label": "Library & reading",
     "description": "Your works, chapters & downloaded text, bookshelves, library membership, "
                    "reading progress and metadata links.",
     "tables": ["works", "chapters", "chapter_contents", "bookshelves", "bookshelf_items",
                "library_items", "reading_states", "metadata_links"]},
    {"key": "catalog", "label": "Discovery catalog & index",
     "description": "The cross-source discovery catalog and the raw crawled index pages "
                    "(otherwise rebuilt by re-crawling/re-indexing).",
     "tables": ["catalog_groups", "catalog_works", "catalog_tags", "catalog_categories",
                "indexed_pages"]},
    {"key": "acquisition", "label": "Acquisition & crawl state",
     "description": "In-flight downloads, the usenet/release registry, stock items and crawl/"
                    "queue jobs.",
     "tables": ["broken_releases", "usenet_grabs", "download_jobs", "stock_jobs", "stock_items",
                "crawl_jobs", "queued_hooks"]},
]
_MEDIA_SECTION = "media"
RESTORE_MODES = ("skip", "merge", "replace")


def _section_table_modes(modes: dict[str, str]) -> dict[str, str]:
    """Expand a {section_key: mode} choice into a {table_name: mode} map (default skip)."""
    out: dict[str, str] = {}
    for sec in SECTIONS:
        m = modes.get(sec["key"], "skip")
        if m not in RESTORE_MODES:
            raise ValueError(f"invalid restore mode {m!r} for section {sec['key']!r}")
        for t in sec["tables"]:
            out[t] = m
    return out


def _level_tables(level: str) -> set[str]:
    if level == "settings":
        return set(_SETTINGS_TABLES)
    return set(_SETTINGS_TABLES) | set(_DATA_ONLY_TABLES)  # data + full carry the whole DB


def _dt_columns(model: type) -> set[str]:
    return {c.name for c in model.__table__.columns if "DATETIME" in str(c.type).upper()}


def _serialize_row(model: type, obj, dt_cols: set[str]) -> dict:
    row = {}
    for col in model.__table__.columns:
        v = getattr(obj, col.name)
        if isinstance(v, datetime):
            v = v.isoformat()
        row[col.name] = v
    return row


def _deserialize_row(row: dict, dt_cols: set[str]) -> dict:
    for k in dt_cols:
        v = row.get(k)
        if isinstance(v, str) and v:
            try:
                row[k] = datetime.fromisoformat(v)
            except ValueError:
                row[k] = None
    return row


# ---------------------------------------------------------------------------- export

def _write_archive(db: Session, level: str, fileobj: IO[bytes]) -> dict:
    """Write a backup zip into the open binary ``fileobj`` (a real file OR a non-seekable stream —
    zipfile falls back to streaming/data-descriptor entries when the target can't seek). Returns the
    manifest dict (also stored in the zip)."""
    if level not in LEVELS:
        raise ValueError(f"unknown backup level: {level!r}")
    tables = _level_tables(level)
    counts: dict[str, int] = {}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "level": level,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "tables": [],
        "counts": counts,
        "media_included": level == "full",
    }
    # ZIP_DEFLATED for the text JSONL (compresses ~5-10x); media is added with ZIP_STORED below
    # (jpeg/webp/png are already compressed — re-deflating just burns CPU for ~0 gain).
    with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for model in _ORDER:
            tn = model.__tablename__
            if tn not in tables:
                continue
            manifest["tables"].append(tn)
            dt_cols = _dt_columns(model)
            n = 0
            # Stream rows into the entry so a huge table never materializes in memory.
            with zf.open(f"data/{tn}.jsonl", "w") as fh:
                for obj in db.query(model).yield_per(2000):
                    line = json.dumps(_serialize_row(model, obj, dt_cols),
                                      ensure_ascii=False, default=str) + "\n"
                    fh.write(line.encode("utf-8"))
                    n += 1
            counts[tn] = n
            log.info("backup: exported %s rows from %s", n, tn)
        if level == "full":
            _add_media(zf, manifest)
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return manifest


def export_archive(db: Session, level: str, out_path: Path) -> dict:
    """Write a backup zip to ``out_path``. Returns the manifest dict (also stored in the zip)."""
    with open(out_path, "wb") as f:
        return _write_archive(db, level, f)


def stream_archive(level: str) -> Iterator[bytes]:
    """Yield a backup zip as it is built, so the HTTP response starts IMMEDIATELY and nothing is
    staged on disk first. This is what makes a multi-GB ``full`` backup survive a reverse proxy /
    Cloudflare tunnel (which times out an origin that takes >100s to send its first byte) — the old
    build-whole-file-to-/tmp-then-send path always tripped that timeout, and the client's retries
    then piled up duplicate full builds that filled the disk.

    A writer thread builds the zip into one end of an OS pipe (with its OWN DB session — SQLAlchemy
    sessions aren't thread-safe); this generator drains the other end. If the client disconnects,
    the generator is closed, the read end closes, and the writer's next ``write`` fails with a
    broken pipe so the build stops promptly instead of running on to fill the disk."""
    if level not in LEVELS:
        raise ValueError(f"unknown backup level: {level!r}")
    r_fd, w_fd = os.pipe()
    err: list[BaseException] = []

    def _build() -> None:
        from .db import SessionLocal
        db = SessionLocal()
        try:
            with os.fdopen(w_fd, "wb") as wf:
                _write_archive(db, level, wf)
        except BrokenPipeError:
            log.info("backup stream: client disconnected; build aborted")
        except BaseException as exc:  # noqa: BLE001 — surfaced to the consumer after join
            err.append(exc)
        finally:
            db.close()

    writer = threading.Thread(target=_build, name=f"backup-{level}", daemon=True)
    writer.start()
    try:
        with os.fdopen(r_fd, "rb") as rf:
            while chunk := rf.read(1 << 20):  # 1 MiB
                yield chunk
    finally:
        writer.join(timeout=30)
    if err:
        raise err[0]


def _add_media(zf: zipfile.ZipFile, manifest: dict) -> None:
    root = media_dir()
    files = 0
    for p in root.rglob("*"):
        if p.is_file():
            # Stored (uncompressed) — images are already compressed.
            zf.write(p, f"media/{p.relative_to(root).as_posix()}", zipfile.ZIP_STORED)
            files += 1
    manifest["media_files"] = files
    log.info("backup: added %s media files", files)


# ---------------------------------------------------------------------------- import

def _model_by_table() -> dict[str, type]:
    return {m.__tablename__: m for m in _ORDER}


def _load_table(db: Session, zf: zipfile.ZipFile, model: type, entry: str, *,
                keep_existing: bool) -> int:
    """Stream rows from a JSONL zip entry into ``model``, batched. With ``keep_existing`` a row
    whose primary key already exists is left untouched (merge); otherwise it's a plain insert
    (the caller has already cleared the table for a replace)."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    dt_cols = _dt_columns(model)

    def _flush(batch: list[dict]) -> int:
        if not batch:
            return 0
        if keep_existing:
            db.execute(sqlite_insert(model).on_conflict_do_nothing(), batch)
        else:
            db.execute(insert(model), batch)
        return len(batch)

    batch: list[dict] = []
    loaded = 0
    with zf.open(entry, "r") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            batch.append(_deserialize_row(json.loads(raw), dt_cols))
            if len(batch) >= 1000:
                loaded += _flush(batch)
                batch = []
    loaded += _flush(batch)
    db.commit()
    return loaded


def restore_plan(db: Session, zip_path: Path) -> dict:
    """Inspect a backup zip without changing anything: return its manifest plus, per restorable
    section, how many rows the backup carries and how many the target already has — so the UI can
    let the admin choose what to import vs. leave in place."""
    by_table = _model_by_table()
    with zipfile.ZipFile(zip_path, "r") as zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError as e:
            raise ValueError("not a Shelf backup: manifest.json missing") from e
        names = set(zf.namelist())
    ver = manifest.get("schema_version", 0)
    if ver > SCHEMA_VERSION:
        raise ValueError(
            f"backup schema v{ver} is newer than this install supports (v{SCHEMA_VERSION}); "
            "upgrade Shelf before importing")
    counts = manifest.get("counts", {})

    def _target_rows(tables: list[str]) -> int:
        from sqlalchemy import func, select
        total = 0
        for t in tables:
            model = by_table.get(t)
            if model is not None:
                total += db.scalar(select(func.count()).select_from(model)) or 0
        return total

    sections = []
    for sec in SECTIONS:
        in_backup = any(f"data/{t}.jsonl" in names for t in sec["tables"])
        sections.append({
            "key": sec["key"], "label": sec["label"], "description": sec["description"],
            "in_backup": in_backup,
            "backup_rows": sum(int(counts.get(t, 0)) for t in sec["tables"]),
            "target_rows": _target_rows(sec["tables"]),
        })
    media = {
        "key": _MEDIA_SECTION, "label": "Media files",
        "description": "Comic/manga page images and cached covers. Only present in a “full” "
                       "backup; otherwise re-fetched on demand.",
        "in_backup": bool(manifest.get("media_included")),
        "backup_files": int(manifest.get("media_files", 0)),
    }
    return {
        "manifest": {"level": manifest.get("level"), "created_at": manifest.get("created_at"),
                     "schema_version": ver},
        "target_empty": database_is_empty(db),
        "sections": sections,
        "media": media,
    }


def import_selective(db: Session, zip_path: Path, modes: dict[str, str]) -> dict:
    """Restore only the chosen sections from a backup. ``modes`` maps a section key (or "media") to
    one of skip | merge | replace:

      * skip    — leave the target's rows for that section untouched (don't import).
      * merge   — insert backup rows whose primary key isn't already present; keep existing rows.
      * replace — delete the target's rows in that section, then load the backup's.

    This lets a migration bring over, say, the library while leaving the target's integrations and
    notification settings exactly as they are. Tables are deleted children-first and inserted
    parents-first so a "replace" stays FK-safe."""
    table_mode = _section_table_modes(modes)
    by_table = _model_by_table()  # noqa: F841 — kept for symmetry/readability
    summary: dict[str, int] = {}
    with zipfile.ZipFile(zip_path, "r") as zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError as e:
            raise ValueError("not a Shelf backup: manifest.json missing") from e
        ver = manifest.get("schema_version", 0)
        if ver > SCHEMA_VERSION:
            raise ValueError(
                f"backup schema v{ver} is newer than this install supports (v{SCHEMA_VERSION}); "
                "upgrade Shelf before importing")
        names = set(zf.namelist())
        # Phase 1 — clear every "replace" table, children before parents (FK-safe).
        from sqlalchemy import delete
        for model in reversed(_ORDER):
            if table_mode.get(model.__tablename__) == "replace":
                db.execute(delete(model))
        db.commit()
        # Phase 2 — load, parents before children.
        for model in _ORDER:
            tn = model.__tablename__
            mode = table_mode.get(tn, "skip")
            if mode == "skip":
                continue
            entry = f"data/{tn}.jsonl"
            if entry not in names:
                continue
            loaded = _load_table(db, zf, model, entry, keep_existing=(mode == "merge"))
            summary[tn] = loaded
            log.info("restore: loaded %s rows into %s (%s)", loaded, tn, mode)
        media_mode = modes.get(_MEDIA_SECTION, "skip")
        if media_mode in ("merge", "replace") and manifest.get("media_included"):
            n = _restore_media(zf, overwrite=(media_mode == "replace"))
            summary["media_files"] = n
    _reconcile_after_import(db)
    return {"manifest": manifest, "loaded": summary}


def import_archive(db: Session, zip_path: Path, *, restore_media: bool = True) -> dict:
    """Load a backup zip into the (empty) database. Returns a summary of rows loaded per table.

    Loads tables in FK-safe order, preserving primary keys so foreign keys line up. After loading,
    reconciles dangling content references (a ``settings``/``data`` restore has no media / no
    content for some chapters) so the crawler re-gathers exactly what's missing instead of serving
    broken rows."""
    by_table = _model_by_table()
    summary: dict[str, int] = {}
    with zipfile.ZipFile(zip_path, "r") as zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError as e:
            raise ValueError("not a Shelf backup: manifest.json missing") from e
        ver = manifest.get("schema_version", 0)
        if ver > SCHEMA_VERSION:
            raise ValueError(
                f"backup schema v{ver} is newer than this install supports (v{SCHEMA_VERSION}); "
                "upgrade Shelf before importing")
        names = set(zf.namelist())
        for model in _ORDER:  # FK-safe order
            tn = model.__tablename__
            entry = f"data/{tn}.jsonl"
            if entry not in names:
                continue
            dt_cols = _dt_columns(model)
            batch: list[dict] = []
            loaded = 0
            with zf.open(entry, "r") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    batch.append(_deserialize_row(json.loads(raw), dt_cols))
                    if len(batch) >= 1000:
                        db.execute(insert(model), batch)
                        loaded += len(batch)
                        batch = []
            if batch:
                db.execute(insert(model), batch)
                loaded += len(batch)
            summary[tn] = loaded
            db.commit()
            log.info("restore: loaded %s rows into %s", loaded, tn)
        if restore_media and manifest.get("media_included"):
            _restore_media(zf)
    _reconcile_after_import(db)
    return {"manifest": manifest, "loaded": summary}


def _restore_media(zf: zipfile.ZipFile, *, overwrite: bool = True) -> int:
    root = media_dir()
    n = 0
    for name in zf.namelist():
        if name.startswith("media/") and not name.endswith("/"):
            dest = root / name[len("media/"):]
            if not overwrite and dest.exists():
                continue  # merge: keep the file already on the target
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(dest, "wb") as out:
                out.write(src.read())
            n += 1
    log.info("restore: wrote %s media files", n)
    return n


def _reconcile_after_import(db: Session) -> None:
    """Make the restored DB internally consistent + ready to resume.

    * A chapter whose content wasn't in the backup (settings level, or media-less data where the
      row exists but points at a now-missing content row) is reset to ``pending`` with no content,
      so the backfill re-downloads it.
    * The derived discovery-grouping caches (catalog_groups/tags/categories) are left to the
      regroup tick to rebuild when they weren't part of the level."""
    have_content = {cid for (cid,) in db.query(M.ChapterContent.id).all()}
    reset = 0
    for ch in db.query(M.Chapter).yield_per(2000):
        if ch.content_id is not None and ch.content_id not in have_content:
            ch.content_id = None
            ch.fetch_status = "pending"
            reset += 1
    if reset:
        db.commit()
        log.info("restore: reset %s chapters with missing content to pending (will re-fetch)", reset)


def database_is_empty(db: Session) -> bool:
    """A restore target must be fresh — refuse to clobber an instance that already has users."""
    return db.query(M.User).first() is None


def wipe_database(db: Session) -> None:
    """Delete all rows from every exportable table (children first, FK-safe) + login sessions.
    Used only by an explicit ``wipe=true`` restore onto a non-empty instance."""
    from sqlalchemy import delete
    db.execute(delete(M.UserSession))
    for model in reversed(_ORDER):  # children before parents
        db.execute(delete(model))
    db.commit()
    log.info("restore: wiped existing database for replacement")
