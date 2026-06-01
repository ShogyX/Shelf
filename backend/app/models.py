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
    reading_state: Mapped[ReadingState | None] = relationship(
        back_populates="work", uselist=False, cascade="all, delete-orphan"
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

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), unique=True, index=True)
    last_chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"), nullable=True)
    scroll_fraction: Mapped[float] = mapped_column(Float, default=0.0)
    # Index of the paragraph at the top of the viewport (robust across font/width changes).
    paragraph_index: Mapped[int] = mapped_column(Integer, default=0)
    chapters_read: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    work: Mapped[Work] = relationship(back_populates="reading_state")


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
    # pending | fetched | failed | skipped
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when the user "hooks" this page into the library as a Work.
    hooked_work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    site: Mapped[IndexSite] = relationship(back_populates="pages")


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
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
