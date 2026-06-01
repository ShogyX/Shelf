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


class HookIn(BaseModel):
    source_key: str
    work_ref: str


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
    created_at: datetime


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
