"""SQLAlchemy ORM models — see plan §3 (Data model)."""
from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from .db import Base
from .textutil import clean_synopsis


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
    # F07: partial index serving the cover-cache localize scan (cover_url LIKE 'http%').
    __table_args__ = (
        Index("ix_works_cover_url_remote", "cover_url",
              sqlite_where=text("cover_url LIKE 'http%'")),
    )
    # NOTE: the race-hardening uniqueness on (source_id, source_work_ref) is NOT declared here —
    # SQLite bakes a table-level UniqueConstraint as an UNDROPPABLE auto-index, which blocks the
    # dedupe-before-enforce migration path (and tests). It's created as an explicit, droppable
    # PARTIAL unique INDEX by db.enforce_unique_indexes() (run from init_db + boot_recover), which
    # exempts NULL refs. See db.py for the rationale.

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
    # Series membership (from the catalog metadata) so the library can group a series into one
    # entry, ordered by series_position (fractional positions exist for novellas, e.g. 2.5).
    series: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    series_position: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Stable canonical series identity (Project 2): "hc:<id>" from Hardcover's series resolution, else
    # "name:<norm>" fallback. Lets the library/dedup match a series by id rather than its free-text
    # name — so two same-named series don't collide and an owned volume whose catalog title drifted is
    # still recognized as in-series (S-DUP-2/S-DUP-3).
    series_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
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
    # sha256 of the imported FILE BYTES (local/uploaded media). Lets re-import dedupe a renamed copy
    # or the same book in another format/path to the SAME Work (update-in-place) instead of creating
    # a duplicate — the path/filename is not a stable identity (13C). NULL for remote-crawled works.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Audiobook playback metadata (media_kind=="audio"): the probed manifest — tracks (duration/mime/
    # native) + chapters + total duration + the source mtime it was probed at — cached as JSON so the
    # player's manifest endpoint only shells out to ffprobe on a cache miss / file change. NULL until
    # first probed (and for non-audio works).
    audio_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Display metadata (premium detail modal) — filled from the catalog row at hook time and topped
    # up by the provider backfill tick (metadata_sync.backfill_work_metadata). genres/identifiers are
    # JSON (list[str] of labels / {scheme: value}); meta_enriched_at gates the backfill sweep.
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)        # 0–10 convention
    rating_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)         # first publication year
    genres: Mapped[list | None] = mapped_column(JSON, nullable=True)
    narrator: Mapped[str | None] = mapped_column(String(255), nullable=True)  # audiobook narrator
    publisher: Mapped[str | None] = mapped_column(String(255), nullable=True)
    identifiers: Mapped[dict | None] = mapped_column(JSON, nullable=True)     # {isbn:[...], anilist:..}
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta_enriched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    meta_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    @validates("description")
    def _clean_description(self, _key, value):
        # Single chokepoint: every assignment to description (from any adapter/provider/catalog path)
        # is normalized to plain text, so raw HTML/markdown can never reach the (plain-text) UI.
        return clean_synopsis(value)

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
    # Checksum of the sanitized, PRE-localize content (before remote <img src> are rewritten to
    # local cache paths). Lets a refresh detect "content unchanged" and skip re-localizing — and
    # thus re-fetching — every image. NULL on rows stored before this column existed; they
    # re-localize once on the next refresh, populating it. ``checksum`` (post-localize) is unchanged.
    raw_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)


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
    # Audiobook listening position (when this row's work_id is an audio Work). audio_updated_at marks
    # rows that have audio progress (drives /continue-listening); last_chapter_id stays NULL for them.
    audio_track: Mapped[int] = mapped_column(Integer, default=0)
    audio_pos_s: Mapped[float] = mapped_column(Float, default=0.0)
    audio_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    # Run lease (single-writer discipline): the runner stamps a fresh token + expiry on pick-up and
    # RENEWS it as it makes progress; it abandons its run the moment the token no longer matches.
    # The reaper may revive a "running" job ONLY once the lease has expired — and bumps the token so
    # the (possibly still-alive) abandoned coroutine's later commits become no-ops instead of
    # clobbering the revived run. This closes the two-writer race on job status/cursor.
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    # When this folder monitors a bookshelf's mapped path: imported works are placed on this shelf
    # (in this user's library) and the shelf's automation events fire on discovery. NULL = a plain
    # operator/integration folder (no per-shelf placement).
    shelf_id: Mapped[int | None] = mapped_column(ForeignKey("bookshelves.id"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
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
    # Per-source media-kind allowlist (subset of {"text","comic"}). NULL/[] = serves all kinds; when
    # set, this site only contributes catalog members of those kinds to acquisition matching — so a
    # novels-only crawl source can't false-match a comic (and vice-versa).
    allowed_media_kinds: Mapped[list | None] = mapped_column(JSON, nullable=True)

    pages: Mapped[list[IndexedPage]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )


class IndexedPage(Base):
    """One fetched (or pending) page within an IndexSite, full-text searchable."""

    __tablename__ = "indexed_pages"
    __table_args__ = (
        UniqueConstraint("site_id", "url", name="uq_indexed_page_site_url"),
        # F07: partial index serving the cover-cache localize scan (cover_url LIKE 'http%').
        Index("ix_indexed_pages_cover_url_remote", "cover_url",
              sqlite_where=text("cover_url LIKE 'http%'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("index_sites.id"), index=True)
    url: Mapped[str] = mapped_column(String(2048), index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    @validates("description")
    def _clean_description(self, _key, value):
        return clean_synopsis(value)
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
    # HTTP cache validators captured on the last successful fetch, replayed as If-None-Match /
    # If-Modified-Since on re-fetch so an UNCHANGED page returns an empty 304 instead of a full
    # re-download + re-parse (F04 — the ~12h discovery-refresh re-crawl is the main beneficiary).
    etag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    __table_args__ = (
        UniqueConstraint("site_id", "work_url", name="uq_catalog_site_url"),
        # F07: partial index serving the cover-cache localize scan (cover_url LIKE 'http%').
        Index("ix_catalog_works_cover_url_remote", "cover_url",
              sqlite_where=text("cover_url LIKE 'http%'")),
    )

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

    @validates("synopsis")
    def _clean_synopsis(self, _key, value):
        return clean_synopsis(value)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True, default="en")
    media_kind: Mapped[str] = mapped_column(String(16), default="text")
    kind: Mapped[str] = mapped_column(String(16), default="work")  # how it was classified
    # 18+ / adult content (an explicit-adult genre or a provider adult flag) — drives content gating.
    is_adult: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
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
    # Stable cross-instance/cross-source identity (e.g. "anilist:12345", "isbn:9780…", "olid:OL…W",
    # "gb:abc", or a provider_ref). When two rows carry the SAME identity_key they are the SAME work
    # regardless of title — the deterministic merge key that title normalization can't reconcile
    # (romaji vs English, native-only, subtitle-on-one-source). Also the handle for a cheap
    # fetch-by-id on re-enrich instead of a fresh title search. (K1)
    identity_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
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
    # F07: partial index serving the cover-cache localize scan (cover_url LIKE 'http%').
    __table_args__ = (
        Index("ix_catalog_groups_cover_url_remote", "cover_url",
              sqlite_where=text("cover_url LIKE 'http%'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    norm_key: Mapped[str] = mapped_column(String(512), index=True, default="")
    # text | comic — clustering never crosses this (a novel and its manga adaptation stay separate).
    media_bucket: Mapped[str] = mapped_column(String(16), default="text", index=True)
    # Representative (richest member) display fields.
    title: Mapped[str] = mapped_column(String(512))
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    synopsis: Mapped[str | None] = mapped_column(Text, nullable=True)

    @validates("synopsis")
    def _clean_synopsis(self, _key, value):
        return clean_synopsis(value)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    media_label: Mapped[str] = mapped_column(String(16), default="Novel", index=True)
    chapters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 18+ if ANY member is adult — rolled up at regroup; drives the Index 18+ content gate.
    is_adult: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
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
    """A connected external service. Several roles share this row, distinguished by ``kind``:

    - library managers (Readarr books/novels, Kapowarr comics) — Shelf reads their library +
      metadata into the catalog and can map their download roots as watched folders;
    - the acquisition pipeline (Prowlarr indexer search + SABnzbd usenet downloader) — driven by
      the matching engine + download orchestrator, not the library-sync path;
    - metadata providers (ranobedb/googlebooks/anilist/novelupdates/goodreads) — source of truth
      for author/synopsis/cover/release signals (goodreads is per-user via ``user_id``)."""

    __tablename__ = "integrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    # readarr|kapowarr | prowlarr|sabnzbd | ranobedb|googlebooks|anilist|novelupdates|goodreads
    kind: Mapped[str] = mapped_column(String(32), index=True)
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
    # Which format to fetch: "ebook" (default) or "audiobook" — set when a companion app (Storyteller/
    # Audiobookshelf) wants the missing half of a read-along.
    variant: Mapped[str] = mapped_column(String(16), default="ebook")
    reason: Mapped[str] = mapped_column(String(32))   # related | goodreads | storyteller | audiobookshelf
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


class DownloadJob(Base):
    """One acquisition through the usenet pipeline: a matched catalog book → an NZB handed to
    SABnzbd → imported into the library when the download completes.

    Status: queued (sent to SAB) → downloading → completed (SAB finished, not yet imported) →
    imported (linked to a Work + added to the requester's library) | failed."""

    __tablename__ = "download_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The catalog book this grab is for (its hooked_work_id is set once imported).
    catalog_work_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_works.id"), nullable=True, index=True
    )
    # Whose library it lands in + which shelf (per-user / shelf auto-fetch). NULL = operator/admin.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    target_shelf_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookshelves.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(512))            # book title (display)
    release_title: Mapped[str | None] = mapped_column(String(1024), nullable=True)  # grabbed release
    indexer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    fmt: Mapped[str | None] = mapped_column(String(16), nullable=True)
    nzo_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sab_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # queued | downloading | completed | retry | imported | failed
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    storage_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)  # SAB-reported dir
    work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    grab_kind: Mapped[str] = mapped_column(String(8), default="manual")  # manual | auto
    # Candidate cascade: the ranked list of releases to try (serialized candidate dicts), the index
    # currently being attempted, and the current candidate's stable broken-tracking key. When a
    # download fails or fails content verification, the orchestrator marks that candidate broken and
    # advances to the next — so a wrong/dead link is replaced automatically, not just abandoned.
    candidates: Mapped[list | None] = mapped_column(JSON, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    # Transient-failure retry count: bumped each time the open-library endpoint is blocked/unreachable
    # while fetching this job. The job stays queued (with growing `not_before` backoff) until it
    # succeeds, the endpoint resolves, or this hits the retry cap — then it's marked failed.
    retries: Mapped[int] = mapped_column(Integer, default=0)
    release_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # When set (status == "deferred"), the grab is held back — the chosen release hit its
    # per-listing daily download cap — and the poll tick re-enqueues it once this time passes.
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # SAB stall detection: the queue slot's remaining MB at the last poll + when it last CHANGED.
    # A download whose mb_left hasn't moved for too long (wedged/no peers) is stalled and advanced to
    # the next candidate rather than tying up the job (and its piggyback group) for the 12h age limit.
    progress_mb_left: Mapped[float | None] = mapped_column(Float, nullable=True)
    progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StockJob(Base):
    """A named operator stocking batch — a group of :class:`StockItem`s queued together so the admin
    can name it, open it to see its titles + progress + stats, and monitor it for issues. The items
    do the actual work; this row carries the name and a snapshot of the selection it came from."""

    __tablename__ = "stock_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    # Snapshot of the selection (for display): media category, genre/theme dimension+value, sort.
    media_category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    dimension: Mapped[str | None] = mapped_column(String(16), nullable=True)
    value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sort: Mapped[str] = mapped_column(String(16), default="popularity")
    requested: Mapped[int] = mapped_column(Integer, default=0)  # how many groups matched at creation
    # What this batch stocks: the ebook, the audiobook (audio categories → separate path), or both.
    variant: Mapped[str] = mapped_column(String(16), default="ebook")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class StockItem(Base):
    """An operator-stocked catalog work: pre-fetched via Prowlarr/SABnzbd into the stock directory so
    it's instantly available (a shared, hooked Work) when any user acquires it. One row per logical
    work (norm_key). A background worker walks ``pending`` rows, searches usenet, and grabs them; the
    resulting download imports into the stock dir and flips the row to ``stocked``."""

    __tablename__ = "stock_items"
    # Uniqueness on norm_key is enforced as an explicit, droppable INDEX (uq_stock_norm_key) by
    # db.enforce_unique_indexes(), NOT a table-level constraint here: SQLite would bake the latter
    # as an undroppable auto-index, blocking the dedupe-before-enforce path on a live DB that
    # predates the constraint (the production DB never had it enforced). See db.py.

    id: Mapped[int] = mapped_column(primary_key=True)
    # The named batch this item belongs to (NULL for items queued before stock jobs existed).
    stock_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_jobs.id"), nullable=True, index=True
    )
    norm_key: Mapped[str] = mapped_column(String(512), index=True)
    # The representative catalog row used for the usenet search (the group's rep the user clicks).
    catalog_work_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_works.id"), nullable=True, index=True
    )
    work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    download_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("download_jobs.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(512))
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_label: Mapped[str] = mapped_column(String(16), default="Book")        # fine badge
    media_category: Mapped[str] = mapped_column(String(24), default="Book", index=True)
    popularity_norm: Mapped[float] = mapped_column(Float, default=0.0, index=True)  # snapshot for sort
    # pending | searching | downloading | stocked | unavailable | failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    stocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CompanionPush(Base):
    """Tracks a (companion integration, Work, format) that Shelf has pushed to Audiobookshelf /
    Storyteller, so the push tick is idempotent (never re-creates the same book). ``external_ref`` is
    the remote id (Storyteller book uuid; unused for ABS, which is folder-scanned)."""

    __tablename__ = "companion_pushes"
    __table_args__ = (
        UniqueConstraint("integration_id", "work_id", "fmt", name="uq_companion_push"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_id: Mapped[int] = mapped_column(ForeignKey("integrations.id"), index=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    fmt: Mapped[str] = mapped_column(String(8))                 # "ebook" | "audio"
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pushed")  # pushed | aligned | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class BrokenRelease(Base):
    """A release (NZB) that failed to download or verify — recorded so the matcher never tries it
    again. Keyed by a stable release identity (the indexer GUID when present, else a hash of the
    download URL). Marked when SAB reports a failed download (corrupt / missing blocks) or when a
    completed download fails post-download content verification (turned out to be the wrong book)."""

    __tablename__ = "broken_releases"

    id: Mapped[int] = mapped_column(primary_key=True)
    release_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    release_title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ContentRequest(Base):
    """A per-TITLE ledger of content that was REQUESTED but NOT FOUND across the acquisition routes
    (usenet pipeline + open-library/libgen). One row per logical title (clustered by ``norm_key`` +
    ``media_bucket`` — the same identity ``acquire``/``downloads`` dedupe on). Complementary to
    :class:`BrokenRelease` (which is per-RELEASE): this records that the whole TITLE couldn't be
    obtained, so the app can GATE further searches/grabs/stocking for known-unavailable titles and
    RE-CHECK them on a periodic, jittered cadence instead of hammering services every request.

    Requester attribution lives in :class:`ContentRequestRequester` (a title can be wanted by several
    users; a NULL requester is a system/stock request)."""

    __tablename__ = "content_requests"
    __table_args__ = (
        UniqueConstraint("norm_key", "media_bucket", name="uq_content_request_cluster"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Cluster key — same norm_key the catalog/dedup uses, split by media bucket (text | comic).
    norm_key: Mapped[str] = mapped_column(String(512), index=True)
    media_bucket: Mapped[str] = mapped_column(String(16), default="text")
    # Representative catalog row, used to RE-ACQUIRE the title on a periodic re-check.
    catalog_work_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_works.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(512))
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # open | searching | unavailable | resolved | planned (provider release date in the FUTURE — not
    # yet searched; the re-evaluation sweep flips it to "open" + searches once release_date passes).
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    # Why the last attempt failed: no_match | all_broken | rate_limited | blocked | unverified |
    # timeout | error (free-string enum, mirrors how the routes describe their exhaustion).
    failure_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the periodic re-check tick should next try this title again (indexed — the tick selects
    # due rows). Jittered so a batch marked unavailable together doesn't all come due at once.
    next_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A Planned title's provider release date (status=="planned"); the re-evaluation sweep flips the
    # row to "open" + searches once this date passes. NULL/past = released (never gates a fetch).
    release_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Mass-rescan queue marker: set by POST /missing/rescan; the rescan_drain_tick picks the oldest
    # queued rows, force-re-acquires them SEQUENTIALLY, and clears this as each is processed.
    rescan_queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # How this row entered the ledger: NULL/"request" = a direct request · "series" = a sibling
    # auto-requested by the auto-series hook (origin_detail = the series name). Surfaced on the Wanted
    # page so an auto-pulled sibling reads as "from series …" rather than an unexplained extra row.
    origin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    origin_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ContentRequestRequester(Base):
    """Who asked for a missing title (the join table behind a clean "my missing" query). A NULL
    ``user_id`` is a system/stock request. UNIQUE(request_id, user_id) so re-requesting is idempotent
    while still letting multiple users want the same title."""

    __tablename__ = "content_request_requesters"
    __table_args__ = (
        UniqueConstraint("request_id", "user_id", name="uq_content_request_requester"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("content_requests.id"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WorkSourceSearch(Base):
    """Per-(content request, durable download source) search state — the Wave B fine-grained gate.

    The title-level :class:`ContentRequest` gate is coarse: once a title is "unavailable" it gates
    EVERY route. This child row tracks the search state of ONE durable download source (torrent /
    pipeline / libgen) for the title, so a transient Prowlarr outage that blocks the usenet search
    doesn't permanently lock out the title across all routes, and a per-source re-check can re-search
    only the source that's due. One row per (content_request_id, source). Web-index/readarr/kapowarr
    are row-existence checks, not durable searches — they get no row here.

    status: pending (never searched / reset) | searching (leased, in flight) | no_match (searched,
    nothing found — terminal) | exhausted (candidates tried, all broke — terminal) | unavailable
    (search backend was unreachable — retried at next_retry_at) | matched (a job/hook started) |
    skipped (another source imported the title → this one's queued search is moot, R20).

    Lease (lease_token/leased_at) mirrors :class:`CrawlJob`: a CAS UPDATE claims a row for one
    searcher so the retry tick and a live acquire never double-search the same source."""

    __tablename__ = "work_source_searches"
    __table_args__ = (
        UniqueConstraint("content_request_id", "source", name="uq_work_source_search"),
        Index("ix_work_source_search_next_retry", "next_retry_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    content_request_id: Mapped[int] = mapped_column(
        ForeignKey("content_requests.id"), index=True
    )
    source: Mapped[str] = mapped_column(String(32))   # torrent | pipeline | libgen
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # CAS run lease (mirrors CrawlJob): a searcher stamps a token + leased_at to claim the row; a
    # stale lease (leased_at older than the reap window) may be re-claimed by the retry tick.
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class SourceAttempt(Base):
    """Append-only record of every durable-source search actually issued — one row per search of a
    (source). Powers "is source S available now / when does it next free up" against an opt-in
    per-source daily cap (``Integration.config.max_daily_requests``). Modeled on :class:`UsenetGrab`
    + ``downloads._grab_blocked_until``."""

    __tablename__ = "source_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)   # torrent | pipeline | libgen
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class UsenetGrab(Base):
    """Append-only ledger of every NZB actually handed to SABnzbd, keyed by the release's stable
    identity. Used to enforce a per-listing daily download cap: a release already grabbed N times in
    the last 24h is held back (the grab is deferred) rather than hammering the indexer/usenet account
    with duplicate pulls of the same listing."""

    __tablename__ = "usenet_grabs"

    id: Mapped[int] = mapped_column(primary_key=True)
    release_key: Mapped[str] = mapped_column(String(255), index=True)
    nzo_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class VtSubmission(Base):
    """Append-only ledger of every SUCCESSFUL VirusTotal hash lookup (a clone of UsenetGrab). Used to
    enforce VirusTotal's free-tier quota DURABLY across restarts — the in-memory ratelimit spacer
    can't back a 500/day counter. One row per lookup that actually returned (not on raise), so the
    per-minute (4) and per-day (500) caps in ``torrent_scan.vt_blocked_until`` can be computed by
    counting rows in the relevant window."""

    __tablename__ = "vt_submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class Subscription(Base):
    """A user's "follow" of an author or a series (Wave E, R14-R16). The ``follow_tick`` re-enumerates
    each active sub on a 6h cadence and (when ``auto_request``) auto-fetches NEW titles that appear,
    reusing the normal acquire pipeline + the Wave D ``origin``="following" ledger tag.

    ``known_keys`` is the diff baseline: the set of norm-keys seen at the last check (seeded at SUBSCRIBE
    time so day-1 backlog is NOT auto-requested — only titles that appear AFTER follow fire). One writer
    (the tick) per sub, so a plain JSON list is safe. ``key`` is the normalized author/series identity:
    author → ``extract._author_norm(name)`` · series → ``norm_title(series_name)``."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "key", name="uq_subscription_user_kind_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))   # author | series
    key: Mapped[str] = mapped_column(String(512), index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Per-subscription auto-fetch toggle (R15 "Follow → auto-fetch"); default True, off-switch in the
    # Following view. When False the tick only updates the baseline (no acquire).
    auto_request: Mapped[bool] = mapped_column(Boolean, default=True)
    # Diff baseline: norm-keys already seen (seeded at subscribe time). JSON list.
    known_keys: Mapped[list | None] = mapped_column(JSON, nullable=True)
    auto_added: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ListSubscription(Base):
    """A monitored import of an external reading list/library (AniList, Goodreads, Open Library,
    Hardcover, MyAnimeList, Amazon wishlist). ``list_sync_tick`` periodically re-fetches the list and
    auto-acquires NEW titles per ``variant``, diffing against ``known_keys`` (same baseline pattern as
    ``Subscription`` — seeded at add time from the curated first import, so only titles that appear
    AFTER are auto-fetched). Per-user; the poll cadence is a global admin setting."""

    __tablename__ = "list_subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", "list_ref", name="uq_listsub_user_provider_ref"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # anilist | goodreads | openlibrary | hardcover | mal | amazon_wishlist
    provider: Mapped[str] = mapped_column(String(24))
    # the list identity: username / numeric id / shelf-or-list name / wishlist URL (provider-specific)
    list_ref: Mapped[str] = mapped_column(String(512))
    # which sub-list, when the provider has several (goodreads shelf, anilist status). NULL = default.
    list_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255))
    # what to fetch for each title: ebook | audiobook | both
    variant: Mapped[str] = mapped_column(String(16), default="ebook")
    target_shelf_id: Mapped[int | None] = mapped_column(ForeignKey("bookshelves.id"), nullable=True)
    # Destination: when true, NEW titles are queued to operator STOCK (shared pre-fetch pool) instead
    # of the user's library — so a list can be tracked without cluttering a personal library.
    to_stock: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # When a fetched title belongs to a series: also fetch the REST of the series now (auto_series),
    # and/or start a series follow so FUTURE volumes keep coming (auto_follow_series). Both per-list.
    auto_series: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_follow_series: Mapped[bool] = mapped_column(Boolean, default=False)
    # Diff baseline: norm-keys already seen (seeded at add time). JSON list. One writer (the tick).
    known_keys: Mapped[list | None] = mapped_column(JSON, nullable=True)
    auto_added: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Stored for password recovery (NOT verified at signup). UNIQUE, but SQLite allows
    # multiple NULLs so admin-created users without an email never collide.
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")  # admin | user
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Self-registration approval gate: "approved" (can log in) | "pending" (awaiting an admin in
    # the "approval" registration mode). Existing + admin-created users are always "approved".
    approval_status: Mapped[str] = mapped_column(String(16), default="approved")
    # Admin-set cap on which Index media categories this user may view (subset of
    # catalog.MEDIA_CATEGORIES). NULL = inherit the global default (AppSetting
    # 'default_user_categories'); that being absent = all categories. Admins are never restricted.
    allowed_categories: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # This user's OWN opt-in to 18+ content, per media category (subset of catalog.MEDIA_CATEGORIES).
    # NULL/[] = sees no adult content. Effective = this ∩ the admin gate (AppSetting
    # 'adult_allowed_categories'). User-self-settable (a content preference, not an admin permission).
    adult_categories: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Admin-set granular capability flags (subset of permissions.ALL_PERMISSIONS). NULL = inherit
    # the global default (AppSetting 'default_user_permissions'); that absent = the built-in
    # baseline. Admins implicitly hold every permission and are never restricted.
    permissions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PasswordResetToken(Base):
    """A single-use, time-limited token emailed to a user to reset a forgotten password."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AppSetting(Base):
    """A global (not per-user) key/value app setting, e.g. indexing crawl defaults."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class RequestStat(Base):
    """Outbound-request telemetry, bucketed by UTC hour × destination host × category. Written by a
    periodic flush of in-memory deltas (app/telemetry.py); read by the Settings → Index dashboard for
    totals, rates, and over-time trends. One row per (bucket, host, category)."""

    __tablename__ = "request_stats"
    __table_args__ = (UniqueConstraint("bucket", "host", "category", "outcome",
                                       name="uq_reqstat_bucket_host_cat_outcome"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bucket: Mapped[str] = mapped_column(String(16), index=True)   # "YYYY-MM-DDTHH:00" (UTC hour)
    host: Mapped[str] = mapped_column(String(255), index=True)    # destination hostname
    category: Mapped[str] = mapped_column(String(32))             # crawl|metadata|integration|…
    outcome: Mapped[str] = mapped_column(String(16), default="success")  # success|blocked|timeout|error
    count: Mapped[int] = mapped_column(Integer, default=0)


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
    # DEPRECATED single push target (Apprise URL). Superseded by the NotificationChannel table; kept
    # for back-compat and migrated into a channel row by migration 0028. No longer written.
    apprise_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Per-event notification opt-in/out: {event_key: bool}. Stores explicit overrides ONLY; an absent
    # key falls back to the registry's default_on (see app/notifications.py).
    notify_prefs: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-title default shelf for THIS user: {str(work_id): shelf_id}. Multi-user-correct (the
    # default lives on the user, not the shared Work). Cleared by removing the key.
    work_default_shelves: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class NotificationChannel(Base):
    """A delivery target for a user's notifications — a user may have several. The structured
    ``config`` (per ``kind``) is the source of truth the UI edits; ``apprise_url`` is the Apprise URL
    we BUILD from it server-side (or, for the advanced ``apprise`` kind, the raw URL the user pasted).
    The ``email`` kind has no apprise_url — it is delivered via the shared SMTP server to the user's
    address. A row with ``user_id IS NULL`` is the admin-configured GLOBAL default channel (used for
    admin ops events when an admin has no channel of their own)."""

    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # ntfy | pushover | telegram | discord | slack | email | apprise
    kind: Mapped[str] = mapped_column(String(16))
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Structured per-kind inputs (and secrets, e.g. tokens). Never returned raw by the API.
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # The Apprise URL built from config (NULL for the email kind).
    apprise_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Notification(Base):
    """A per-user in-app notification (the header bell). Written by ``dispatch_event`` for every
    delivered event so the user has a durable record regardless of push/email outcome."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notif_user_unread", "user_id", "read_at"),
        Index("ix_notif_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    event_key: Mapped[str] = mapped_column(String(48))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[str] = mapped_column(String(8), default="info")  # info | warn | error
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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

    Per-shelf automation toggles: ``auto_kindle`` (auto-send newly gathered content to the user's
    Kindle), ``notify_on_add`` (push a notification when a title is auto/queued-hooked onto this
    shelf). ``goodreads_target`` marks the shelf as the destination for the user's Goodreads wishlist
    auto-hooks. ``auto_update`` is DEPRECATED + no-op: every actively-releasing library item is now
    auto-refreshed regardless (see scheduler.schedule_refresh_jobs); the column is kept for
    backward-compat. Pause a single title via ``Work.crawl_paused`` to opt it out."""

    __tablename__ = "bookshelves"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_bookshelf_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    auto_update: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_kindle: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_on_add: Mapped[bool] = mapped_column(Boolean, default=False)
    # Email the book to the user's personal email on discovery (distinct from auto_kindle, which
    # targets the Kindle address). Both reuse the SMTP delivery config.
    notify_email: Mapped[bool] = mapped_column(Boolean, default=False)
    goodreads_target: Mapped[bool] = mapped_column(Boolean, default=False)
    # A host directory mapped to this shelf: new content discovered here is auto-placed on the shelf
    # and fires its automation events. Admin-only to set (it reads the host filesystem). NULL = none.
    watch_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
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
