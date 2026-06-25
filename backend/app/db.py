"""Database engine, session factory, and the declarative Base."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

log = logging.getLogger("shelf.db")
settings = get_settings()

_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}
# The default QueuePool (5 + 10 overflow = 15) is too small: the AsyncIO scheduler fires 20+ ticks
# that each hold a Session across slow network I/O (crawl/enrich/download polls), and several come
# due at once — so the 15 connections were constantly exhausted, timing out the web requests, folder
# sync and health probe (QueuePool timeout). Raise the ceiling so ticks + web requests don't starve
# each other; pre_ping drops connections a WAL checkpoint/restart left stale. (Per-connection page
# cache is trimmed to 24 MB below so the larger pool stays memory-bounded — the shared mmap window
# dominates read latency anyway.)
_pool_kw = (
    {"pool_size": 20, "max_overflow": 40, "pool_timeout": 30, "pool_pre_ping": True}
    if _is_sqlite and ":memory:" not in settings.database_url else {}
)
engine = create_engine(settings.database_url, connect_args=_connect_args, future=True, **_pool_kw)
if _is_sqlite and ":memory:" not in settings.database_url:
    # Log the RESOLVED absolute DB file on boot. The URL is a relative `./shelf.db` (cwd-dependent);
    # surfacing the absolute path makes it unambiguous which file is production (the 2026-06-18
    # data-loss incident hinged on a process operating on this file from backend/).
    import os as _os
    log.info("Shelf database: %s", _os.path.abspath(settings.database_url.split("///", 1)[-1]))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_con, _record):  # noqa: ANN001
        """WAL + a busy timeout so the web service, scheduler, and shelfcli can read
        and write the same SQLite file concurrently without 'database is locked'."""
        cur = dbapi_con.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            # Wait out a write burst instead of erroring with 'database is locked'. With the
            # index crawl uncapped (unlimited daily budget), several writers (page store, chapter
            # backfill, cover cache) commit concurrently; 5s was occasionally exceeded during a
            # checkpoint / large-blob write, surfacing transient lock errors on background ticks.
            cur.execute("PRAGMA busy_timeout=15000")
            cur.execute("PRAGMA synchronous=NORMAL")
            # Read latency on the multi-GB DB is dominated by page IO while the crawler
            # writes concurrently. A large per-connection page cache + memory-mapped IO keep
            # hot pages resident so reads (and page switches in the reader) stay fast even
            # under heavy write load. wal_autocheckpoint caps WAL growth so checkpoints are
            # frequent+small rather than rare+stalling.
            # Page cache is PER-CONNECTION; with the threadpool + scheduler + crawler each holding
            # connections, 64 MB×N reached hundreds of MB to >1 GB. Trimmed to ~24 MB so even at the
            # 60-connection ceiling (pool_size 20 + max_overflow 40) worst-case page cache is
            # ~24 MB×60 ≈ 1.4 GB — bounded on the 8 GB box — while the 256 MB shared (OS-level) mmap
            # window, which actually dominates read latency on the multi-GB DB, keeps hot pages
            # resident so reads/page-switches stay fast (P4).
            cur.execute("PRAGMA cache_size=-24576")        # ~24 MB page cache per connection
            cur.execute("PRAGMA mmap_size=268435456")      # 256 MB memory-mapped read window (shared)
            cur.execute("PRAGMA wal_autocheckpoint=1000")  # checkpoint every ~4 MB of WAL
        except Exception:
            pass
        finally:
            cur.close()


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables if they do not yet exist (guarantees boot; Alembic also available)."""
    from . import models  # noqa: F401  (register mappers)

    _drop_stale_catalog_works()
    _drop_stale_catalog_categories()
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _ensure_indexes()
    # Create the race-hardening unique indexes. On a FRESH DB (incl. every test DB) there are no
    # collisions so this succeeds here; on an existing DB that predates them it may fail-and-log,
    # and boot_recover re-runs it AFTER dedupe_unique_collisions. Schema-only (no data writes), so
    # it's safe in the init_db path that read-only clients also call.
    enforce_unique_indexes()
    _migrate_reading_states_per_user()
    _ensure_fts()
    _check_schema_drift()  # ARCH-H1: loud safety net for a mapped column missing from the live DB


def apply_pending_restore() -> None:
    """Server-boot ONLY, run BEFORE any DB connection: if an admin staged a full-DB snapshot restore
    (a ``.shelf-restore-pending`` marker written by backups_store.request_db_restore), swap that
    snapshot in wholesale. The current DB is safety-copied first, so a restore is itself reversible.
    Best-effort + defensive: any validation failure leaves the live DB untouched."""
    if not _is_sqlite or ":memory:" in settings.database_url:
        return
    import os as _os
    import shutil as _shutil
    import time as _time
    from pathlib import Path

    db_path = Path(_os.path.abspath(settings.database_url.split("///", 1)[-1]))
    marker = db_path.parent / ".shelf-restore-pending"
    if not marker.exists():
        return
    try:
        snap = Path(marker.read_text().strip())
        # Confine to the DB directory + verify it's a real SQLite file before touching anything.
        if (not snap.is_file() or snap.resolve().parent != db_path.parent.resolve()
                or snap.name in ("shelf.db", "shelf.db-wal", "shelf.db-shm")):
            log.error("pending restore: invalid snapshot %r — ignoring", str(snap))
            marker.unlink(missing_ok=True)
            return
        with open(snap, "rb") as fh:
            if fh.read(16) != b"SQLite format 3\x00":
                log.error("pending restore: %s is not a SQLite file — ignoring", snap.name)
                marker.unlink(missing_ok=True)
                return
        engine.dispose()  # ensure no open handle on the file we're about to replace
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        safety = db_path.with_name(f"{db_path.name}.pre-restore-{stamp}.bak")
        if db_path.exists():
            _shutil.copy2(db_path, safety)
        # Drop the live DB's WAL/SHM (they belong to the OLD file; keeping them corrupts the new one).
        for suf in ("-wal", "-shm"):
            db_path.with_name(db_path.name + suf).unlink(missing_ok=True)
        # Copy to a sibling temp then atomically rename over the live path (same filesystem).
        tmp = db_path.with_name(db_path.name + ".restoring")
        _shutil.copy2(snap, tmp)
        _os.replace(tmp, db_path)
        marker.unlink(missing_ok=True)
        log.warning("RESTORED full DB from snapshot %s (previous DB saved as %s)",
                    snap.name, safety.name)
    except Exception:  # noqa: BLE001 — never let a restore attempt brick boot
        log.exception("pending restore failed — leaving the current DB in place")
        try:
            marker.unlink(missing_ok=True)  # don't loop the failed restore every boot
        except OSError:
            pass


def boot_recover() -> None:
    """Server-boot data maintenance: budget normalization + retired-source cleanup + WAL reclaim.

    Kept SEPARATE from ``init_db`` (which is schema-only) because read-only clients that share the
    DB — notably ``shelfcli`` — call ``init_db`` on every start to ensure the schema. Those clients
    must NOT run these data writes / WAL checkpoints against the live server DB: under the crawl's
    write bursts they would hit 'database is locked' and the client would fail to start. Only the
    server lifespan calls this, once, on boot."""
    _sync_schema_version()
    _recover_web_index_budget()
    _remove_retired_sources()
    _seed_library_membership()
    _backfill_adult_flags()
    _backfill_descriptions()
    # Race-hardening unique indexes: dedupe pre-existing collisions, then enforce. Server-only
    # (data writes don't belong in init_db, which read-only clients also call).
    dedupe_unique_collisions()
    enforce_unique_indexes()
    # Reclaim the WAL on boot: under the continuous crawl the -wal file can balloon (passive
    # autocheckpoint is starved by always-active readers) to multiple GB, which collapses write
    # throughput. At boot there are no other connections, so this fully truncates it.
    checkpoint_wal()


def _alembic_config():
    """A programmatic Alembic Config pointing at this project's migrations + the live DB URL, with
    NO ini file so loading it never reconfigures the app's logging (env.py only calls fileConfig when
    config_file_name is set). The DB URL is set on both the main option and the [alembic] section so
    env.py's online path (which reads the section) targets the live DB."""
    from pathlib import Path

    from alembic.config import Config

    url = str(engine.url)
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_section_option("alembic", "sqlalchemy.url", url)
    return cfg


def _sync_schema_version() -> None:
    """Make Alembic the schema VERSION authority + forward-migration applier on boot (F4.3).

    The full schema is still BUILT/maintained by ``create_all`` + ``_ensure_columns`` (the Alembic
    chain is incremental — early revisions assume a create_all baseline, e.g. they reference
    ``stock_items`` they never create — so it can't build from empty). What Alembic owns now is the
    version ledger: a DB already at full schema but UNSTAMPED (built by create_all) is STAMPED at
    head — we never replay the create_all-dependent revisions over an existing schema — and a stamped
    DB that's BEHIND head is UPGRADED, so a NEW revision auto-applies on the next boot. Best-effort:
    a failure here is logged and never blocks startup (create_all already built the schema)."""
    if not _is_sqlite and engine.url.get_backend_name() not in ("sqlite", "postgresql", "mysql"):
        return  # unknown backend — leave migration management to the operator
    try:
        from alembic import command
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        cfg = _alembic_config()
        head = ScriptDirectory.from_config(cfg).get_current_head()
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        if current == head:
            return
        if current is None:
            # Schema already built by create_all → record it as head without replaying revisions.
            command.stamp(cfg, "head")
            log.info("alembic: stamped existing schema at head %s", head)
        else:
            command.upgrade(cfg, "head")
            log.info("alembic: upgraded schema %s → %s", current, head)
    except Exception:  # noqa: BLE001 — schema tracking is best-effort; create_all already built it
        log.exception("alembic schema-version sync failed (continuing)")


_LIBRARY_SEED_KEY = "library_membership_seed_v1"
_ADULT_BACKFILL_KEY = "adult_flags_backfill_v1"
_DESC_BACKFILL_KEY = "description_clean_backfill_v1"


def _backfill_descriptions() -> None:
    """One-time: re-clean already-stored descriptions/synopses that carry raw HTML/markdown. The
    ``@validates`` hooks on Work.description / CatalogWork.synopsis / CatalogGroup.synopsis only fire
    on NEW writes, so existing rows need this sweep. Guarded by an app_settings sentinel — a no-op once
    run; safe on a fresh install (no matching rows)."""
    from sqlalchemy import or_, select, text

    from .models import AppSetting, CatalogGroup, CatalogWork, IndexedPage, Work
    from .textutil import clean_synopsis

    db = SessionLocal()
    try:
        if db.scalar(text("SELECT 1 FROM app_settings WHERE key = :k"), {"k": _DESC_BACKFILL_KEY}):
            return

        def _markup(col):  # cheap pre-filter for rows that might carry markup/entities
            return or_(col.like("%<%"), col.like("%**%"), col.like("%](%"),
                       col.like("%&#%"), col.like("%&amp;%"), col.like("%&lt;%"))

        n = 0
        for model, attr in ((Work, "description"), (CatalogWork, "synopsis"),
                            (CatalogGroup, "synopsis"), (IndexedPage, "description")):
            col = getattr(model, attr)
            for r in db.scalars(select(model).where(col.is_not(None), _markup(col))).all():
                cur = getattr(r, attr)
                cleaned = clean_synopsis(cur)
                if cleaned != cur:
                    setattr(r, attr, cleaned)  # re-runs the validator (idempotent)
                    n += 1
        db.add(AppSetting(key=_DESC_BACKFILL_KEY, value="done"))
        db.commit()
        log.info("description backfill: cleaned %s rows", n)
    except Exception:
        db.rollback()
        log.exception("description backfill failed")
    finally:
        db.close()


def _backfill_adult_flags() -> None:
    """One-time: flag existing catalog rows 18+ from their already-stored genres, then force a regroup
    so the groups recompute ``is_adult``. New rows are flagged at enrichment; new groups at regroup.
    Guarded by an app_settings sentinel — a no-op once run."""
    from sqlalchemy import select, text

    from .ingestion.catalog import taxonomy_is_adult
    from .ingestion.catalog_groups import _WATERMARK_KEY
    from .models import AppSetting, CatalogWork

    db = SessionLocal()
    try:
        if db.scalar(text("SELECT 1 FROM app_settings WHERE key = :k"), {"k": _ADULT_BACKFILL_KEY}):
            return
        n = 0
        # Only enriched rows carry genres; the rest stay False (and get flagged when enriched).
        for r in db.scalars(select(CatalogWork).where(CatalogWork.enriched_at.isnot(None))).all():
            if taxonomy_is_adult(r.extra) and not r.is_adult:
                r.is_adult = True
                n += 1
        # Force the next regroup tick to recompute group-level is_adult.
        db.execute(text("DELETE FROM app_settings WHERE key = :k"), {"k": _WATERMARK_KEY})
        db.add(AppSetting(key=_ADULT_BACKFILL_KEY, value="done"))
        db.commit()
        log.info("adult backfill: flagged %s catalog rows 18+", n)
    except Exception:
        db.rollback()
        log.exception("adult flags backfill failed")
    finally:
        db.close()


def _seed_library_membership() -> None:
    """One-time: the library became PER-USER (membership), so the previously-global works become
    the first admin's library — other users start empty. Hooking thereafter adds per-user
    membership. Gated by an app_settings sentinel; a no-op once seeded (or on a fresh install,
    where the hook flow creates memberships)."""
    from sqlalchemy import inspect

    insp = inspect(engine)
    need = ("library_items", "works", "users", "app_settings")
    if not all(insp.has_table(t) for t in need):
        return
    with engine.begin() as conn:
        if conn.execute(
            text("SELECT 1 FROM app_settings WHERE key = :k"), {"k": _LIBRARY_SEED_KEY}
        ).fetchone():
            return
        admin = conn.execute(
            text("SELECT id FROM users WHERE role = 'admin' AND is_active = 1 ORDER BY id LIMIT 1")
        ).fetchone() or conn.execute(text("SELECT id FROM users ORDER BY id LIMIT 1")).fetchone()
        if admin is None:
            return  # no users yet (pre-setup) — retry next boot once an admin exists
        # Re-flood guard: if the admin ALREADY has a library, the per-user migration has effectively
        # happened (or a restore re-created it) — DON'T sweep every Work in again. Without this, a
        # restore that wiped this sentinel would re-add operator-stocked + watched-folder works to the
        # admin's library, making stocked titles wrongly show "in library".
        if conn.execute(
            text("SELECT 1 FROM library_items WHERE user_id = :uid LIMIT 1"), {"uid": admin[0]}
        ).fetchone():
            conn.execute(text("INSERT OR IGNORE INTO app_settings (key, value) VALUES (:k, :v)"),
                         {"k": _LIBRARY_SEED_KEY, "v": '{"done": true, "skipped": "library populated"}'})
            return
        # Never seed operator-STOCK content into the library: a stocked file is a shared Work, not a
        # deliberate library addition. Stock files live under the configured stock_dir (which may be a
        # subfolder of a watched library path); exclude any Work whose local_path is inside it.
        sd = conn.execute(
            text("SELECT value FROM app_settings WHERE key = 'stock_dir'")
        ).fetchone()
        stock_prefix = None
        if sd and sd[0]:
            try:
                stock_prefix = (json.loads(sd[0]) if sd[0].strip().startswith('"') else sd[0]).rstrip("/") + "/"
            except Exception:  # noqa: BLE001
                stock_prefix = None
        sql = ("INSERT INTO library_items (user_id, work_id, added_at) "
               "SELECT :uid, w.id, CURRENT_TIMESTAMP FROM works w "
               "WHERE NOT EXISTS (SELECT 1 FROM library_items li "
               "                  WHERE li.user_id = :uid AND li.work_id = w.id)")
        params = {"uid": admin[0]}
        if stock_prefix:
            sql += " AND (w.local_path IS NULL OR w.local_path NOT LIKE :sp)"
            params["sp"] = stock_prefix + "%"
        conn.execute(text(sql), params)
        conn.execute(
            text("INSERT INTO app_settings (key, value) VALUES (:k, :v)"),
            {"k": _LIBRARY_SEED_KEY, "v": '{"done": true}'},
        )


def checkpoint_wal(mode: str = "TRUNCATE") -> None:
    """Force a WAL checkpoint so the -wal file stays bounded. Passive autocheckpoint only keeps
    the DB in sync; it never shrinks the file and gets starved under continuous read load, letting
    the WAL grow without bound (observed ~6 GB → 'database is locked' everywhere). A periodic
    TRUNCATE checkpoint (scheduler) + this boot call keep it small. Best-effort: returns busy
    (no-op) if a reader currently blocks truncation, so it never raises into the caller."""
    if not _is_sqlite:
        return
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(f"PRAGMA wal_checkpoint({mode})")
    except Exception:  # pragma: no cover — checkpointing is best-effort
        log.exception("wal checkpoint failed")


def _ensure_indexes() -> None:
    """Composite indexes that speed the hottest aggregate queries. The Jobs page's per-site
    page counts GROUP BY (site_id, status) over the large indexed_pages table — without this
    index that scan made the indexing section load noticeably slower than the rest."""
    from sqlalchemy import text

    stmts = [
        "CREATE INDEX IF NOT EXISTS ix_indexed_pages_site_status "
        "ON indexed_pages (site_id, status)",
        # COVERING indexes: the Jobs page sums word_count and maxes fetched_at per site.
        # Without these, SQLite scans the indexed_pages rows — which carry huge html/text
        # blobs — just to read two small columns (seconds of IO on a multi-GB DB). With the
        # column in the index, the aggregate is answered from the index alone.
        "CREATE INDEX IF NOT EXISTS ix_indexed_pages_site_words "
        "ON indexed_pages (site_id, word_count)",
        "CREATE INDEX IF NOT EXISTS ix_indexed_pages_site_fetched "
        "ON indexed_pages (site_id, fetched_at)",
        "CREATE INDEX IF NOT EXISTS ix_catalog_works_site ON catalog_works (site_id)",
        # The Index/catalog page lists the newest catalog rows (ORDER BY updated_at DESC LIMIT N).
        # Without this index SQLite scans + temp-sorts the whole table (slow as the catalog grows
        # to tens of thousands of rows); the index answers the top-N directly.
        "CREATE INDEX IF NOT EXISTS ix_catalog_works_updated ON catalog_works (updated_at)",
        # Discovery read paths: catalog_works.group_id link, group rows ordered by popularity per
        # media bucket, and the tag join behind every genre/theme row + the browse grid.
        "CREATE INDEX IF NOT EXISTS ix_catalog_works_group ON catalog_works (group_id)",
        # Enrichment picks unenriched rows popular-first.
        "CREATE INDEX IF NOT EXISTS ix_catalog_works_enrich "
        "ON catalog_works (enriched_at, popularity)",
        # The Index list ranks the whole catalog by popularity (find_rows: ORDER BY popularity DESC,
        # updated_at DESC LIMIT N). Without a popularity-leading index SQLite full-scans + temp-
        # filesorts tens of thousands of rows on every cache miss; this serves the top-N directly
        # (a reverse scan covers the DESC order).
        "CREATE INDEX IF NOT EXISTS ix_catalog_works_pop "
        "ON catalog_works (popularity, updated_at)",
        # Identity-based grouping/merge (K1): rows sharing a non-null identity_key are the same work.
        "CREATE INDEX IF NOT EXISTS ix_catalog_works_identity ON catalog_works (identity_key)",
        "CREATE INDEX IF NOT EXISTS ix_catalog_groups_pop "
        "ON catalog_groups (media_bucket, popularity_norm)",
        # The Index discovery rows rank each media CATEGORY (Manga/Manhua/Webtoon/…) by popularity.
        "CREATE INDEX IF NOT EXISTS ix_catalog_groups_label_pop "
        "ON catalog_groups (media_label, popularity_norm)",
        "CREATE INDEX IF NOT EXISTS ix_catalog_tags_kind_slug ON catalog_tags (kind, slug)",
        # The reader's per-work counts (total + fetched) and the scheduler's pending lookup
        # filter chapters by (work_id, fetch_status); without this they scan a work's whole
        # chapter set, which is slow under crawl write-contention (the "switching pages is
        # slow" symptom). 'index' is the chapter ordering used by the TOC + next/prev.
        "CREATE INDEX IF NOT EXISTS ix_chapters_work_status ON chapters (work_id, fetch_status)",
        "CREATE INDEX IF NOT EXISTS ix_chapters_work_index ON chapters (work_id, \"index\")",
        # Content-hash dedupe on import looks a Work up by the sha256 of its file bytes (13C).
        "CREATE INDEX IF NOT EXISTS ix_works_content_hash ON works (content_hash)",
        # Statistics page: COUNT of web-crawl-hooked catalog rows. provider='web_index' matches ~87%
        # of catalog_works, so the provider index forces a 200k-row lookup; a PARTIAL index over just
        # the hooked rows (a few thousand) answers the count from the index alone.
        "CREATE INDEX IF NOT EXISTS ix_catalog_works_web_hooked ON catalog_works (hooked_work_id) "
        "WHERE provider = 'web_index' AND hooked_work_id IS NOT NULL",
    ]
    with engine.begin() as conn:
        for s in stmts:
            try:
                conn.execute(text(s))
            except Exception:  # pragma: no cover — index creation is best-effort
                pass


def insert_or_reuse(db, obj, lookup):
    """Insert ``obj``; if a unique constraint trips (a concurrent writer beat us to it), return the
    EXISTING row instead. Returns (row, created). The insert runs inside a SAVEPOINT so a collision
    rolls back ONLY this insert — never the caller's wider in-progress transaction (a sync batch /
    multi-item queue must not be aborted by one duplicate). ``lookup`` is the select() that finds
    the row the constraint points at. NOTE: ``obj`` must be flushable as-is (all NOT NULL columns
    set) — it is flushed immediately inside the savepoint."""
    from sqlalchemy.exc import IntegrityError
    try:
        with db.begin_nested():     # savepoint: a flush error rolls this back, leaving the outer txn
            db.add(obj)
            db.flush()
        return obj, True
    except IntegrityError:
        # The context manager already rolled the savepoint back and the outer transaction is intact;
        # do NOT db.rollback() here (that would discard the caller's other pending work).
        return db.scalar(lookup), False


def dedupe_unique_collisions() -> None:
    """One-shot cleanup of rows that would collide with the race-hardening unique indexes
    (duplicates created by the historical check-then-insert races). Keeps the best row of each
    duplicate set, migrates/clears the rest. Idempotent; runs before enforce_unique_indexes.

      * stock_items.norm_key — keep the stocked one (else oldest); the rest are redundant
        queue entries for the same title.
      * crawl_jobs active (work_id, kind) — keep the running one (else oldest); duplicates
        double-pace the source for no benefit.
      * works (source_id, source_work_ref) — keep the row with the most chapters (else oldest),
        repoint library/shelf memberships at it, drop the spares (same file, same content).
    """
    from sqlalchemy import select, text

    db = SessionLocal()
    try:
        # stock_items: window-rank per norm_key, prefer status='stocked', then oldest.
        db.execute(text(
            "DELETE FROM stock_items WHERE id NOT IN ("
            " SELECT id FROM ("
            "  SELECT id, ROW_NUMBER() OVER (PARTITION BY norm_key"
            "   ORDER BY (status = 'stocked') DESC, id) AS rn FROM stock_items"
            " ) WHERE rn = 1)"
        ))
        # crawl_jobs: only ACTIVE duplicates of the same (work_id, kind) collide; prefer the
        # running one, then oldest. Terminal rows (done/failed) are history — untouched.
        db.execute(text(
            "DELETE FROM crawl_jobs WHERE status IN ('scheduled','running','paused')"
            " AND id NOT IN ("
            " SELECT id FROM ("
            "  SELECT id, ROW_NUMBER() OVER (PARTITION BY work_id, kind"
            "   ORDER BY (status = 'running') DESC, id) AS rn"
            "  FROM crawl_jobs WHERE status IN ('scheduled','running','paused')"
            " ) WHERE rn = 1)"
        ))
        db.commit()
        # works: duplicates carry user data (library/shelf placements) — migrate, don't just drop.
        from .models import Work
        dups = db.execute(text(
            "SELECT source_id, source_work_ref FROM works"
            " WHERE source_id IS NOT NULL AND source_work_ref IS NOT NULL"
            " GROUP BY source_id, source_work_ref HAVING COUNT(*) > 1"
        )).all()
        if dups:
            from .ingestion.stock import _migrate_work_links
            for sid, ref in dups:
                rows = db.scalars(
                    select(Work).where(Work.source_id == sid, Work.source_work_ref == ref)
                ).all()
                keep = max(rows, key=lambda w: (len(w.chapters), -w.id))
                for w in rows:
                    if w.id != keep.id:
                        _migrate_work_links(db, w.id, keep.id)   # repoints memberships, drops w
            db.commit()
    except Exception:  # noqa: BLE001 — cleanup must never block boot
        db.rollback()
        import logging
        logging.getLogger("shelf.db").exception("unique-collision dedupe failed")
    finally:
        db.close()


def enforce_unique_indexes() -> None:
    """Create the race-hardening UNIQUE indexes on an existing DB (fresh DBs get the table-level
    constraints from create_all). Best-effort per index: if an un-deduped collision remains the
    CREATE fails and is logged — the check-then-insert code paths still behave as before."""
    from sqlalchemy import text

    stmts = [
        # One queued stock item per title (model declares uq_stock_norm_key for fresh DBs).
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_norm_key ON stock_items (norm_key)",
        # One Work per source ref (see models.Work.__table_args__ for the rationale).
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_work_source_ref ON works (source_id, source_work_ref)"
        " WHERE source_id IS NOT NULL AND source_work_ref IS NOT NULL",
        # One ACTIVE crawl job per (work, kind). Scoped by kind — a work legitimately runs
        # backfill + descramble (+ refresh) at once, and descramble DEPENDS on a live backfill.
        # 'paused' counts as active so resume can't collide with a fresh schedule.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_crawl_active ON crawl_jobs (work_id, kind)"
        " WHERE status IN ('scheduled','running','paused')",
    ]
    with engine.begin() as conn:
        for s in stmts:
            try:
                conn.execute(text(s))
            except Exception:  # noqa: BLE001 — leftover dup → index skipped, behavior unchanged
                import logging
                logging.getLogger("shelf.db").warning(
                    "unique index not created (duplicates remain?): %s", s.split(" ON ")[0])


def _drop_stale_catalog_works() -> None:
    """The catalog gained provider columns + a nullable site_id (for integration entries).
    SQLite can't relax NOT NULL in place; the catalog is a derived cache (rebuilt from
    crawl + integration sync), so drop the pre-integration table and let create_all
    recreate it with the new schema."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("catalog_works"):
        return
    cols = {c["name"] for c in insp.get_columns("catalog_works")}
    if "provider" not in cols:
        with engine.begin() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM catalog_works")).scalar() or 0
            # NEVER drop a POPULATED catalog — it's a derived cache, but one that takes a very long
            # time (full re-crawl + re-ingest) to rebuild, so silent data loss is unacceptable. The
            # only DB this migration ever needed to fix was a tiny pre-integration one. If a large
            # legacy table somehow lacks 'provider', leave it and warn loudly rather than nuke it.
            if n > 100:
                log.warning("catalog_works lacks 'provider' but has %s rows — NOT dropping (would "
                            "destroy the derived catalog). Migrate it manually.", n)
                return
            conn.execute(text("DROP TABLE catalog_works"))


def _drop_stale_catalog_categories() -> None:
    """Discovery categories are now keyed by media_label (Manga/Manhua/Webtoon/…), not the coarse
    comic/text bucket — which needs a different UNIQUE constraint. catalog_categories is a derived
    cache (rebuilt every regroup tick), so drop the pre-media_label table and let create_all
    recreate it with the new schema."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("catalog_categories"):
        return
    cols = {c["name"] for c in insp.get_columns("catalog_categories")}
    if "media_label" not in cols:
        with engine.begin() as conn:
            # catalog_categories is cheap to rebuild (one regroup tick), so dropping it is fine —
            # but keep the guard symmetric/defensive in case it ever grows expensive.
            conn.execute(text("DROP TABLE catalog_categories"))


# Lightweight additive migrations for existing SQLite DBs (create_all won't add columns).
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    # render_js + config (a single entry — a duplicate "sources" key below would silently win).
    "sources": {"render_js": "BOOLEAN NOT NULL DEFAULT 0", "config": "JSON"},
    "reading_states": {
        "paragraph_index": "INTEGER NOT NULL DEFAULT 0", "user_id": "INTEGER",
        # Audiobook listening position (the row's work_id is the audio Work). last_chapter_id stays
        # NULL for audio rows, so they're excluded from /continue-reading.
        "audio_track": "INTEGER NOT NULL DEFAULT 0",
        "audio_pos_s": "REAL NOT NULL DEFAULT 0",
        "audio_updated_at": "DATETIME",
    },
    # Discovery signals for the Index page's popularity/genre/theme rows.
    "catalog_works": {
        "popularity": "FLOAT NOT NULL DEFAULT 0",
        "rating": "FLOAT",
        "rating_count": "INTEGER",
        "year": "INTEGER",
        "group_id": "INTEGER",
        "enriched_at": "DATETIME",
        "enrich_source": "VARCHAR(32)",
        "is_adult": "BOOLEAN NOT NULL DEFAULT 0",
        "identity_key": "VARCHAR(64)",
    },
    "catalog_groups": {"is_adult": "BOOLEAN NOT NULL DEFAULT 0"},
    # content_requests columns added after the create_all baseline (Wave D origin tags = migration 0036;
    # Watchlist release_date/rescan_queued_at = migration 0038) — registered here so an existing DB gets
    # them additively at boot and the schema-drift check passes (create_all won't ALTER an existing table).
    "content_requests": {
        "origin": "VARCHAR(16)",
        "origin_detail": "VARCHAR(255)",
        "release_date": "DATE",
        "rescan_queued_at": "DATETIME",
    },
    # What an operator stocking batch fetches: ebook | audiobook | both.
    "stock_jobs": {"variant": "VARCHAR(16) NOT NULL DEFAULT 'ebook'"},
    # HTTP cache validators for conditional-GET on crawl re-fetch (F04).
    # (etag/last_modified live in the merged indexed_pages block below — a separate key here would be
    # silently dropped by the duplicate-key footgun.)
    "works": {
        "total_chapters_expected": "INTEGER",
        "media_kind": "VARCHAR(16) NOT NULL DEFAULT 'text'",
        "local_path": "VARCHAR(1024)",
        "local_mtime": "FLOAT",
        "local_size": "INTEGER",
        "health": "VARCHAR(16) NOT NULL DEFAULT 'unknown'",
        "health_detail": "TEXT",
        "health_checked_at": "DATETIME",
        "last_checked_at": "DATETIME",
        "last_update_at": "DATETIME",
        "crawl_interval_s": "FLOAT",
        "crawl_daily_limit": "INTEGER",
        "crawl_window_start": "INTEGER",
        "crawl_window_end": "INTEGER",
        "crawl_count_today": "INTEGER NOT NULL DEFAULT 0",
        "crawl_day": "VARCHAR(10)",
        # Operator-paused crawling (deleted/paused a job) → no auto-revive until resumed.
        "crawl_paused": "BOOLEAN NOT NULL DEFAULT 0",
        # Hook from a later chapter (skip chapters the user already read elsewhere): chapters with
        # index < this are never created/gathered. 1 = from the beginning.
        "start_chapter": "INTEGER NOT NULL DEFAULT 1",
        # Series grouping for the library.
        "series": "VARCHAR(255)",
        "series_position": "FLOAT",
        # Stable canonical series identity (Project 2 / migration 0040): "hc:<id>" or "name:<norm>".
        "series_id": "VARCHAR(64)",
        # sha256 of imported file bytes → content-hash dedupe on re-import (13C).
        "content_hash": "VARCHAR(64)",
        # Cached audiobook playback manifest (probed via ffprobe on first /audio/manifest request).
        "audio_meta": "JSON",
        # Display metadata for the detail modal (Wave 5) — filled at hook + a provider backfill tick.
        "rating": "FLOAT",
        "rating_count": "INTEGER",
        "year": "INTEGER",
        "genres": "JSON",
        "narrator": "VARCHAR(255)",
        "publisher": "VARCHAR(255)",
        "identifiers": "JSON",
        "page_count": "INTEGER",
        "meta_enriched_at": "DATETIME",
        "meta_source": "VARCHAR(64)",
    },
    # List-import series options (migration 0042) — additive on the existing list_subscriptions table.
    "list_subscriptions": {
        "auto_series": "BOOLEAN NOT NULL DEFAULT 0",
        "auto_follow_series": "BOOLEAN NOT NULL DEFAULT 0",
        "to_stock": "BOOLEAN NOT NULL DEFAULT 0",
    },
    # When the descramble job last checked a captured comic chapter for scrambled pages
    # (NULL = unchecked; non-comic chapters stay NULL).
    "chapters": {"descrambled_at": "DATETIME"},
    # raw_checksum also rides Alembic 0033, but that revision is STAMP-skipped on create_all-built
    # DBs (never replayed), so an existing pre-0033 table could miss it forever — register it here so
    # init_db heals it. This is exactly the Alembic-only-column gap the ARCH-H1 drift net flags.
    "chapter_contents": {"raw_checksum": "VARCHAR(64)"},
    # Admin-set per-user cap on viewable Index media categories (NULL = inherit global default).
    # email/approval_status: self-registration recovery + approval gate (existing rows → approved).
    "users": {
        "allowed_categories": "JSON", "permissions": "JSON", "adult_categories": "JSON",
        "email": "VARCHAR(255)",
        "approval_status": "VARCHAR(16) NOT NULL DEFAULT 'approved'",
    },
    "user_settings": {
        "kindle_email": "VARCHAR(255)", "delivery_config": "JSON", "user_id": "INTEGER",
        # Per-user push-notification target (an Apprise URL → ntfy/Pushover/Telegram/… ).
        "apprise_url": "VARCHAR(2048)",
        # Per-user per-title default shelf map {str(work_id): shelf_id}.
        "work_default_shelves": "JSON",
    },
    # provider-specific settings (e.g. Goodreads shelf) + the user a Goodreads connection
    # belongs to (so its wishlist auto-hooks land in that user's library, not the operator's).
    "integrations": {"config": "JSON", "user_id": "INTEGER"},
    "queued_hooks": {
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        # Per-user auto-hook destination (which user's library + bookshelf it lands in).
        "user_id": "INTEGER",
        "target_shelf_id": "INTEGER",
        # Which format a companion missing-half want fetches: ebook | audiobook.
        "variant": "VARCHAR(16) NOT NULL DEFAULT 'ebook'",
    },
    "library_items": {
        # Highest chapter index already auto-sent to the member's Kindle (NULL = not yet
        # baselined; the first auto-kindle pass records the current ceiling without sending,
        # so enabling auto-kindle never mails the entire existing backlog).
        "auto_kindle_through": "INTEGER",
    },
    # An external Goodreads shelf name whose titles auto-hook onto this bookshelf, plus per-shelf
    # path monitoring (watch_path) + a send-to-email automation toggle.
    "bookshelves": {
        "goodreads_shelf": "VARCHAR(128)",
        "notify_email": "BOOLEAN NOT NULL DEFAULT 0",
        "watch_path": "VARCHAR(1024)",
    },
    # Per-shelf folder monitoring: which shelf/user a watched folder feeds.
    "watched_folders": {"shelf_id": "INTEGER", "user_id": "INTEGER"},
    # Named stocking batches: link existing stock items to the job that queued them.
    "stock_items": {"stock_job_id": "INTEGER"},
    # Run lease for the single-writer job lifecycle (see models.CrawlJob).
    "crawl_jobs": {"lease_token": "VARCHAR(36)", "lease_expires_at": "DATETIME"},
    # Download candidate cascade + post-download verification bookkeeping.
    "download_jobs": {
        "candidates": "JSON",
        "attempt": "INTEGER NOT NULL DEFAULT 0",
        "retries": "INTEGER NOT NULL DEFAULT 0",
        "release_key": "VARCHAR(255)",
        "verified": "BOOLEAN NOT NULL DEFAULT 0",
        "not_before": "DATETIME",
        "progress_mb_left": "FLOAT",
        "progress_at": "DATETIME",
    },
    "indexed_pages": {
        # HTTP cache validators for conditional-GET on crawl re-fetch (F04).
        "etag": "VARCHAR(256)",
        "last_modified": "VARCHAR(64)",
        "author": "VARCHAR(255)",
        "cover_url": "VARCHAR(1024)",
        "site_name": "VARCHAR(255)",
        "page_type": "VARCHAR(64)",
        "priority": "INTEGER NOT NULL DEFAULT 0",
        # Transient-failure retry bookkeeping (see models.IndexedPage).
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at": "DATETIME",
    },
    "index_sites": {
        # Stop-on-idle crawling: halt once this many pages in a row surface no NEW title,
        # instead of a hard page cap. 0 disables the idle stop (rely on max_pages only).
        "pages_since_new_title": "INTEGER NOT NULL DEFAULT 0",
        "stop_after_idle_pages": "INTEGER NOT NULL DEFAULT 0",
        "titles_found": "INTEGER NOT NULL DEFAULT 0",
        # Adaptive backoff when a site blocks/rate-limits us (see models.IndexSite).
        "consecutive_errors": "INTEGER NOT NULL DEFAULT 0",
        "cooldown_until": "DATETIME",
        # API-catalog ingest (comix.to): next API page to fetch (0/NULL = idle) + last full-pass
        # completion time, so a site whose catalog comes from a JSON API is paged incrementally
        # and refreshed periodically instead of HTML-crawled.
        "api_cursor": "INTEGER",
        "api_synced_at": "DATETIME",
        # Per-source media-kind allowlist (migration 0039).
        "allowed_media_kinds": "JSON",
    },
}


def _ensure_columns() -> None:
    from sqlalchemy import inspect, text

    from .models import Base

    # F4.4 guard: every additive-column table must be a REAL mapped table. A dict literal silently
    # drops a duplicate key (the "duplicate 'sources'" footgun noted above), and a typo'd/renamed
    # table name would otherwise just be skipped (has_table False) and the column never created —
    # both fail loudly here instead of silently diverging from the ORM schema.
    declared = set(Base.metadata.tables)
    unknown = [t for t in _ADDITIVE_COLUMNS if t not in declared]
    assert not unknown, f"_ADDITIVE_COLUMNS references unmapped table(s): {unknown}"

    insp = inspect(engine)
    with engine.begin() as conn:
        for table, columns in _ADDITIVE_COLUMNS.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    # table/name/ddl come from the _ADDITIVE_COLUMNS code constant (model metadata),
                    # never user input — DDL identifiers can't be parametrized.
                    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def schema_drift(eng=None, metadata=None) -> dict[str, list[str]]:
    """Mapped columns that exist in the ORM but are MISSING from the live DB (per existing table).
    Empty dict = in sync. The usual cause of a non-empty result is a new ``mapped_column`` added to
    a model but NOT to ``_ADDITIVE_COLUMNS`` — ``create_all`` won't ALTER an existing SQLite table,
    so the column never lands and the app errors the first time it queries it. Whole-table absence is
    a different (create_all-handled) case and is skipped here. (ARCH-H1.)"""
    from sqlalchemy import inspect

    from .models import Base

    eng = eng if eng is not None else engine
    md = metadata if metadata is not None else Base.metadata
    insp = inspect(eng)
    drift: dict[str, list[str]] = {}
    for name, table in md.tables.items():
        if not insp.has_table(name):
            continue
        db_cols = {c["name"] for c in insp.get_columns(name)}
        missing = [c.name for c in table.columns if c.name not in db_cols]
        if missing:
            drift[name] = missing
    return drift


def _check_schema_drift() -> None:
    """ARCH-H1 safety net, run at the end of ``init_db`` (after additive migrations apply). Fail HARD
    on a disposable/test DB so CI catches the drift pre-deploy; on a real DB log a loud ERROR but
    NEVER block boot — the running service must not be bricked by this check (and the same column
    would already error at query time, so logging loses nothing)."""
    from .safety import db_is_disposable

    drift = schema_drift()
    if not drift:
        return
    detail = "; ".join(f"{t}: {', '.join(cols)}" for t, cols in sorted(drift.items()))
    if db_is_disposable(str(engine.url)):
        raise AssertionError(
            f"schema drift — mapped column(s) missing from the DB (add to _ADDITIVE_COLUMNS): {detail}"
        )
    log.error(
        "SCHEMA DRIFT — mapped column(s) missing from the live DB (add them to _ADDITIVE_COLUMNS; "
        "the app may error when querying them): %s", detail,
    )


def _migrate_reading_states_per_user() -> None:
    """Make reading_states per-user: drop the legacy UNIQUE(work_id) index (it would
    block a second user from having progress on the same work) and add the composite
    UNIQUE(user_id, work_id). Idempotent."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("reading_states"):
        return
    with engine.begin() as conn:
        for idx in insp.get_indexes("reading_states"):
            if idx.get("unique") and idx.get("column_names") == ["work_id"]:
                # idx["name"] is a DB-introspected index name (not user input).
                # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
                conn.execute(text(f'DROP INDEX IF EXISTS "{idx["name"]}"'))
        names = {i["name"] for i in inspect(engine).get_indexes("reading_states")}
        if "uq_reading_user_work" not in names:
            # Dedupe any (user_id, work_id) collisions among NON-NULL user_ids first — a prior
            # per-user backfill could have claimed two legacy rows to the same (user, work), and
            # CREATE UNIQUE INDEX would then fail. Keep the highest-id (most recent) row per pair.
            # (NULL user_ids don't collide under SQLite's NULL-distinct rule, so they're untouched.)
            conn.execute(text(
                "DELETE FROM reading_states WHERE user_id IS NOT NULL AND id NOT IN "
                "(SELECT MAX(id) FROM reading_states WHERE user_id IS NOT NULL "
                " GROUP BY user_id, work_id)"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_reading_user_work "
                "ON reading_states (user_id, work_id)"
            ))


# Sentinel marking the one-time normalization of web_index to the unlimited daily budget.
_WEB_INDEX_UNLIMITED_KEY = "web_index_budget_unlimited_v1"


def _recover_web_index_budget() -> None:
    """Normalize web_index to an UNLIMITED daily budget and recover budget-stranded pages.

    The web_index daily request budget is now UNLIMITED (0) — the per-source request interval +
    adaptive backoff is the only throttle. But ``ensure_source`` only seeds a Source's budget on
    row CREATE, so older installs kept a positive auto-default (2000, briefly 50000) and the index
    crawler hit that cap constantly, marking *thousands* of pages permanently ``failed`` with
    "daily budget … exhausted" (the legacy path, before budget exhaustion became a pacing pause).
    This:

    1. ONCE (gated by an app_settings sentinel) forces web_index to the unlimited default,
       regardless of its current value — so every existing install moves to the new design.
       Gated so a positive cap the operator deliberately sets *afterwards* is never overwritten
       on later boots;
    2. EVERY boot (idempotent), re-queues pages that budget *pacing* stranded as ``failed`` back
       to ``pending`` so the crawl resumes and finishes them (a budget pause was never a real
       fetch failure).
    """
    from sqlalchemy import inspect, text

    from .ingestion.adapters.web_index import WebIndexAdapter

    new_budget = WebIndexAdapter.compliance.max_daily_requests  # 0 = unlimited
    insp = inspect(engine)
    with engine.begin() as conn:
        if insp.has_table("sources") and insp.has_table("app_settings"):
            already = conn.execute(
                text("SELECT 1 FROM app_settings WHERE key = :k"),
                {"k": _WEB_INDEX_UNLIMITED_KEY},
            ).fetchone()
            if not already:
                conn.execute(
                    text("UPDATE sources SET max_daily_requests = :new WHERE key = 'web_index'"),
                    {"new": new_budget},
                )
                conn.execute(
                    text("INSERT INTO app_settings (key, value) VALUES (:k, :v)"),
                    {"k": _WEB_INDEX_UNLIMITED_KEY, "v": '{"done": true}'},
                )
        if insp.has_table("indexed_pages"):
            conn.execute(
                text(
                    "UPDATE indexed_pages SET status = 'pending', attempts = 0, "
                    "next_attempt_at = NULL, last_error = NULL "
                    "WHERE status = 'failed' AND last_error LIKE '%daily budget%'"
                )
            )


# Source adapters retired from the app: their leftover Source rows are removed on boot so they
# don't linger as broken, un-actionable entries on the Sources page.
_RETIRED_SOURCE_KEYS = ("mangadex",)


def _remove_retired_sources() -> None:
    """Delete Source rows for retired adapters — but only when no Work references the source, so
    library content is never orphaned (the operator must delete those works first)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("sources"):
        return
    has_works = insp.has_table("works")
    with engine.begin() as conn:
        for key in _RETIRED_SOURCE_KEYS:
            row = conn.execute(
                text("SELECT id FROM sources WHERE key = :k"), {"k": key}
            ).fetchone()
            if row is None:
                continue
            if has_works and conn.execute(
                text("SELECT 1 FROM works WHERE source_id = :sid LIMIT 1"), {"sid": row[0]}
            ).fetchone():
                continue  # still referenced by library works — leave it
            conn.execute(text("DELETE FROM sources WHERE id = :sid"), {"sid": row[0]})


# Whether the connected SQLite build has FTS5 (graceful fallback to LIKE search if not).
fts_enabled = False


def _ensure_fts() -> None:
    """Create an external-content FTS5 index over indexed_pages (title + text).

    Kept in sync manually via index_fts_* helpers (no triggers, so the same code
    path works whether or not FTS5 is compiled in).
    """
    global fts_enabled
    if not settings.database_url.startswith("sqlite"):
        return
    from sqlalchemy import text

    with engine.begin() as conn:
        try:
            conn.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS fts_test_probe USING fts5(x)"))
            conn.execute(text("DROP TABLE IF EXISTS fts_test_probe"))
        except Exception:
            fts_enabled = False
            return
        # Drop a stale contentless table from an earlier build (snippet() needs content).
        stale = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='indexed_pages_fts'"
            )
        ).scalar()
        if stale and "content=''" in stale.replace('"', "'"):
            conn.execute(text("DROP TABLE indexed_pages_fts"))
        # Contentful (not content='') so snippet()/highlight() work for search results.
        # rowid is set explicitly to indexed_pages.id by the sync helpers below.
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS indexed_pages_fts USING fts5("
                "title, body, tokenize='unicode61 remove_diacritics 2')"
            )
        )
    fts_enabled = True


def index_fts_upsert(conn, page_id: int, title: str, body: str) -> None:
    """Re-index one page (delete-then-insert; rowid == indexed_pages.id)."""
    if not fts_enabled:
        return
    from sqlalchemy import text

    conn.execute(text("DELETE FROM indexed_pages_fts WHERE rowid = :id"), {"id": page_id})
    conn.execute(
        text("INSERT INTO indexed_pages_fts (rowid, title, body) VALUES (:id, :t, :b)"),
        {"id": page_id, "t": title or "", "b": body or ""},
    )


def index_fts_delete(conn, page_id: int) -> None:
    if not fts_enabled:
        return
    from sqlalchemy import text

    conn.execute(text("DELETE FROM indexed_pages_fts WHERE rowid = :id"), {"id": page_id})
