"""Standard Ebooks adapter (Stage 8) — public domain / CC0.

Standard Ebooks publishes meticulously produced public-domain editions. We accept
an ebook page URL (or `author/title` slug), derive the compatible EPUB download,
and chapterize it with ebooklib. Content is public domain; the SE *typography/markup*
effort is released under CC0.
"""
from __future__ import annotations

from ..base import ChapterRef, ComplianceDeclaration, RawChapter, SourceAdapter, WorkMeta, registry
from .local_import import ParsedChapter, chapterize_epub

SE_BASE = "https://standardebooks.org"

# Per-process cache: slug -> (metadata, [ParsedChapter]).
_EPUB_CACHE: dict[str, tuple[dict, list[ParsedChapter]]] = {}


def _normalize(ref: str) -> tuple[str, str]:
    """Return (page_url, slug) for a SE ref given a URL or `author/title` slug."""
    ref = ref.strip().rstrip("/")
    if ref.startswith("http"):
        path = ref.split("/ebooks/", 1)[-1]
    else:
        path = ref.lstrip("/")
    if path.startswith("ebooks/"):
        path = path[len("ebooks/"):]
    slug = path.replace("/", "_")
    page_url = f"{SE_BASE}/ebooks/{path}"
    return page_url, slug


def _epub_url(page_url: str, slug: str) -> str:
    return f"{page_url}/downloads/{slug}.epub"


@registry.register
class StandardEbooksAdapter(SourceAdapter):
    key = "standardebooks"
    display_name = "Standard Ebooks"
    description = "Public-domain editions (content public domain, markup CC0). Reuse-permitted."
    base_url = SE_BASE
    enabled = True
    compliance = ComplianceDeclaration(
        license_basis="public-domain/cc0",
        tos_permitted_default=True,
        robots_respected=True,
        needs_attestation=False,
        min_request_interval_s=4.0,
        max_daily_requests=200,
    )

    async def _ensure(self, ref: str) -> tuple[dict, list[ParsedChapter], str]:
        page_url, slug = _normalize(ref)
        if slug not in _EPUB_CACHE:
            resp = await self.fetcher.get(self.key, _epub_url(page_url, slug))
            resp.raise_for_status()
            meta, chapters = chapterize_epub(resp.content)
            _EPUB_CACHE[slug] = (meta, chapters)
        meta, chapters = _EPUB_CACHE[slug]
        return meta, chapters, slug

    async def discover_work(self, ref: str) -> WorkMeta:
        meta, _chapters, slug = await self._ensure(ref)
        page_url, _slug = _normalize(ref)
        return WorkMeta(
            source_work_ref=slug,
            title=meta.get("title", slug),
            author=meta.get("author"),
            description=meta.get("description"),
            cover_url=f"{page_url}/downloads/cover.jpg",  # SE's published cover
            language=meta.get("language", "en"),
            status="complete",
        )

    async def list_chapters(self, meta: WorkMeta) -> list[ChapterRef]:
        _m, chapters, _slug = await self._ensure(meta.source_work_ref)
        return [
            ChapterRef(source_chapter_ref=f"{meta.source_work_ref}#{c.index}", index=c.index,
                       title=c.title)
            for c in chapters
        ]

    async def fetch_chapter(self, ref: ChapterRef) -> RawChapter:
        slug = ref.source_chapter_ref.split("#", 1)[0]
        _m, chapters, _slug = await self._ensure(slug)
        match = next((c for c in chapters if c.index == ref.index), None)
        if match is None:
            raise RuntimeError(f"Chapter {ref.index} not found for {slug}")
        return RawChapter(title=match.title, body=match.body_html, fmt="html")
