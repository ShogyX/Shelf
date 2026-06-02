"""SQLAlchemy ORM models — see plan §3 (Data model)."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    adapter_key: Mapped[str] = mapped_column(String(64))

    # Compliance declaration (operator-controllable; gates ingestion).
    license_basis: Mapped[str] = mapped_column(String(128), default="unknown")
    tos_permitted: Mapped[bool] = mapped_column(Boolean, default=False)
    robots_respected: Mapped[bool] = mapped_column(Boolean, default=True)
    # Render pages with a headless browser (JS-heavy sites / passive anti-bot challenges).
    render_js: Mapped[bool] = mapped_column(Boolean, default=False)
    min_request_interval_s: Mapped[float] = mapped_column(Float, default=5.0)
    max_daily_requests: Mapped[int] = mapped_column(Integer, default=500)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    works: Mapped[list[Work]] = relationship(back_populates="source")


class Work(Base):
    __tablename__ = "works"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    source_work_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title: Mapped[str] = mapped_column(String(512))
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True, default="en")
    status: Mapped[str] = mapped_column(String(16), default="ongoing")  # ongoing | complete
    hooked: Mapped[bool] = mapped_column(Boolean, default=False)
    hooked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_chapters_known: Mapped[int] = mapped_column(Integer, default=0)
    # Source-advertised total (for sequential crawls where the TOC isn't enumerable).
    total_chapters_expected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Reading-media kind hint for the reader ("text" | "comic").
    media_kind: Mapped[str] = mapped_column(String(16), default="text")
    # Completeness diagnosis (set by the diagnostics engine):
    #   unknown | ok | incomplete | no_chapters | unreachable
    health: Mapped[str] = mapped_column(String(16), default="unknown")
    health_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    health_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Update tracker: when this hooked title was last re-checked at its source, and when
    # new content (chapters or metadata) was last found.
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_update_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Per-title crawl policy (override source defaults for THIS title's backfill).
    # NULL = use the source default. Window hours are UTC 0–23 (inclusive start,
    # exclusive end; start==end or NULL = anytime).
    crawl_interval_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    crawl_daily_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crawl_window_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crawl_window_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Rolling per-UTC-day request counter for the daily cap.
    crawl_count_today: Mapped[int] = mapped_column(Integer, default=0)
    crawl_day: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Watched-local-folder provenance (NULL for non-local works). Used by folder sync
    # to detect added/changed/removed files without re-importing unchanged ones.
    local_path: Mapped[str | None] = mapped_column(String(1024), nullable=True, index=True)
    local_mtime: Mapped[float | None] = mapped_column(Float, nullable=True)
    local_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    source: Mapped[Source | None] = relationship(back_populates="works")
    chapters: Mapped[list[Chapter]] = relationship(
        back_populates="work", cascade="all, delete-orphan", order_by="Chapter.index"
    )
    # One reading state per (user, work) now that progress is per-user.
    reading_states: Mapped[list[ReadingState]] = relationship(
        back_populates="work", cascade="all, delete-orphan"
    )


class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("work_id", "index", name="uq_chapter_work_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    source_chapter_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(512), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # pending / fetched / failed / skipped
    fetch_status: Mapped[str] = mapped_column(String(16), default="pending")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapter_contents.id"), nullable=True
    )

    work: Mapped[Work] = relationship(back_populates="chapters")
    content: Mapped[ChapterContent | None] = relationship(
        foreign_keys=[content_id], cascade="all, delete-orphan", single_parent=True
    )


class ChapterContent(Base):
    __tablename__ = "chapter_contents"

    id: Mapped[int] = mapped_column(primary_key=True)
    chapter_id: Mapped[int] = mapped_column(Integer, index=True)
    format: Mapped[str] = mapped_column(String(8), default="html")  # html | md | text
    body: Mapped[str] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64), index=True)


class ReadingState(Base):
    __tablename__ = "reading_states"
    # Progress is per (user, work). user_id is nullable for legacy rows migrated to the
    # first admin at setup; the app always sets it.
    __table_args__ = (UniqueConstraint("user_id", "work_id", name="uq_reading_user_work"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    last_chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"), nullable=True)
    scroll_fraction: Mapped[float] = mapped_column(Float, default=0.0)
    # Index of the paragraph at the top of the viewport (robust across font/width changes).
    paragraph_index: Mapped[int] = mapped_column(Integer, default=0)
    chapters_read: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    work: Mapped[Work] = relationship(back_populates="reading_states")


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # index | backfill | refresh
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    # scheduled | running | paused | done | failed
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    work: Mapped[Work] = relationship()


class WatchedFolder(Base):
    """A local directory mapped as a reading-media source and watched for changes."""

    __tablename__ = "watched_folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recursive: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class IndexSite(Base):
    """A web location the user asked the app to index (auto-crawled within bounds)."""

    __tablename__ = "index_sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    root_url: Mapped[str] = mapped_column(String(2048))
    domain: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # active | paused | done | failed
    status: Mapped[str] = mapped_column(String(16), default="active")
    max_pages: Mapped[int] = mapped_column(Integer, default=200)
    max_depth: Mapped[int] = mapped_column(Integer, default=3)
    same_host_only: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Stop-on-idle crawling: how many consecutive fetched pages have surfaced no NEW catalog
    # title, the threshold at which to stop, and a running count of titles this site found.
    pages_since_new_title: Mapped[int] = mapped_column(Integer, default=0)
    stop_after_idle_pages: Mapped[int] = mapped_column(Integer, default=0)
    titles_found: Mapped[int] = mapped_column(Integer, default=0)

    pages: Mapped[list[IndexedPage]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )


class IndexedPage(Base):
    """One fetched (or pending) page within an IndexSite, full-text searchable."""

    __tablename__ = "indexed_pages"
    __table_args__ = (UniqueConstraint("site_id", "url", name="uq_indexed_page_site_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("index_sites.id"), index=True)
    url: Mapped[str] = mapped_column(String(2048), index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Preview metadata gathered from the page (so the reader can preview a discovered title).
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    site_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    page_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Sanitized, readable HTML (for native in-app reading).
    html: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Plain text (for FTS + snippets).
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    # Smart-crawl priority (work landing=2, listing=1, other=0): drained highest-first.
    priority: Mapped[int] = mapped_column(Integer, default=0)
    # pending | fetched | failed | skipped
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when the user "hooks" this page into the library as a Work.
    hooked_work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    site: Mapped[IndexSite] = relationship(back_populates="pages")


class CatalogWork(Base):
    """A literary work DISCOVERED while indexing — a catalog entry the user can search
    and then 'hook' into their library. Distinct from IndexedPage (page-granular): a
    CatalogWork is one book/novel/comic, identified by its landing/TOC URL, and may be
    one of several sources for the same title (grouped by norm_key)."""

    __tablename__ = "catalog_works"
    __table_args__ = (UniqueConstraint("site_id", "work_url", name="uq_catalog_site_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Where this entry came from: "web_index" (crawled site) | "readarr" | "kapowarr".
    provider: Mapped[str] = mapped_column(String(32), default="web_index", index=True)
    # External id for integration entries (e.g. Readarr bookId / Kapowarr volumeId).
    provider_ref: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    integration_id: Mapped[int | None] = mapped_column(
        ForeignKey("integrations.id"), nullable=True, index=True
    )
    # Provider-specific payload (grab params: foreignId, root folder, profiles, …).
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # NULL for integration entries (those come from a connected service, not a crawl).
    site_id: Mapped[int | None] = mapped_column(
        ForeignKey("index_sites.id"), nullable=True, index=True
    )
    domain: Mapped[str] = mapped_column(String(255), index=True)
    work_url: Mapped[str] = mapped_column(String(2048))
    # Normalized title key for cross-site grouping/dedup of the same work.
    norm_key: Mapped[str] = mapped_column(String(512), index=True, default="")
    title: Mapped[str] = mapped_column(String(512))
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    synopsis: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True, default="en")
    media_kind: Mapped[str] = mapped_column(String(16), default="text")
    kind: Mapped[str] = mapped_column(String(16), default="work")  # how it was classified
    # Counts: what the source advertises vs. how many chapter links we enumerated.
    chapters_advertised: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chapters_listed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Discovery health: unknown | ok | no_chapters | incomplete | unreachable
    health: Mapped[str] = mapped_column(String(16), default="unknown")
    health_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set once this catalog entry has been hooked into the library.
    hooked_work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    site: Mapped[IndexSite] = relationship()


class Integration(Base):
    """A connected library manager (Readarr for books/novels, Kapowarr for comics).

    Shelf reads its library + metadata to fill the catalog and can map its download
    root folders as watched folders so pulled files import automatically."""

    __tablename__ = "integrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # readarr | kapowarr
    name: Mapped[str] = mapped_column(String(128))
    base_url: Mapped[str] = mapped_column(String(512))
    api_key: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Primary download/library root (auto-discovered; mappable as a watched folder).
    root_folder: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    quality_profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Auto-create watched folders for this service's root folders.
    auto_map_folders: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")  # admin | user
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AppSetting(Base):
    """A global (not per-user) key/value app setting, e.g. indexing crawl defaults."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    # One settings row per user (nullable for the legacy global row, claimed at setup).
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, unique=True, index=True
    )
    # system | light | dark | sepia
    theme: Mapped[str] = mapped_column(String(16), default="system")
    reader_prefs: Mapped[dict] = mapped_column(JSON, default=dict)
    kindle_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # SMTP + personal-email delivery config (operator-set via UI). Password stored here;
    # never returned by the API. Keys: smtp_host, smtp_port, smtp_username, smtp_password,
    # smtp_from, smtp_security (none|starttls|ssl), email_to.
    delivery_config: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
