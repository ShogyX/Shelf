"""Pydantic v2 response/request schemas."""
from __future__ import annotations

from datetime import datetime
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


class SourceUpdate(BaseModel):
    tos_permitted: bool | None = None
    robots_respected: bool | None = None
    render_js: bool | None = None
    min_request_interval_s: float | None = Field(default=None, ge=0)
    max_daily_requests: int | None = Field(default=None, ge=0)
    display_name: str | None = None
    base_url: str | None = None


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
    total_chapters_known: int
    total_chapters_expected: int | None = None
    chapters_fetched: int = 0
    health: str = "unknown"
    health_detail: str | None = None
    last_checked_at: datetime | None = None
    last_update_at: datetime | None = None
    crawl_interval_s: float | None = None
    crawl_daily_limit: int | None = None
    crawl_window_start: int | None = None
    crawl_window_end: int | None = None


class WorkDetailOut(WorkOut):
    chapters_total: int = 0
    chapters_read: int = 0
    last_chapter_id: int | None = None
    scroll_fraction: float = 0.0


class ChapterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    work_id: int
    index: int
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
    crawl_daily_limit: int | None = Field(default=None, ge=0)
    crawl_window_start: int | None = Field(default=None, ge=0, le=23)
    crawl_window_end: int | None = Field(default=None, ge=0, le=23)


class HookIn(BaseModel):
    source_key: str
    work_ref: str
    # Optional per-title crawl policy applied at hook time.
    crawl_interval_s: float | None = Field(default=None, ge=0)
    crawl_daily_limit: int | None = Field(default=None, ge=0)
    crawl_window_start: int | None = Field(default=None, ge=0, le=23)
    crawl_window_end: int | None = Field(default=None, ge=0, le=23)


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
    delivery: dict[str, Any] = {}  # masked SMTP config + personal email


class SettingsIn(BaseModel):
    theme: str | None = None
    reader_prefs: dict[str, Any] | None = None
    kindle_email: str | None = None
    delivery: dict[str, Any] | None = None  # smtp_* fields + email_to (password write-only)


class SendToKindleIn(BaseModel):
    to: str | None = None  # explicit recipient (Kindle or personal email)
    kindle_email: str | None = None  # back-compat alias
    start: int = Field(default=1, ge=1)
    limit: int | None = Field(default=None, ge=1)


class SendToKindleOut(BaseModel):
    sent: bool
    chapters: int
    to: str


class IndexSiteIn(BaseModel):
    url: str
    max_pages: int | None = Field(default=None, ge=1, le=5000)
    max_depth: int | None = Field(default=None, ge=0, le=10)
    same_host_only: bool = True


class IndexSiteOut(BaseModel):
    id: int
    root_url: str
    domain: str
    title: str | None
    status: str
    max_pages: int
    max_depth: int
    same_host_only: bool
    last_error: str | None = None
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
    site_id: int | None = None
    domain: str
    work_url: str
    provider: str = "web_index"        # web_index | readarr | kapowarr
    kind: str = "online"               # online | readarr | kapowarr
    integration_id: int | None = None
    chapters_advertised: int | None = None
    chapters_listed: int | None = None
    health: str = "unknown"
    health_detail: str | None = None
    hooked_work_id: int | None = None
    grab_status: str | None = None     # set once a grab has been requested


class GrabOut(BaseModel):
    ok: bool
    integration: str | None = None
    message: str


class CatalogGroupOut(BaseModel):
    """A discovered work, merged across the sites that carry it."""
    norm_key: str
    title: str
    author: str | None = None
    cover_url: str | None = None
    synopsis: str | None = None
    language: str | None = None
    media_kind: str = "text"
    chapters: int | None = None
    hooked_work_id: int | None = None
    sources: list[CatalogSourceOut] = []


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
    kind: str = Field(pattern="^(readarr|kapowarr)$")
    name: str | None = None
    base_url: str
    api_key: str
    enabled: bool = True
    root_folder: str | None = None
    auto_map_folders: bool = True


class IntegrationUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None        # omit/None = keep existing
    enabled: bool | None = None
    root_folder: str | None = None
    auto_map_folders: bool | None = None


class IntegrationOut(BaseModel):
    id: int
    kind: str
    name: str
    base_url: str
    enabled: bool
    root_folder: str | None = None
    auto_map_folders: bool = True
    has_api_key: bool = False         # the key itself is never returned
    last_sync_at: datetime | None = None
    last_error: str | None = None
    catalog_count: int = 0


class IntegrationTestOut(BaseModel):
    ok: bool
    app: str | None = None
    version: str | None = None
    root_folders: list[str] = []
    error: str | None = None


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
    role: str
    is_active: bool
    created_at: datetime


class MeOut(BaseModel):
    authenticated: bool
    needs_setup: bool
    user: UserOut | None = None


class LoginIn(BaseModel):
    username: str
    password: str


class SetupIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8)
    display_name: str | None = None
    token: str | None = None  # required if SHELF_SETUP_TOKEN is configured


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8)
    display_name: str | None = None
    role: str = "user"  # admin | user


class UserUpdate(BaseModel):
    password: str | None = Field(default=None, min_length=8)
    display_name: str | None = None
    role: str | None = None
    is_active: bool | None = None


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
