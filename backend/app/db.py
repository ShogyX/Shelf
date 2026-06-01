"""Database engine, session factory, and the declarative Base."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

settings = get_settings()

_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}
engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_con, _record):  # noqa: ANN001
        """WAL + a busy timeout so the web service, scheduler, and shelfcli can read
        and write the same SQLite file concurrently without 'database is locked'."""
        cur = dbapi_con.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
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
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _migrate_reading_states_per_user()
    _ensure_fts()


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
            conn.execute(text("DROP TABLE catalog_works"))


# Lightweight additive migrations for existing SQLite DBs (create_all won't add columns).
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "sources": {"render_js": "BOOLEAN NOT NULL DEFAULT 0"},
    "reading_states": {"paragraph_index": "INTEGER NOT NULL DEFAULT 0", "user_id": "INTEGER"},
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
    },
    "user_settings": {
        "kindle_email": "VARCHAR(255)", "delivery_config": "JSON", "user_id": "INTEGER",
    },
    "indexed_pages": {
        "author": "VARCHAR(255)",
        "cover_url": "VARCHAR(1024)",
        "site_name": "VARCHAR(255)",
        "page_type": "VARCHAR(64)",
        "priority": "INTEGER NOT NULL DEFAULT 0",
    },
}


def _ensure_columns() -> None:
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table, columns in _ADDITIVE_COLUMNS.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


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
                conn.execute(text(f'DROP INDEX IF EXISTS "{idx["name"]}"'))
        names = {i["name"] for i in inspect(engine).get_indexes("reading_states")}
        if "uq_reading_user_work" not in names:
            # NULL user_ids (legacy rows) don't collide under SQLite's NULL-distinct rule.
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_reading_user_work "
                "ON reading_states (user_id, work_id)"
            ))


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
