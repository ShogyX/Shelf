"""Local import adapter (Stage 8) + shared chapterization helpers.

Handles EPUB (via ebooklib), TXT and Markdown files the operator legally owns.
The actual import is driven by an upload endpoint (no crawl job), but the
chapterization helpers here are reused by the Standard Ebooks adapter.
"""
from __future__ import annotations

import html as html_mod
import io
import re
import warnings
import zipfile
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

try:  # EPUB chapter docs are XHTML; silence the lxml-HTML-parser warning.
    from bs4 import XMLParsedAsHTMLWarning

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except Exception:  # pragma: no cover
    pass

from ..base import ComplianceDeclaration, SourceAdapter, registry


@dataclass
class ParsedChapter:
    index: int
    title: str
    body_html: str


_HEADINGS = ("h1", "h2", "h3")
# Wrapper elements we descend INTO so that headings nested inside them still act as
# chapter boundaries (Project Gutenberg and many sites wrap each chapter in a div).
_CONTAINERS = ("div", "section", "article", "main", "body")


def _strip_boilerplate(soup: BeautifulSoup) -> None:
    """Remove non-content cruft: scripts/styles and Project Gutenberg's header/footer
    license blocks (which otherwise leak into the first/last 'chapter')."""
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    for sel in (
        "#pg-header", "#pg-footer", "#pg-machine-header", ".pg-boilerplate",
        "section.pg-boilerplate", "pre.pg-boilerplate",
    ):
        for el in soup.select(sel):
            el.decompose()


def _has_text(html: str) -> bool:
    return bool(BeautifulSoup(html, "lxml").get_text(strip=True))


def _iter_segments(node):
    """Yield ('h', title) at each heading and ('c', html) for content blocks, in
    document order — descending into wrapper containers that themselves hold headings,
    so nested chapter structures split correctly instead of collapsing into one blob."""
    for el in node.children:
        name = getattr(el, "name", None)
        if name in _HEADINGS:
            yield ("h", el.get_text(" ", strip=True))
        elif name is None:  # NavigableString
            s = str(el)
            if s.strip():
                yield ("c", s)
        elif name in _CONTAINERS and el.find(_HEADINGS):
            yield from _iter_segments(el)
        else:
            yield ("c", str(el))


def _split_html_by_headings(
    html: str, fallback_title: str = "Chapter", base_url: str = ""
) -> list[ParsedChapter]:
    """Split a single HTML document into chapters at h1/h2/h3 boundaries.

    Robust to chapters wrapped in container <div>s (the common Project Gutenberg /
    web layout) and strips publisher boilerplate. A 'chapter' is only emitted when it
    holds real readable text, so we never produce empty chapters. When ``base_url`` is
    given, relative <img>/<a> URLs are made absolute so an illustrated book's images
    actually load in the reader (e.g. Gutenberg's images/foo.jpg → full URL)."""
    soup = BeautifulSoup(html, "lxml")
    _strip_boilerplate(soup)
    if base_url:
        for img in soup.find_all("img", src=True):
            img["src"] = urljoin(base_url, img["src"])
        for a in soup.find_all("a", href=True):
            a["href"] = urljoin(base_url, a["href"])
    body = soup.body or soup
    chapters: list[ParsedChapter] = []
    current_title = fallback_title
    current_nodes: list[str] = []

    def flush() -> None:
        nonlocal current_nodes, current_title
        inner = "".join(current_nodes).strip()
        if inner and _has_text(inner):
            chapters.append(
                ParsedChapter(index=len(chapters) + 1, title=current_title, body_html=inner)
            )
        current_nodes = []

    for kind, val in _iter_segments(body):
        if kind == "h":
            flush()
            current_title = val or fallback_title
        else:
            current_nodes.append(val)
    flush()

    if not chapters:
        whole = body.decode_contents()
        if _has_text(whole):
            chapters = [ParsedChapter(index=1, title=fallback_title, body_html=whole)]
    return chapters


def _chapterize_epub_tolerant(data: bytes) -> tuple[dict, list[ParsedChapter]]:
    """Spine-order chapterizer that reads straight from the zip and SKIPS missing internal resources
    (a fonts/image entry referenced in the manifest but absent). ebooklib's read_epub fails the whole
    book on such a gap even though the text is intact; this recovers it. The archive itself is already
    integrity-checked (CRCs) before we get here."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    opf_name = None
    if "META-INF/container.xml" in names:
        m = re.search(r'full-path="([^"]+\.opf)"', zf.read("META-INF/container.xml").decode("utf-8", "replace"))
        opf_name = m.group(1) if m else None
    opf_name = opf_name or next((n for n in names if n.lower().endswith(".opf")), None)
    if not opf_name:
        raise ValueError("no OPF in epub")
    opf = zf.read(opf_name).decode("utf-8", "replace")
    base = opf_name.rsplit("/", 1)[0] + "/" if "/" in opf_name else ""

    def _dc(tag: str) -> str | None:
        mt = re.search(rf"<dc:{tag}\b[^>]*>(.*?)</dc:{tag}>", opf, re.I | re.S)
        return html_mod.unescape(re.sub(r"<[^>]+>", "", mt.group(1))).strip() if mt else None

    metadata = {"title": _dc("title") or "Untitled", "author": _dc("creator"),
                "description": _dc("description"), "language": _dc("language") or "en"}
    manifest = {mid: href for mid, href in re.findall(r'<item\b[^>]*\bid="([^"]+)"[^>]*\bhref="([^"]+)"', opf, re.I)}
    manifest.update({mid: href for href, mid in re.findall(r'<item\b[^>]*\bhref="([^"]+)"[^>]*\bid="([^"]+)"', opf, re.I)})
    spine = re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf, re.I)
    chapters: list[ParsedChapter] = []
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        from urllib.parse import unquote
        full = base + unquote(href.split("#")[0])
        if full not in names:           # missing content doc → skip, don't fail the book
            continue
        try:
            raw = zf.read(full).decode("utf-8", errors="replace")
        except Exception:               # bad CRC on this one doc → skip it, keep the rest
            continue
        soup = BeautifulSoup(raw, "lxml")
        text = soup.get_text(" ", strip=True)
        if len(text) < 40:
            continue
        heading = soup.find(["h1", "h2", "h3", "title"])
        title = heading.get_text(" ", strip=True) if heading else f"Chapter {len(chapters) + 1}"
        body = soup.body.decode_contents() if soup.body else raw
        chapters.append(ParsedChapter(index=len(chapters) + 1, title=title or f"Chapter {len(chapters)+1}",
                                      body_html=body))
    if not chapters:
        chapters = [ParsedChapter(index=1, title=metadata["title"], body_html="")]
    return metadata, chapters


def chapterize_epub(data: bytes) -> tuple[dict, list[ParsedChapter]]:
    """Return (metadata, chapters) from EPUB bytes using ebooklib, falling back to a tolerant
    zip-spine reader when ebooklib chokes on a missing/strict internal resource."""
    from ebooklib import epub  # imported lazily so the app boots without it during tests

    try:
        book = epub.read_epub(io.BytesIO(data))
    except Exception:
        return _chapterize_epub_tolerant(data)

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
    # A single-file EPUB (or convert.py's _wrap_html_as_epub fallback) yields ONE giant chapter —
    # unnavigable. When the whole book collapsed to a single large doc, re-split it by headings so
    # it gets a real TOC (13C). Multi-spine books are already chapterized by their spine above.
    if len(chapters) == 1 and len(BeautifulSoup(chapters[0].body_html or "", "lxml")
                                  .get_text(" ", strip=True)) > 8000:
        split = _split_html_by_headings(chapters[0].body_html, fallback_title=metadata["title"])
        if len(split) > 1:
            chapters = split
    return metadata, chapters


def extract_epub_images(data: bytes) -> dict[str, tuple[bytes, str]]:
    """Return {basename: (bytes, mime)} for every image embedded in an EPUB, so an
    illustrated book's inline <img> tags (which point at EPUB-internal paths the reader
    can't resolve) can be re-served from /media. Keyed by basename — EPUB hrefs are
    relative (e.g. '../Images/fig1.jpg') but the filename is what we match on."""
    from ebooklib import ITEM_IMAGE, epub

    out: dict[str, tuple[bytes, str]] = {}
    try:
        book = epub.read_epub(io.BytesIO(data))
    except Exception:
        return out
    for item in book.get_items_of_type(ITEM_IMAGE):
        name = (item.get_name() or "").rsplit("/", 1)[-1]
        if not name:
            continue
        try:
            out[name] = (item.get_content(), item.media_type or "image/jpeg")
        except Exception:
            continue
    return out


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
