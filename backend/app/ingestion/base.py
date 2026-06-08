"""SourceAdapter interface, compliance declaration, and the adapter registry (Stage 6)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .fetcher import PoliteFetcher


class PermanentFetchError(Exception):
    """A chapter that can never be fetched as-is (e.g. members-only/paywalled content
    without credentials). The scheduler marks such chapters 'unavailable' and does NOT
    retry them — unlike transient failures, retrying only thrashes the source budget."""


class RateLimited(Exception):
    """The source is throttling or blocking us right now (e.g. a Cloudflare 403/challenge after a
    burst of headless renders) — NOT the chapter's fault. The scheduler cools the whole job down
    (exponential backoff) and resumes when the block lifts, instead of failing the chapter and
    hammering through the block."""


@dataclass(frozen=True)
class ComplianceDeclaration:
    """Every adapter MUST declare its compliance posture.

    The engine refuses to run an adapter (Source) whose `tos_permitted` is False.
    `needs_attestation` marks adapters (e.g. user-supplied feeds) where the operator
    must explicitly affirm they are permitted to ingest the target.
    """

    license_basis: str  # e.g. "public-domain", "cc0", "user-owned", "user-attested"
    tos_permitted_default: bool
    robots_respected: bool = True
    needs_attestation: bool = False
    min_request_interval_s: float = 5.0
    max_daily_requests: int = 500


@dataclass
class WorkMeta:
    source_work_ref: str
    title: str
    author: str | None = None
    description: str | None = None
    cover_url: str | None = None
    language: str | None = "en"
    status: str = "ongoing"  # ongoing | complete
    total_chapters_expected: int | None = None  # source-advertised total
    media_kind: str = "text"  # text | comic — drives reader/library treatment


@dataclass
class ChapterRef:
    source_chapter_ref: str
    index: int
    title: str = ""
    published_at: str | None = None  # ISO string if known


@dataclass
class RawChapter:
    title: str
    body: str
    fmt: str = "html"  # html | md | text
    published_at: str | None = None
    # Sequential crawling: the next chapter discovered from this page (if any).
    next_ref: str | None = None
    next_title: str | None = None


class SourceAdapter:
    """Abstract base. Subclasses target one source and declare compliance."""

    key: str = "abstract"
    display_name: str = "Abstract adapter"
    description: str = ""
    base_url: str | None = None
    compliance: ComplianceDeclaration = ComplianceDeclaration(
        license_basis="unknown", tos_permitted_default=False
    )
    enabled: bool = True

    def __init__(self, fetcher: PoliteFetcher, config: dict | None = None) -> None:
        self.fetcher = fetcher
        # Per-source settings/credentials from Source.config (e.g. a members-only access token).
        self.config = config or {}

    async def discover_work(self, ref: str) -> WorkMeta:  # pragma: no cover - interface
        raise NotImplementedError

    async def list_chapters(self, meta: WorkMeta) -> list[ChapterRef]:  # pragma: no cover
        raise NotImplementedError

    async def fetch_chapter(self, ref: ChapterRef) -> RawChapter:  # pragma: no cover
        raise NotImplementedError


@dataclass
class AdapterRegistry:
    _adapters: dict[str, type[SourceAdapter]] = field(default_factory=dict)

    def register(self, adapter_cls: type[SourceAdapter]) -> type[SourceAdapter]:
        self._adapters[adapter_cls.key] = adapter_cls
        return adapter_cls

    def get(self, key: str) -> type[SourceAdapter]:
        if key not in self._adapters:
            raise KeyError(f"Unknown adapter: {key!r}")
        return self._adapters[key]

    def all(self) -> list[type[SourceAdapter]]:
        return list(self._adapters.values())


registry = AdapterRegistry()
