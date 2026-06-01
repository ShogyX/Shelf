"""Local import adapter (Stage 8) + shared chapterization helpers.

Handles EPUB (via ebooklib), TXT and Markdown files the operator legally owns.
The actual import is driven by an upload endpoint (no crawl job), but the
chapterization helpers here are reused by the Standard Ebooks adapter.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from ..base import ComplianceDeclaration, SourceAdapter, registry


@dataclass
class ParsedChapter:
    index: int
    title: str
    body_html: str


def _split_html_by_headings(html: str, fallback_title: str = "Chapter") -> list[ParsedChapter]:
    """Split a single HTML document into chapters at h1/h2/h3 boundaries."""
    soup = BeautifulSoup(html, "lxml")
    body = soup.body or soup
    chapters: list[ParsedChapter] = []
    current_title = fallback_title
    current_nodes: list[str] = []

    def flush() -> None:
        nonlocal current_nodes, current_title
        inner = "".join(current_nodes).strip()
        if inner:
            chapters.append(
                ParsedChapter(index=len(chapters) + 1, title=current_title, body_html=inner)
            )
        current_nodes = []

    for el in list(body.children):
        name = getattr(el, "name", None)
        if name in ("h1", "h2", "h3"):
            flush()
            current_title = el.get_text(" ", strip=True) or fallback_title
            current_nodes = []
        else:
            text = str(el)
            if text.strip():
                current_nodes.append(text)
    flush()

    if not chapters:
        chapters = [ParsedChapter(index=1, title=fallback_title, body_html=html)]
    return chapters


def chapterize_epub(data: bytes) -> tuple[dict, list[ParsedChapter]]:
    """Return (metadata, chapters) from EPUB bytes using ebooklib."""
    from ebooklib import epub  # imported lazily so the app boots without it during tests

    book = epub.read_epub(io.BytesIO(data))

    def meta(name: str) -> str | None:
        items = book.get_metadata("DC", name)
        return items[0][0] if items else None

    metadata = {
        "title": meta("title") or "Untitled",
        "author": meta("creator"),
        "description": meta("description"),
        "language": meta("language") or "en",
    }

    chapters: list[ParsedChapter] = []
    # Honour spine order so chapters are sequential.
    for spine_id, _ in book.spine:
        item = book.get_item_with_id(spine_id)
        if item is None:
            continue
        try:
            html = item.get_content().decode("utf-8", errors="replace")
        except Exception:
            continue
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)
        if len(text) < 40:  # skip cover/nav/empty docs
            continue
        heading = soup.find(["h1", "h2", "h3", "title"])
        title = heading.get_text(" ", strip=True) if heading else f"Chapter {len(chapters) + 1}"
        body = soup.body.decode_contents() if soup.body else html
        chapters.append(
            ParsedChapter(index=len(chapters) + 1, title=title or f"Chapter {len(chapters)+1}",
                          body_html=body)
        )
    if not chapters:
        chapters = [ParsedChapter(index=1, title=metadata["title"], body_html="")]
    return metadata, chapters


def extract_epub_cover(data: bytes) -> tuple[bytes, str] | None:
    """Return (image_bytes, mime) for an EPUB's cover image, if present."""
    import io

    from ebooklib import ITEM_COVER, ITEM_IMAGE, epub

    book = epub.read_epub(io.BytesIO(data))
    # 1) Explicit cover items.
    for item in book.get_items_of_type(ITEM_COVER):
        content = item.get_content()
        if content:
            return content, item.media_type or "image/jpeg"
    # 2) An image whose name/id looks like a cover.
    images = list(book.get_items_of_type(ITEM_IMAGE))
    for item in images:
        name = (item.get_name() or "").lower()
        if "cover" in name or "cover" in (item.id or "").lower():
            return item.get_content(), item.media_type or "image/jpeg"
    # 3) Fallback: the first image in the book.
    if images:
        return images[0].get_content(), images[0].media_type or "image/jpeg"
    return None


def chapterize_text(text: str, is_markdown: bool = False) -> tuple[dict, list[ParsedChapter]]:
    """Split plain text / markdown into chapters on heading-ish lines."""
    from ..adapters._mdtext import text_to_paragraph_html

    lines = text.splitlines()
    # Detect chapter boundaries: markdown headings or "Chapter N" lines.
    boundary = re.compile(r"^\s*(#{1,3}\s+.+|chapter\s+\w+.*|prologue.*|epilogue.*)\s*$", re.I)
    chapters: list[ParsedChapter] = []
    cur_title = "Chapter 1"
    cur_lines: list[str] = []

    def flush() -> None:
        nonlocal cur_lines, cur_title
        body = "\n".join(cur_lines).strip()
        if body:
            chapters.append(
                ParsedChapter(
                    index=len(chapters) + 1,
                    title=cur_title,
                    body_html=text_to_paragraph_html(body),
                )
            )
        cur_lines = []

    for line in lines:
        if boundary.match(line):
            flush()
            cur_title = re.sub(r"^#+\s*", "", line.strip()) or f"Chapter {len(chapters)+1}"
        else:
            cur_lines.append(line)
    flush()
    if not chapters:
        chapters = [
            ParsedChapter(index=1, title="Imported text", body_html=text_to_paragraph_html(text))
        ]
    return {"title": "Imported document", "author": None, "language": "en"}, chapters


@registry.register
class LocalImportAdapter(SourceAdapter):
    key = "local_import"
    display_name = "Local import (EPUB / TXT / Markdown)"
    description = "Upload an EPUB, text or Markdown file you legally own. No network access."
    base_url = None
    enabled = True
    compliance = ComplianceDeclaration(
        license_basis="user-owned",
        tos_permitted_default=True,
        robots_respected=False,
        needs_attestation=False,
        min_request_interval_s=0.0,
        max_daily_requests=1000000,
    )
    # No discover/list/fetch — import is handled by the upload endpoint directly.
