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


# Adapter keys for members-only sources that accept an access token (surfaced in the UI).
_AUTH_SOURCES = {"jnovel"}


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
    # Per-source settings + credentials (e.g. a members-only access token for J-Novel:
    # {"auth_token": "..."}). Secrets here are NEVER returned by the API (only has_auth).
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    works: Mapped[list[Work]] = relationship(back_populates="source")

    @property
    def has_auth(self) -> bool:
        """Whether a credential (e.g. J-Novel access token) is stored — surfaced instead of
        the secret itself."""
        cfg = self.config or {}
        return bool((cfg.get("auth_token") or "").strip())

    @property
    def supports_auth(self) -> bool:
        """Adapters that can use a members-only access token (the UI shows a credential field)."""
        return self.adapter_key in _AUTH_SOURCES


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
    # Hooked from a later chapter: chapters with index < this are never created/gathered (the user
    # already read them elsewhere). 1 = from the beginning.
    start_chapter: Mapped[int] = mapped_column(Integer, default=1)
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
    # Operator paused this work's crawling: the scheduler/reaper will NOT auto-create or revive
    # crawl jobs while True. Set by deleting/pausing a job; cleared by resume/retry or an explicit
    # "check for updates". This is what makes a deleted job STAY gone (no auto-resurrection).
    crawl_paused: Mapped[bool] = mapped_column(Boolean, default=False)
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
    # When the descramble job last checked this (captured comic) chapter for scrambled pages.
    # NULL = not yet checked; the job processes NULLs and stamps this. Non-comix/text stays NULL.
    descrambled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    kind: Mapped[str] = mapped_column(String(16))  # index | backfill | refresh | descramble
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
    # active | paused | done | failed | removed (soft-deleted: crawl stopped, content kept)
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
    # Adaptive backoff when a site pushes back (blocks / rate-limits / sustained errors): a
    # running count of consecutive fetch errors drives an escalating cooldown. While
    # cooldown_until is in the future the scheduler skips this site, then picks speed back up
    # once it clears and fetches succeed. This is a *pause*, not a stop — the site stays
    # "active" and resumes on its own (only the idle-stop above ends a crawl).
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # API-catalog ingest (e.g. comix.to): a JS SPA whose /browse only renders a slice, so its
    # catalog is paged from a JSON API instead. ``api_cursor`` is the next API page to fetch
    # (0/NULL = idle/complete); ``api_synced_at`` stamps the last full pass (drives periodic refresh).
    api_cursor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
    # Transient-failure retry: how many times this page has been attempted, and the earliest
    # time it may be retried (jittered backoff). A page is only marked permanently "failed"
    # after exhausting its attempts or on a non-retryable error (404/410/robots) — so a passing
    # network blip or temporary block no longer drops the page for good.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    # --- Discovery signals (powers the Index page's popularity/genre rows) ---
    # Raw source popularity signal (comix followsTotal / gutendex download_count / provider score).
    # Normalized to a cross-source 0..1 score on the group (CatalogGroup.popularity_norm).
    popularity: Mapped[float] = mapped_column(Float, default=0.0)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)        # 0..10 avg
    rating_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The persisted cross-source cluster this row belongs to (assigned by the regroup tick).
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_groups.id"), nullable=True, index=True
    )
    # When genre/theme enrichment last ran for this row + which strategy produced it.
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enrich_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    site: Mapped[IndexSite] = relationship()


class CatalogGroup(Base):
    """A logical work, clustered across the sources that carry it (comix + a novel site + …).

    Precomputed by the regroup tick (:mod:`app.ingestion.catalog_groups`) so the Index page's
    popularity/genre/theme rows are cheap indexed reads instead of re-running the O(n²) union-find
    on every request. Tags + the normalized popularity score live here (not on the member rows) so a
    genre row is inherently deduped — a work carried by two sources appears once."""

    __tablename__ = "catalog_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    norm_key: Mapped[str] = mapped_column(String(512), index=True, default="")
    # text | comic — clustering never crosses this (a novel and its manga adaptation stay separate).
    media_bucket: Mapped[str] = mapped_column(String(16), default="text", index=True)
    # Representative (richest member) display fields.
    title: Mapped[str] = mapped_column(String(512))
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    synopsis: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    media_label: Mapped[str] = mapped_column(String(16), default="Novel", index=True)
    chapters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Cross-source popularity, normalized to 0..1 (percentile within source+bucket) at write time.
    popularity_norm: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    # Dominant source domain of the representative — used for the Most-Popular diversity cap.
    source_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, default=1)
    hooked_work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    tags: Mapped[list[CatalogTag]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class CatalogTag(Base):
    """A genre/theme/demographic/format label on a :class:`CatalogGroup`. Rolled up from member
    rows during regroup so it's deduped at the work level. A genre row is a single indexed query:
    ``catalog_tags JOIN catalog_groups ORDER BY popularity_norm DESC LIMIT N``."""

    __tablename__ = "catalog_tags"
    __table_args__ = (
        UniqueConstraint("group_id", "kind", "slug", name="uq_catalog_tag"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_groups.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), index=True)  # genre | theme | demographic | format
    slug: Mapped[str] = mapped_column(String(96), index=True)
    label: Mapped[str] = mapped_column(String(96))

    group: Mapped[CatalogGroup] = relationship(back_populates="tags")


class CatalogCategory(Base):
    """Materialized summary of which tags are populous enough to be a browsable row/category.
    Rebuilt by the regroup tick; the Index page reads it to choose rows and the browse nav."""

    __tablename__ = "catalog_categories"
    __table_args__ = (
        # Keyed by media_label (Manga/Manhua/Webtoon/Comic/Novel/Book) so the same genre can be a
        # row in several categories — the comic/text bucket would merge Manga + Manhua counts.
        UniqueConstraint("kind", "slug", "media_label", name="uq_catalog_category"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)   # genre | theme
    slug: Mapped[str] = mapped_column(String(96), index=True)
    label: Mapped[str] = mapped_column(String(96))
    media_bucket: Mapped[str] = mapped_column(String(16), default="text", index=True)
    media_label: Mapped[str] = mapped_column(String(16), default="Novel", index=True)
    group_count: Mapped[int] = mapped_column(Integer, default=0)


class IndexBlock(Base):
    """An operator block: a URL or domain barred from the index. When the operator removes
    broken content, an entry is added here so the crawler won't re-discover/re-catalog it and
    it can't be hooked. Matched by exact normalized URL or by domain (covers any URL on it)."""

    __tablename__ = "index_blocks"
    __table_args__ = (UniqueConstraint("scope", "value", name="uq_index_block_scope_value"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(8), index=True)  # url | domain
    value: Mapped[str] = mapped_column(String(2048), index=True)  # defragged url, or domain
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)  # for display
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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
    # Provider-specific settings (e.g. Goodreads {"user_id":..,"shelf":"to-read"}).
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The user this connection belongs to (per-user Goodreads): its wishlist auto-hooks land in
    # this user's library + their goodreads_target shelf. NULL = legacy/operator-owned (→ admin).
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MetadataLink(Base):
    """Links a library Work to a metadata-provider entry (ranobedb/goodreads), making that
    provider the source of truth for the work's author/synopsis/cover/release count and the
    signal for new releases + related titles. One row per (work, provider)."""

    __tablename__ = "metadata_links"
    __table_args__ = (UniqueConstraint("work_id", "provider", name="uq_metalink_work_provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)  # ranobedb | goodreads
    ref: Mapped[str] = mapped_column(String(255))                  # provider entry id
    matched_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="auto")  # auto|confirmed|rejected
    # Latest release marker last seen (date/count) — a change means a new release dropped.
    release_marker: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)  # volumes|chapters
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class QueuedHook(Base):
    """A title the operator wants hooked once it's available in the index — from a related
    series (prequel/sequel) or a Goodreads shelf. A watcher matches it against the catalog
    and hooks it automatically when it appears (from an enabled source)."""

    __tablename__ = "queued_hooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    norm_key: Mapped[str] = mapped_column(String(512), index=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_kind: Mapped[str] = mapped_column(String(16), default="text")
    reason: Mapped[str] = mapped_column(String(32))   # related | goodreads
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)  # provider/origin
    relation: Mapped[str | None] = mapped_column(String(32), nullable=True)  # prequel|sequel|…
    related_work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    # Per-user auto-hook destination: whose library it lands in + which bookshelf (if any).
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    target_shelf_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookshelves.id"), nullable=True
    )
    # pending | hooked | skipped | failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # genuine auto-hook failures
    hooked_work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")  # admin | user
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Admin-set cap on which Index media categories this user may view (subset of
    # catalog.MEDIA_CATEGORIES). NULL = inherit the global default (AppSetting
    # 'default_user_categories'); that being absent = all categories. Admins are never restricted.
    allowed_categories: Mapped[list | None] = mapped_column(JSON, nullable=True)
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
    # Per-user push target — an Apprise URL (ntfy/Pushover/Telegram/Discord/…). Used by the
    # notify_on_add shelf automation. Returned by the settings API (it's not a secret like SMTP).
    apprise_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class LibraryItem(Base):
    """A user's membership of a (global, shared) Work in their personal library.

    Works + their crawl/chapters are SHARED across users (one crawl serves everyone); this row is
    the per-user "it's in my library". New users start with none. Hooking a title — including one
    already hooked by someone else — just adds a membership and never re-crawls or re-jobs it."""

    __tablename__ = "library_items"
    __table_args__ = (UniqueConstraint("user_id", "work_id", name="uq_library_user_work"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Highest chapter index already auto-sent to this member's Kindle (auto-kindle shelves).
    # NULL until the first auto-kindle pass baselines it to the current ceiling — so turning
    # auto-kindle on never floods the member with the whole existing backlog.
    auto_kindle_through: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Bookshelf(Base):
    """A user-defined shelf for organizing their library. A work can sit on 0+ shelves.

    Per-shelf automation toggles: ``auto_update`` (keep its works' chapters refreshed on interval),
    ``auto_kindle`` (auto-send newly gathered content to the user's Kindle), ``notify_on_add``
    (push a notification when a title is auto/queued-hooked onto this shelf). ``goodreads_target``
    marks the shelf as the destination for the user's Goodreads wishlist auto-hooks."""

    __tablename__ = "bookshelves"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_bookshelf_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    auto_update: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_kindle: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_on_add: Mapped[bool] = mapped_column(Boolean, default=False)
    goodreads_target: Mapped[bool] = mapped_column(Boolean, default=False)
    # An external Goodreads shelf/list name (e.g. "to-read", "currently-reading") whose titles
    # auto-hook onto THIS bookshelf, using the owner's per-user Goodreads connection. NULL = none.
    goodreads_shelf: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class BookshelfItem(Base):
    """A work placed on a bookshelf (implies it's in the shelf owner's library)."""

    __tablename__ = "bookshelf_items"
    __table_args__ = (UniqueConstraint("shelf_id", "work_id", name="uq_shelf_work"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shelf_id: Mapped[int] = mapped_column(ForeignKey("bookshelves.id"), index=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
