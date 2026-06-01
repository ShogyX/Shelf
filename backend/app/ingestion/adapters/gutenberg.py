"""Project Gutenberg adapter (Stage 8) — public domain.

Reads directly from gutenberg.org (whose robots.txt permits the content paths used
here — only /ebooks/search is disallowed). Metadata comes from the ebook landing
page; content comes from the HTML or plain-text edition, split into chapters.

The split book is cached per-process so the slow backfill re-reads it for each
chapter without re-downloading.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from ..base import ChapterRef, ComplianceDeclaration, RawChapter, SourceAdapter, WorkMeta, registry
from ..extract import og_image
from .local_import import ParsedChapter, _split_html_by_headings

GUT = "https://www.gutenberg.org"

# Per-process cache: gutenberg book id -> [ParsedChapter].
_BOOK_CACHE: dict[str, list[ParsedChapter]] = {}


def _content_candidates(book_id: str) -> list[str]:
    return [
        f"{GUT}/files/{book_id}/{book_id}-h/{book_id}-h.htm",
        f"{GUT}/cache/epub/{book_id}/pg{book_id}-images.html",
        f"{GUT}/cache/epub/{book_id}/pg{book_id}.html",
        f"{GUT}/cache/epub/{book_id}/pg{book_id}.txt.utf8",
        f"{GUT}/files/{book_id}/{book_id}-0.txt",
        f"{GUT}/cache/epub/{book_id}/pg{book_id}.txt",
    ]


def _text_to_html(text: str) -> str:
    return "".join(f"<p>{p.strip()}</p>" for p in text.split("\n\n") if p.strip())


@registry.register
class GutenbergAdapter(SourceAdapter):
    key = "gutenberg"
    display_name = "Project Gutenberg"
    description = "Public-domain books read directly from gutenberg.org. Reuse-permitted."
    base_url = GUT
    enabled = True
    compliance = ComplianceDeclaration(
        license_basis="public-domain",
        tos_permitted_default=True,
        robots_respected=True,
        needs_attestation=False,
        min_request_interval_s=3.0,
        max_daily_requests=300,
    )

    async def discover_work(self, ref: str) -> WorkMeta:
        book_id = ref.strip().lstrip("#")
        resp = await self.fetcher.get(self.key, f"{GUT}/ebooks/{book_id}")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        og = soup.select_one('meta[property="og:title"]')
        h1 = soup.find("h1")
        raw_title = (og["content"] if og and og.has_attr("content") else None) or (
            h1.get_text(" ", strip=True) if h1 else f"Gutenberg #{book_id}"
        )
        # Landing-page titles look like "Title by Author"; keep just the title.
        title = raw_title.split(" by ")[0].strip() if " by " in raw_title else raw_title
        author_el = soup.select_one('[itemprop="creator"]') or soup.select_one(
            'a[rel="marcrel:aut"]'
        )
        author = author_el.get_text(" ", strip=True) if author_el else None
        lang_el = soup.select_one('[itemprop="inLanguage"]')
        language = "en"
        if lang_el:
            # Text is like "Language English"; strip the label.
            language = lang_el.get_text(" ", strip=True).replace("Language", "").strip() or "en"

        cover = og_image(resp.text, GUT) or (
            f"{GUT}/cache/epub/{book_id}/pg{book_id}.cover.medium.jpg"
        )

        return WorkMeta(
            source_work_ref=book_id,
            title=title,
            author=author,
            description=None,
            cover_url=cover,
            language=language,
            status="complete",
        )

    async def _ensure_split(self, book_id: str) -> list[ParsedChapter]:
        if book_id in _BOOK_CACHE:
            return _BOOK_CACHE[book_id]
        last_err: Exception | None = None
        for url in _content_candidates(book_id):
            try:
                if not await self.fetcher.allowed(self.key, url):
                    continue
                resp = await self.fetcher.get(self.key, url)
                if resp.status_code != 200 or not resp.text.strip():
                    continue
                if url.endswith((".txt", ".txt.utf8")) or "-0.txt" in url:
                    html = _text_to_html(resp.text)
                else:
                    html = resp.text
                chapters = _split_html_by_headings(html, fallback_title=f"Book {book_id}")
                _BOOK_CACHE[book_id] = chapters
                return chapters
            except Exception as exc:  # try the next candidate
                last_err = exc
                continue
        raise RuntimeError(f"No readable edition found for Gutenberg #{book_id}: {last_err}")

    async def list_chapters(self, meta: WorkMeta) -> list[ChapterRef]:
        chapters = await self._ensure_split(meta.source_work_ref)
        return [
            ChapterRef(
                source_chapter_ref=f"{meta.source_work_ref}#{c.index}",
                index=c.index,
                title=c.title,
            )
            for c in chapters
        ]

    async def fetch_chapter(self, ref: ChapterRef) -> RawChapter:
        book_id = ref.source_chapter_ref.split("#", 1)[0]
        chapters = await self._ensure_split(book_id)
        match = next((c for c in chapters if c.index == ref.index), None)
        if match is None:
            raise RuntimeError(f"Chapter {ref.index} not found in book {book_id}")
        body = f"<h2>{match.title}</h2>{match.body_html}" if match.title else match.body_html
        return RawChapter(title=match.title or ref.title, body=body, fmt="html")
