"""Pydantic v2 response/request schemas."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ComplianceOut(BaseModel):
    license_basis: str
    tos_permitted: bool
    robots_respected: bool
    min_request_interval_s: float
    max_daily_requests: int


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    key: str
    display_name: str
    base_url: str | None
    adapter_key: str
    license_basis: str
    tos_permitted: bool
    robots_respected: bool
    render_js: bool
    min_request_interval_s: float
    max_daily_requests: int
    has_auth: bool = False           # a credential (e.g. J-Novel token) is stored (never the secret)
    supports_auth: bool = False      # this source can use an access token (UI shows the field)


class SourceUpdate(BaseModel):
    tos_permitted: bool | None = None
    robots_respected: bool | None = None
    render_js: bool | None = None
    min_request_interval_s: float | None = Field(default=None, ge=0)
    max_daily_requests: int | None = Field(default=None, ge=0)
    display_name: str | None = None
    base_url: str | None = None
    auth_token: str | None = None    # write-only credential; stored in Source.config, never returned


class WorkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_id: int | None
    source_work_ref: str | None
    title: str
    author: str | None
    cover_url: str | None
    description: str | None
    language: str | None
    status: str
    hooked: bool
    media_kind: str = "text"
    series: str | None = None              # series name (for library grouping), if known
    series_position: float | None = None   # this volume's position in the series (may be fractional)
    total_chapters_known: int
    total_chapters_expected: int | None = None
    chapters_fetched: int = 0
    start_chapter: int = 1  # hooked from this chapter number (1 = from the beginning)
    health: str = "unknown"
    health_detail: str | None = None
    # One clear, human state for the library card, derived from status + health + outstanding work:
    #   gathering  — actively downloading chapters now
    #   ongoing    — caught up; the series is still releasing (more will come)
    #   complete   — the series has finished AND everything is gathered
    #   incomplete — chapters are missing / couldn't be fetched (needs attention)
    library_status: str = "ongoing"
    last_checked_at: datetime | None = None
    last_update_at: datetime | None = None
    crawl_interval_s: float | None = None
    crawl_window_start: int | None = None
    crawl_window_end: int | None = None
    shelf_ids: list[int] = []  # which of the caller's bookshelves this work is on
    audiobook_work_id: int | None = None  # matching shared audiobook Work (the "listen" format), if any


class WorkDetailOut(WorkOut):
    chapters_total: int = 0
    chapters_read: int = 0
    last_chapter_id: int | None = None
    scroll_fraction: float = 0.0
    # The caller's per-title default shelf for this work (None = no default set).
    default_shelf_id: int | None = None


class WorkMetaUpdate(BaseModel):
    """Manual metadata correction for a library work. Only the fields PRESENT in the request are
    applied (so a partial edit leaves the rest untouched); an empty string clears author/series/cover.
    ``source_work_ref`` re-points the fetching source's reference (fix a wrong match's source)."""
    title: str | None = None
    author: str | None = None
    cover_url: str | None = None
    series: str | None = None
    series_position: float | None = None
    source_work_ref: str | None = None


class WorkProvenanceOut(BaseModel):
    """Where a library work came from — to diagnose/fix a wrong match. Surfaces the fetching source +
    on-disk filename, the catalog metadata used for the fetch, and the originally-requested title/author
    (from an import list / watchlist), so a mismatch like 'It Takes Two' is visible."""
    source_key: str | None = None
    source_name: str | None = None      # Source.display_name (e.g. "Local import", "Web index")
    source_ref: str | None = None       # the work's source_work_ref on that source
    source_url: str | None = None       # a clickable link to the source page, when derivable
    filename: str | None = None         # basename of the on-disk file, if imported/downloaded
    file_size: int | None = None
    catalog_title: str | None = None    # the catalog entry that hooked into this work (metadata used)
    catalog_author: str | None = None
    catalog_domain: str | None = None
    catalog_url: str | None = None
    request_title: str | None = None    # what was ORIGINALLY requested (import list / watchlist)
    request_author: str | None = None
    request_origin: str | None = None   # e.g. "goodreads", "anilist", "import", "catalog"
    request_detail: str | None = None   # origin_detail (e.g. the list name)


class MetaCandidateOut(BaseModel):
    """One search hit from a metadata provider, offered as a 'fix this match' option."""
    provider: str
    ref: str
    title: str
    author: str | None = None
    year: int | None = None
    cover_url: str | None = None
    synopsis: str | None = None
    media_kind: str = "text"


class ChapterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    work_id: int
    index: int          # internal ordering position (may differ from the chapter's number)
    number: float       # the chapter's human number (e.g. 700) — what to DISPLAY
    title: str
    fetch_status: str
    has_content: bool = False


class ChapterListOut(BaseModel):
    items: list[ChapterOut]
    total: int
    limit: int
    offset: int


class ReaderContentOut(BaseModel):
    chapter_id: int
    work_id: int
    index: int
    title: str
    html: str
    word_count: int
    prev_chapter_id: int | None
    next_chapter_id: int | None


class ProgressIn(BaseModel):
    last_chapter_id: int
    scroll_fraction: float = Field(ge=0.0, le=1.0, default=0.0)
    paragraph_index: int = Field(ge=0, default=0)


class ProgressOut(BaseModel):
    work_id: int
    last_chapter_id: int | None
    scroll_fraction: float
    paragraph_index: int = 0
    chapters_read: int
    continue_chapter_id: int | None


class ContinueItem(BaseModel):
    work_id: int
    title: str
    author: str | None
    cover_url: str | None
    chapter_id: int
    chapter_index: int
    chapter_title: str
    paragraph_index: int
    scroll_fraction: float
    chapters_read: int
    total_chapters: int
    percent: float
    updated_at: datetime


class CrawlPolicyIn(BaseModel):
    """Per-title crawl policy. Any field omitted/None leaves that knob at its current
    value via PATCH (and unset = source default). Window hours are UTC 0–23."""
    crawl_interval_s: float | None = Field(default=None, ge=0)
    crawl_window_start: int | None = Field(default=None, ge=0, le=23)
    crawl_window_end: int | None = Field(default=None, ge=0, le=23)


class HookIn(BaseModel):
    source_key: str
    work_ref: str
    # Optional per-title crawl policy applied at hook time.
    crawl_interval_s: float | None = Field(default=None, ge=0)
    crawl_window_start: int | None = Field(default=None, ge=0, le=23)
    crawl_window_end: int | None = Field(default=None, ge=0, le=23)
    shelf_id: int | None = None  # place the hooked work on this bookshelf


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    work_id: int
    kind: str
    status: str
    attempts: int
    last_error: str | None
    cursor: dict | None
    scheduled_for: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    theme: str
    reader_prefs: dict[str, Any]
    kindle_email: str | None = None
    smtp_configured: bool = False
    smtp_from: str | None = None  # the shared sending address (admin-configured; read-only here)
    delivery: dict[str, Any] = {}  # the user's recipient ('email_to')
    apprise_url: str | None = None  # per-user push target (ntfy/Pushover/Telegram/…)


class GlobalSmtpOut(BaseModel):
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_from: str | None = None
    smtp_security: str = "starttls"   # none | starttls | ssl
    smtp_password_set: bool = False   # whether a password is stored (never returned)
    configured: bool = False          # host + from present → mail can be sent


class GlobalSmtpIn(BaseModel):
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    smtp_from: str | None = None
    smtp_security: str | None = None
    smtp_password: str | None = None  # write-only; only applied when non-empty


class SettingsIn(BaseModel):
    theme: str | None = None
    reader_prefs: dict[str, Any] | None = None
    kindle_email: str | None = None
    delivery: dict[str, Any] | None = None  # smtp_* fields + email_to (password write-only)
    apprise_url: str | None = None


# ---------------------------------------------------------------- notifications
class ChannelIn(BaseModel):
    kind: str                              # ntfy | pushover | telegram | discord | slack | email | apprise
    label: str | None = None
    config: dict[str, Any] = {}            # structured per-kind inputs (secrets kept-when-blank)
    enabled: bool | None = None


class ChannelOut(BaseModel):
    id: int
    kind: str
    label: str | None = None
    config: dict[str, Any] = {}            # redacted (secret fields → '<field>_set' booleans)
    enabled: bool = True


class EventDefOut(BaseModel):
    key: str
    label: str
    description: str
    audience: str
    category: str
    default_on: bool
    enabled: bool                          # effective for this viewer (default_on merged with overrides)


class PrefsIn(BaseModel):
    selected: dict[str, bool]              # {event_key: bool}


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_key: str
    title: str
    body: str = ""
    level: str = "info"
    created_at: datetime
    read_at: datetime | None = None


class BroadcastIn(BaseModel):
    kind: str = "announcement"             # announcement | downtime
    title: str
    body: str = ""


class SendToKindleIn(BaseModel):
    to: str | None = None  # explicit recipient (Kindle or personal email)
    kindle_email: str | None = None  # back-compat alias
    start: int = Field(default=1, ge=1)
    limit: int | None = Field(default=None, ge=1)


class SendToKindleOut(BaseModel):
    sent: bool
    chapters: int
    to: str


class BulkDownloadIn(BaseModel):
    work_ids: list[int] = []
    shelf_id: int | None = None  # include every work on this shelf too


class IndexSiteIn(BaseModel):
    url: str
    max_pages: int | None = Field(default=None, ge=0, le=1_000_000)  # 0 = unlimited
    max_depth: int | None = Field(default=None, ge=0, le=20)
    same_host_only: bool = True
    # Re-adding an already-indexed (or removed) URL resumes WITHOUT re-fetching crawled pages by
    # default. Set true to also refresh already-indexed content (re-queue every fetched page).
    update_indexed: bool = False


class IndexSiteUpdate(BaseModel):
    """Editable per-site crawl bounds (Jobs page)."""
    stop_after_idle_pages: int | None = Field(default=None, ge=1, le=100_000)
    max_pages: int | None = Field(default=None, ge=0, le=1_000_000)  # 0 = unlimited
    max_depth: int | None = Field(default=None, ge=0, le=20)
    # Media-kind allowlist (subset of {"text","comic"}); [] / null = all kinds. Restricts which catalog
    # members this site contributes to acquisition matching (e.g. mark a novels-only crawl source).
    allowed_media_kinds: list[str] | None = None


class IndexConfigOut(BaseModel):
    """Global indexing defaults (Settings → Indexing)."""
    stop_after_idle_pages: int
    max_pages: int  # 0 = unlimited


class IndexConfigIn(BaseModel):
    stop_after_idle_pages: int = Field(ge=1, le=100_000)


class IndexBlockOut(BaseModel):
    id: int
    scope: str          # url | domain
    value: str
    reason: str | None = None
    title: str | None = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class IndexBlockIn(BaseModel):
    scope: str = Field(pattern="^(url|domain)$")
    value: str = Field(min_length=1, max_length=2048)
    reason: str | None = None
    title: str | None = None


class CrawlTuningOut(BaseModel):
    """Live-editable crawl speed (Settings → Indexing)."""
    tick_seconds: int       # how often a crawl/index cycle runs
    chapters_per_tick: int  # chapters one backfill job fetches per cycle
    parallel_fetches: int   # per-cycle work/page budget + global fetch concurrency
    refresh_hours: int      # how often hooked titles are checked for new chapter releases


class CrawlTuningIn(BaseModel):
    tick_seconds: int | None = Field(default=None, ge=2, le=600)
    chapters_per_tick: int | None = Field(default=None, ge=1, le=50)
    parallel_fetches: int | None = Field(default=None, ge=1, le=32)
    refresh_hours: int | None = Field(default=None, ge=1, le=168)


class OperatorIdentityOut(BaseModel):
    """Live-editable crawl identity (Settings → Crawl identity) — what the fetcher tells sources."""
    user_agent: str       # sent as the User-Agent header (and used for robots.txt matching)
    contact_email: str    # sent as the From header (a contact a site admin can reach you at)


class OperatorIdentityIn(BaseModel):
    user_agent: str | None = Field(default=None, max_length=512)
    contact_email: str | None = Field(default=None, max_length=512)


class IndexSiteOut(BaseModel):
    id: int
    root_url: str
    domain: str
    title: str | None
    status: str
    max_pages: int
    max_depth: int
    same_host_only: bool
    stop_after_idle_pages: int = 0      # idle-page timeout (0 → uses global default)
    pages_since_new_title: int = 0      # consecutive fetched pages with no new title
    allowed_media_kinds: list[str] | None = None  # null/[] = all kinds; else the restriction set
    last_error: str | None = None
    # When set + in the future, the site is throttling after pushback (paused, not stopped).
    cooldown_until: datetime | None = None
    consecutive_errors: int = 0       # transient errors in a row (drives cooldown escalation)
    status_reason: str | None = None  # human explanation of why it's done/paused/cooling/failed
    pages_total: int = 0
    pages_fetched: int = 0
    pages_pending: int = 0
    pages_failed: int = 0
    words: int = 0
    titles_found: int = 0          # catalog works discovered from this site
    requests: int = 0              # pages actually requested (fetched + failed)
    duration_seconds: float = 0.0  # created_at → last activity (or now, if crawling)
    last_activity_at: datetime | None = None
    created_at: datetime


class IndexStatsOut(BaseModel):
    """Aggregate crawl observability across all indexed sites."""
    sites_total: int = 0
    sites_active: int = 0    # in-progress
    sites_paused: int = 0    # aborted / stopped
    sites_done: int = 0      # complete
    sites_failed: int = 0    # error
    pages_total: int = 0
    pages_fetched: int = 0
    pages_pending: int = 0
    pages_failed: int = 0
    titles_found: int = 0
    requests_made: int = 0
    words_indexed: int = 0
    time_spent_seconds: float = 0.0


class IndexedPageOut(BaseModel):
    id: int
    site_id: int
    url: str
    title: str | None
    description: str | None = None
    author: str | None = None
    cover_url: str | None = None
    site_name: str | None = None
    page_type: str | None = None
    word_count: int
    depth: int
    status: str
    hooked_work_id: int | None = None
    fetched_at: datetime | None = None
    snippet: str | None = None
    last_error: str | None = None          # why it failed/was skipped (kind-prefixed)
    attempts: int = 0                       # transient-retry count
    next_attempt_at: datetime | None = None  # when a deferred page retries


class IndexedPageDetailOut(IndexedPageOut):
    html: str | None = None
    domain: str | None = None


class IndexSearchOut(BaseModel):
    page_id: int
    site_id: int
    url: str
    title: str | None
    description: str | None = None
    author: str | None = None
    cover_url: str | None = None
    snippet: str
    score: float


# ----------------------------------------------------------------- catalog
class CatalogSourceOut(BaseModel):
    """One source's copy of a discovered work (a selectable source for hooking/grabbing)."""
    catalog_id: int
    title: str | None = None       # this source's own matched title (sub-title)
    author: str | None = None
    cover_url: str | None = None
    synopsis: str | None = None
    site_id: int | None = None
    domain: str
    work_url: str
    provider: str = "web_index"        # web_index | readarr | kapowarr
    kind: str = "online"               # online | readarr | kapowarr
    media_kind: str = "text"           # text | comic
    media_label: str = "Novel"         # human label: Novel | Book | Manga | Webtoon | Comic
    integration_id: int | None = None
    chapters_advertised: int | None = None
    chapters_listed: int | None = None
    health: str = "unknown"
    health_detail: str | None = None
    hooked_work_id: int | None = None
    grab_status: str | None = None     # set once a grab has been requested
    listing_only: bool = False         # a metadata listing (no direct hook/grab)


class GrabOut(BaseModel):
    ok: bool
    integration: str | None = None
    message: str


class CatalogGroupOut(BaseModel):
    """A discovered work, merged across the sites that carry it."""
    id: int = 0                    # representative catalog id — stable unique group key
    norm_key: str
    title: str
    author: str | None = None
    cover_url: str | None = None
    synopsis: str | None = None
    language: str | None = None
    media_kind: str = "text"
    media_label: str = "Novel"         # fine per-title badge: Novel|Book|Manga|Manhua|Webtoon|Comic
    media_category: str = "Novel"      # coarse section: Manga & Comics | Novel | Book
    is_adult: bool = False             # 18+ content (shown with an 18+ badge when visible)
    chapters: int | None = None
    hooked_work_id: int | None = None
    in_library: bool = False           # the current user added this to THEIR library
    in_stock: bool = False             # operator pre-fetched + hooked, but not in the user's library
    series: str | None = None          # series name when part of a known series (gates View Series)
    # When >1, this card REPRESENTS a collapsed series: that many per-volume cards were folded into
    # this one in the browse to cut over-cardinality (each volume is still its own acquirable work,
    # reachable via search + View Series). 1 = a normal single card.
    series_count: int = 1
    sources: list[CatalogSourceOut] = []


class CatalogRowOut(BaseModel):
    """One Index-page discovery row — a popularity/genre/theme lane of titles + a browse target."""
    kind: str                      # popular | genre | theme
    slug: str                      # category slug ('' for the popular lane)
    label: str                     # display heading ("Most Popular", "Fantasy", …)
    media_category: str = "Manga & Comics"  # Manga & Comics | Novel | Book — the section
    count: int = 0                 # how many titles exist in this category (for the Browse target)
    items: list[CatalogGroupOut] = []


class WorkUpdateOut(BaseModel):
    """Result of re-checking a hooked title for new content."""
    work_id: int
    checked: bool
    new_chapters: int = 0
    metadata_changed: bool = False
    status: str | None = None
    total_chapters_expected: int | None = None
    error: str | None = None


class CheckAllUpdatesOut(BaseModel):
    works_checked: int = 0
    works_updated: int = 0
    new_chapters: int = 0


class IntegrationIn(BaseModel):
    # readarr/kapowarr = download managers; prowlarr/sabnzbd = acquisition pipeline
    # (search source + usenet downloader); the rest are metadata providers
    # (ranobedb=volumes, googlebooks/hardcover=books, anilist/novelupdates=chapters, goodreads=wishlist).
    kind: str = Field(
        pattern="^(readarr|kapowarr|prowlarr|sabnzbd|qbittorrent|libgen|virustotal|ranobedb|googlebooks|hardcover|anilist|novelupdates|goodreads|audiobookshelf|storyteller)$"
    )
    name: str | None = None
    base_url: str = ""                # optional for metadata providers (ranobedb has a default)
    api_key: str = ""                 # not needed for metadata providers
    enabled: bool = True
    root_folder: str | None = None
    auto_map_folders: bool = True
    config: dict | None = None        # provider settings (Goodreads {"user_id":..,"shelf":..})


class IntegrationUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None        # omit/None = keep existing
    enabled: bool | None = None
    root_folder: str | None = None
    auto_map_folders: bool | None = None
    config: dict | None = None


class IntegrationOut(BaseModel):
    id: int
    kind: str
    name: str
    base_url: str
    enabled: bool
    root_folder: str | None = None
    auto_map_folders: bool = True
    config: dict | None = None
    category: str = "manager"         # metadata | manager | pipeline (from the provider catalog)
    is_metadata: bool = False         # metadata provider (no downloads/root folders)
    is_pipeline: bool = False         # acquisition pipeline (Prowlarr search / SABnzbd downloader)
    has_api_key: bool = False         # the key itself is never returned
    requests_per_minute: float = 60   # effective request cap (override or catalog default)
    timeout: float = 20              # effective per-request timeout (seconds)
    last_sync_at: datetime | None = None
    last_error: str | None = None
    catalog_count: int = 0


class ProviderCatalogOut(BaseModel):
    """One connectable integration's static descriptor (drives the Settings provider boxes)."""
    kind: str
    category: str                     # metadata | manager | pipeline
    label: str
    tagline: str
    provides: list[str] = []
    use: str = ""
    requests: str = ""
    matching: str = ""
    auth: str = "none"                # none | optional_key | key | token | cookie
    per_user: bool = False
    default_rpm: float = 60
    default_timeout: float = 20


class FetchPriorityIn(BaseModel):
    order: list[str]


class SeriesBookOut(BaseModel):
    title: str
    author: str | None = None
    year: int | None = None
    position: float | None = None   # fractional positions exist for novellas (e.g. 2.5, 12.5)
    cover_url: str | None = None
    ref: str | None = None              # Open Library work key (stable selector)
    catalog_id: int | None = None
    hooked_work_id: int | None = None   # already in the library
    in_library: bool = False            # in THIS user's library (else it's a missing volume)


class SeriesOut(BaseModel):
    series: str | None = None
    books: list[SeriesBookOut] = []


class SeriesAcquireIn(BaseModel):
    refs: list[str] = []   # OL keys to fetch
    all: bool = False      # fetch the whole series
    shelf_id: int | None = None  # place each acquired volume on this bookshelf


class AuthorBooksOut(BaseModel):
    author: str | None = None
    books: list[SeriesBookOut] = []
    count: int = 0   # the FULL roster size (the acquire is server-capped) so the UI confirm is honest


class AuthorAcquireIn(BaseModel):
    refs: list[str] = []   # provider keys to fetch
    all: bool = False      # fetch every (not-owned) book by this author, up to the server cap
    shelf_id: int | None = None  # place each acquired book on this bookshelf


class SubscriptionOut(BaseModel):
    id: int
    kind: str             # author | series
    key: str
    display_name: str
    active: bool
    auto_request: bool
    auto_added: int
    last_checked_at: datetime | None = None
    created_at: datetime | None = None


class SubscriptionCreateIn(BaseModel):
    kind: str                       # author | series
    catalog_id: int | None = None   # follow the author/series of this catalog row
    series_name: str | None = None  # (kind=series) follow a series by name directly


class SubscriptionPatchIn(BaseModel):
    auto_request: bool | None = None
    active: bool | None = None


class ReleaseCandidateOut(BaseModel):
    """A ranked Prowlarr release candidate for a catalog book."""
    title: str
    indexer: str | None = None
    guid: str | None = None
    size: int = 0
    size_mb: float = 0.0
    fmt: str | None = None
    is_audiobook: bool = False
    language: str | None = None
    confidence: float
    score: float
    accepted: bool
    auto_ok: bool
    reason: str


class DownloadJobOut(BaseModel):
    id: int
    catalog_work_id: int | None = None
    title: str
    release_title: str | None = None
    indexer: str | None = None
    size: int = 0
    fmt: str | None = None
    status: str
    grab_kind: str
    work_id: int | None = None
    error: str | None = None
    not_before: datetime | None = None  # when a deferred (daily-cap) grab is scheduled to retry
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None


class StockItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    stock_job_id: int | None = None
    norm_key: str
    catalog_work_id: int | None = None
    work_id: int | None = None
    title: str
    author: str | None = None
    media_label: str = "Book"
    media_category: str = "Book"
    popularity_norm: float = 0.0
    status: str                          # pending | searching | downloading | stocked | unavailable | failed
    size: int | None = None
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    stocked_at: datetime | None = None


class StockSummaryOut(BaseModel):
    configured: bool = False             # pipeline + stock dir both set
    pipeline_configured: bool = False
    stock_dir: str | None = None
    counts: dict[str, int] = {}          # per-status counts
    total: int = 0


class StockQueueIn(BaseModel):
    name: str | None = None              # operator's name for this batch (blank → derived from filter)
    media: str | None = None             # category: Manga & Comics | Novel | Book
    dimension: str | None = None         # genre | theme (with value)
    value: str | None = None             # category slug
    sort: str = "popularity"             # popularity | title | new
    limit: int = Field(default=200, ge=1, le=5000)
    group_ids: list[int] | None = None   # explicit catalog group ids (overrides the filter when set)
    variant: str = "ebook"               # ebook | audiobook | both


class StockJobOut(BaseModel):
    id: int | None = None                # None = the legacy 'ungrouped' bucket
    name: str
    media_category: str | None = None
    dimension: str | None = None
    value: str | None = None
    sort: str | None = None
    variant: str = "ebook"               # ebook | audiobook | both
    requested: int = 0                   # groups matched when queued
    created_at: datetime | None = None
    # rolled-up progress + monitoring stats
    total: int = 0
    stocked: int = 0
    in_flight: int = 0
    pending: int = 0
    issues: int = 0                      # failed + unavailable (need attention)
    progress: float = 0.0               # stocked / total (0..1)
    stocked_size: int = 0               # bytes of stocked files
    overall: str = "empty"              # working | complete | needs attention | empty
    counts: dict[str, int] = {}


class StockJobDetailOut(StockJobOut):
    items: list[StockItemOut] = []          # capped sample for display (see items_shown vs total)
    items_shown: int = 0                    # how many items this response actually carries
    problem_items: list[StockItemOut] = []  # the failed/unavailable items, for quick triage (capped)


class StockConfigIn(BaseModel):
    stock_dir: str | None = None


class BookCatalogConfigIn(BaseModel):
    enabled: bool | None = None
    hot_set_cap: int | None = Field(default=None, ge=0, le=1_000_000)
    closeness_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class IntegrationTestOut(BaseModel):
    ok: bool
    app: str | None = None
    version: str | None = None
    detail: str | None = None
    root_folders: list[str] = []
    error: str | None = None


class ProviderStats(BaseModel):
    provider: str
    total: int          # hooked library works (the denominator)
    matched: int        # works with a link for this provider
    unmatched: int
    high_confidence: int    # confidence >= 0.8
    medium_confidence: int  # 0.6 <= confidence < 0.8
    low_confidence: int     # confidence < 0.6
    match_ratio: float


class MetadataStatsOut(BaseModel):
    total_library_works: int
    providers: list[ProviderStats] = []


class GoodreadsIn(BaseModel):
    """A user connecting their own Goodreads want-to-read shelf."""
    goodreads_user_id: str = Field(min_length=1)  # numeric id or profile URL
    shelf: str | None = "to-read"
    enabled: bool | None = None


class GoodreadsOut(BaseModel):
    connected: bool = False
    id: int | None = None
    enabled: bool = False
    goodreads_user_id: str | None = None
    shelf: str | None = None
    last_sync_at: datetime | None = None
    last_error: str | None = None


class ListPreviewIn(BaseModel):
    """Read an external reading list to preview before subscribing."""
    provider: str = Field(min_length=1)        # anilist | goodreads | openlibrary | hardcover | mal | amazon_wishlist
    list_ref: str = Field(min_length=1)        # username / numeric id / wishlist URL
    list_name: str | None = None               # which sub-list (shelf/status), provider-specific


class ListPreviewItemOut(BaseModel):
    title: str
    author: str | None = None
    media_kind: str = "text"
    cover_url: str | None = None
    match_catalog_id: int | None = None        # a quick local catalog match (user can correct), or null
    match_title: str | None = None
    match_author: str | None = None


class ListPreviewOut(BaseModel):
    provider: str
    list_ref: str
    list_name: str | None = None
    count: int
    items: list[ListPreviewItemOut]


class ListResolveItemIn(BaseModel):
    title: str = Field(min_length=1)
    author: str | None = None
    media_kind: str = "text"   # text | comic — enforces strict content-type matching vs crawled sources


class ListResolveIn(BaseModel):
    """A chunk of previewed titles to resolve catalog-first then upstream (book_catalog.resolve_live),
    so metadata exists + the fetch pipeline has correct data before the import is finalized."""
    items: list[ListResolveItemIn]


class ListConfirmItemIn(BaseModel):
    title: str = Field(min_length=1)
    author: str | None = None
    selected: bool = True                      # fetch this title now? (unselected → baselined, not fetched)
    variant: str | None = None                 # per-item override of the subscription variant


class ListConfirmIn(BaseModel):
    provider: str = Field(min_length=1)
    list_ref: str = Field(min_length=1)
    list_name: str | None = None
    display_name: str = Field(min_length=1)
    variant: str = "ebook"                      # ebook | audiobook | both (default for selected items)
    target_shelf_id: int | None = None
    to_stock: bool = False                       # queue NEW titles to operator stock instead of the library
    auto_series: bool = False                   # also fetch the rest of a fetched title's series now
    auto_follow_series: bool = False            # follow a fetched title's series for future volumes
    items: list[ListConfirmItemIn]             # the FULL previewed list (selected flags drive acquisition)


class ListSubOut(BaseModel):
    id: int
    provider: str
    list_ref: str
    list_name: str | None = None
    display_name: str
    variant: str
    target_shelf_id: int | None = None
    to_stock: bool = False
    active: bool
    auto_series: bool = False
    auto_follow_series: bool = False
    auto_added: int
    last_checked_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime | None = None


class ListSubUpdate(BaseModel):
    variant: str | None = None
    target_shelf_id: int | None = None
    to_stock: bool | None = None
    active: bool | None = None
    auto_series: bool | None = None
    auto_follow_series: bool | None = None
    list_name: str | None = None
    list_ref: str | None = None
    display_name: str | None = None


class WorkHealthOut(BaseModel):
    """Completeness diagnosis for a hooked work."""
    work_id: int
    health: str
    detail: str | None = None
    fetched: int = 0
    failed: int = 0
    pending: int = 0
    listed: int = 0
    advertised: int | None = None
    gaps: list[int] = []
    actions: list[str] = []


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    display_name: str | None = None
    email: str | None = None
    role: str
    is_active: bool
    approval_status: str = "approved"  # approved | pending
    # Admin-set cap on viewable Index categories (None = inherit the global default).
    allowed_categories: list[str] | None = None
    # Admin-set granular capability flags (None = inherit the global default).
    permissions: list[str] | None = None
    created_at: datetime
    # Derived from UserSession (no stored last_login column): the newest session's start time, and
    # how many sessions are still unexpired. Populated by list_users; absent elsewhere → defaults.
    last_seen: datetime | None = None
    active_sessions: int = 0


class MeOut(BaseModel):
    authenticated: bool
    needs_setup: bool
    user: UserOut | None = None
    # Resolved categories the current user may view on the Index (admins → all). Lets the frontend
    # show only permitted categories without re-deriving the admin cap + global default.
    allowed_categories: list[str] = []
    # Resolved capability flags the current user holds (admins → all). Drives what the UI shows/does.
    permissions: list[str] = []
    # Categories the admin permits 18+ content in (global gate; default all, empty = disabled).
    adult_allowed_categories: list[str] = []
    # Resolved categories where THIS user sees 18+ content (inherits the full gate by default).
    adult_categories: list[str] = []


class LoginIn(BaseModel):
    username: str
    password: str


class RegisterIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=255)
    password: str  # length validated server-side against the configured minimum
    kindle_email: str | None = None  # optional Send-to-Kindle address set at signup


class RegisterOut(BaseModel):
    # In "open" mode this carries the logged-in user; in "approval" mode status="pending" + user=None.
    status: str = "ok"  # ok | pending
    user: UserOut | None = None


class ForgotPasswordIn(BaseModel):
    identifier: str = Field(min_length=1)  # username OR email


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=1)
    password: str  # length validated server-side


class DefaultShelfIn(BaseModel):
    shelf_id: int | None = None  # null clears the per-title default


class SetupIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8)
    display_name: str | None = None
    token: str | None = None  # required if SHELF_SETUP_TOKEN is configured


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8)
    display_name: str | None = None
    email: str | None = None  # optional, for password recovery; must be unique if set
    role: str = "user"  # admin | user
    # Optional per-user category cap (None = inherit the global default).
    allowed_categories: list[str] | None = None
    # Optional per-user capability set (None = inherit the global default).
    permissions: list[str] | None = None


class UserUpdate(BaseModel):
    password: str | None = Field(default=None, min_length=8)
    display_name: str | None = None
    email: str | None = None  # present (even null) → set/clear; must be unique if set
    role: str | None = None
    is_active: bool | None = None
    # Present (even as null) → set the cap; null resets to the global default. Absent → unchanged.
    allowed_categories: list[str] | None = None
    # Present (even as null) → set the capability set; null resets to default. Absent → unchanged.
    permissions: list[str] | None = None


class CategoryDefaultIn(BaseModel):
    # null = no cap (all categories) for normal users.
    categories: list[str] | None = None


class PermissionDefaultIn(BaseModel):
    # null = reset to the built-in baseline default for normal users.
    permissions: list[str] | None = None


class AdultAllowedIn(BaseModel):
    # Admin gate: the categories 18+ content MAY appear in. Empty/null = 18+ off everywhere.
    categories: list[str] | None = None


class AdultOptInIn(BaseModel):
    # A user's own per-category 18+ opt-in. Empty/null = no 18+ content.
    categories: list[str] | None = None


class RestoreCommitIn(BaseModel):
    # Name of a backup in the store (from /admin/backups) to restore from.
    name: str
    # Per-section restore mode: section_key (or "media") -> skip | merge | replace.
    sections: dict[str, str] = {}


class PermissionInfo(BaseModel):
    key: str
    label: str


class PermissionsMetaOut(BaseModel):
    all: list[PermissionInfo]          # every grantable capability (key + description)
    default: list[str]                 # the current global default for new normal users
    baseline: list[str]                # the built-in baseline (when no global default is set)


class WatchedFolderIn(BaseModel):
    path: str
    display_name: str | None = None
    recursive: bool = True


class WatchedFolderOut(BaseModel):
    id: int
    path: str
    display_name: str | None
    recursive: bool
    enabled: bool
    file_count: int
    works: int = 0
    last_scan_at: datetime | None = None
    last_error: str | None = None


class AdapterInfoOut(BaseModel):
    key: str
    display_name: str
    license_basis: str
    tos_permitted_default: bool
    needs_attestation: bool
    description: str
    enabled: bool


# --------------------------------------------------------------- metadata providers
class MetadataLinkOut(BaseModel):
    id: int
    work_id: int
    provider: str
    ref: str
    matched_title: str | None = None
    confidence: float = 0.0
    status: str = "auto"
    total_units: int | None = None
    unit_kind: str | None = None
    release_marker: str | None = None
    url: str | None = None
    provider_status: str | None = None
    last_checked_at: datetime | None = None
    # Chapter-tracking providers only: the max chapters this provider says have been released, the
    # signed gap vs what we've gathered, and whether that gap is large enough to flag (> 10).
    expected_chapters: int | None = None
    chapter_discrepancy: int | None = None
    major_discrepancy: bool = False


class RelatedItemOut(BaseModel):
    title: str
    relation: str
    provider: str
    ref: str | None = None
    queued_status: str | None = None
    in_library: bool = False


class WorkRelatedOut(BaseModel):
    work_id: int
    related: list[RelatedItemOut] = []


class QueuedHookOut(BaseModel):
    id: int
    title: str
    author: str | None = None
    media_kind: str = "text"
    reason: str
    source: str | None = None
    relation: str | None = None
    status: str
    related_work_id: int | None = None
    hooked_work_id: int | None = None
    detail: str | None = None
    created_at: datetime | None = None


# -------------------------------------------------------------------- bookshelves
class BookshelfOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    sort_order: int = 0
    auto_update: bool = False
    auto_kindle: bool = False
    notify_on_add: bool = False
    notify_email: bool = False
    goodreads_target: bool = False
    goodreads_shelf: str | None = None
    watch_path: str | None = None
    count: int = 0  # works on the shelf


class BookshelfIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # Optional initial configuration (the add-shelf dialog sets these in one go).
    auto_update: bool = False
    auto_kindle: bool = False
    notify_on_add: bool = False
    notify_email: bool = False
    goodreads_target: bool = False
    goodreads_shelf: str | None = None
    watch_path: str | None = None  # admin-only host dir mapped to this shelf (monitored)
    work_ids: list[int] = []  # works to place on the new shelf (must be in the caller's library)


class BookshelfUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    sort_order: int | None = None
    auto_update: bool | None = None
    auto_kindle: bool | None = None
    notify_on_add: bool | None = None
    notify_email: bool | None = None
    goodreads_target: bool | None = None
    goodreads_shelf: str | None = None
    watch_path: str | None = None


# -------------------------------------------------------------------- missing-content ledger
class SourceSearchOut(BaseModel):
    """Wave B per-source search state for a missing title (the Missing-page info-icon popover): the
    last result per durable download source (torrent/pipeline/libgen) so the user sees which sources
    were searched, what each returned, and when."""
    source: str                       # torrent | pipeline | libgen
    status: str                       # pending | searching | no_match | exhausted | unavailable | matched | skipped
    reason: str | None = None
    last_attempt_at: datetime | None = None
    next_retry_at: datetime | None = None
    attempts: int = 0


class MissingRequestOut(BaseModel):
    """A per-title row of the missing-content ledger. Requester fields are admin-only (the count +
    usernames of who wants it); a regular user only ever sees rows they themselves requested."""
    id: int
    title: str
    author: str | None = None
    status: str                       # open | searching | unavailable | resolved | planned
    failure_reason: str | None = None
    last_provider: str | None = None
    attempts: int = 0
    first_requested_at: datetime | None = None
    last_attempt_at: datetime | None = None
    next_check_at: datetime | None = None
    release_date: date | None = None             # Planned title's provider release date (status=planned)
    resolved_at: datetime | None = None
    requested_at: datetime | None = None        # when the CALLER requested it (None for admins viewing all)
    requester_count: int | None = None          # admin-only
    requesters: list[str] | None = None          # admin-only usernames (system request shown as "system")
    origin: str = "request"                      # "request" · "goodreads" (waiting-on-hook) · "series"
    origin_detail: str | None = None             # for origin="series": the series name it was pulled from
    catalog_work_id: int | None = None           # the representative catalog row (opens the series modal)
    series: str | None = None                    # series name from the joined CatalogWork.extra (no detect)
    series_position: int | None = None           # volume number within the series, when known
    cover_url: str | None = None                 # the catalog row's cover art (Watchlist gallery thumbnail)
    sources: list[SourceSearchOut] | None = None  # per-durable-source search state (info-icon popover)


class MissingStatsOut(BaseModel):
    total: int = 0
    total_unavailable: int = 0
    by_status: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    next_due_at: datetime | None = None         # soonest pending re-check across unavailable rows


class RescanIn(BaseModel):
    """Mass-rescan scope — exactly one of these is set (validated in the endpoint)."""
    all: bool = False
    author: str | None = None
    series: str | None = None
    ids: list[int] | None = None


class RescanStatusOut(BaseModel):
    total: int = 0          # the active run's size (0 when idle)
    done: int = 0           # max(0, total - queued)
    queued: int = 0         # rows still holding rescan_queued_at
    active: bool = False    # queued > 0
