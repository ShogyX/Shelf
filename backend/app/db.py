"""Database engine, session factory, and the declarative Base."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

settings = get_settings()

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


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

    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _ensure_fts()


# Lightweight additive migrations for existing SQLite DBs (create_all won't add columns).
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "sources": {"render_js": "BOOLEAN NOT NULL DEFAULT 0"},
    "reading_states": {"paragraph_index": "INTEGER NOT NULL DEFAULT 0"},
    "works": {
        "total_chapters_expected": "INTEGER",
        "media_kind": "VARCHAR(16) NOT NULL DEFAULT 'text'",
        "local_path": "VARCHAR(1024)",
        "local_mtime": "FLOAT",
        "local_size": "INTEGER",
    },
    "user_settings": {"kindle_email": "VARCHAR(255)", "delivery_config": "JSON"},
    "indexed_pages": {
        "author": "VARCHAR(255)",
        "cover_url": "VARCHAR(1024)",
        "site_name": "VARCHAR(255)",
        "page_type": "VARCHAR(64)",
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
