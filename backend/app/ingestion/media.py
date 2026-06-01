"""Unified reading-media parsing.

Dispatches a file (by extension) to the right parser and returns a normalized
``ParsedMedia``: metadata, an ordered list of ``ParsedChapter`` (HTML bodies), and
an optional cover image. Reused by the local-import upload endpoint and the
watched-local-folder sync.

Supported media:
  * EPUB / TXT / Markdown — delegated to the existing local_import helpers.
  * PDF                    — text extracted per page via pypdf, chapterized by
                             outline (bookmarks) when present, else one chapter.
  * CBZ / CBR (comics)     — page images extracted to the media dir; each chapter
                             body is an <img> gallery the HTML reader renders.
"""
from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass, field

from ..media import comic_dir, comic_url
from ..sanitize import text_to_html
from .adapters.local_import import (
    ParsedChapter,
    chapterize_epub,
    chapterize_text,
    extract_epub_cover,
)

TEXT_EXTS = (".txt", ".text")
MD_EXTS = (".md", ".markdown")
EPUB_EXTS = (".epub",)
PDF_EXTS = (".pdf",)
COMIC_EXTS = (".cbz", ".cbr")
SUPPORTED_EXTS = TEXT_EXTS + MD_EXTS + EPUB_EXTS + PDF_EXTS + COMIC_EXTS

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_IMAGE_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


@dataclass
class ParsedMedia:
    title: str
    chapters: list[ParsedChapter]
    author: str | None = None
    description: str | None = None
    language: str = "en"
    # (bytes, mime) for cover art, when the format embeds one.
    cover: tuple[bytes, str] | None = None
    # "text" | "comic" — drives reader rendering hints.
    kind: str = "text"
    meta: dict = field(default_factory=dict)


def ext_of(filename: str) -> str:
    name = filename.lower()
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def is_supported(filename: str) -> bool:
    return ext_of(filename) in SUPPORTED_EXTS


def _stem(filename: str) -> str:
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[:dot] if dot > 0 else base


# --------------------------------------------------------------------------- PDF
def _parse_pdf(data: bytes, filename: str) -> ParsedMedia:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    info = reader.metadata or {}
    title = (getattr(info, "title", None) or _stem(filename)).strip() or _stem(filename)
    author = getattr(info, "author", None)

    # Page text, lightly paragraphed.
    page_text: list[str] = []
    for page in reader.pages:
        try:
            page_text.append(page.extract_text() or "")
        except Exception:
            page_text.append("")

    chapters: list[ParsedChapter] = []
    outline = _pdf_outline_ranges(reader)
    if outline:
        for idx, (ch_title, start, end) in enumerate(outline, start=1):
            body = "\n\n".join(t for t in page_text[start:end] if t.strip())
            if not body.strip():
                continue
            chapters.append(
                ParsedChapter(index=len(chapters) + 1, title=ch_title or f"Section {idx}",
                              body_html=text_to_html(body))
            )
    if not chapters:
        body = "\n\n".join(t for t in page_text if t.strip())
        chapters = [ParsedChapter(index=1, title=title, body_html=text_to_html(body))]

    return ParsedMedia(title=title, author=author, chapters=chapters, kind="text")


def _pdf_outline_ranges(reader) -> list[tuple[str, int, int]]:
    """Flatten the PDF outline into (title, start_page, end_page) ranges."""
    try:
        outline = reader.outline
    except Exception:
        return []
    items: list[tuple[str, int]] = []

    def walk(nodes) -> None:
        for node in nodes:
            if isinstance(node, list):
                walk(node)
                continue
            try:
                title = str(getattr(node, "title", "") or "").strip()
                page = reader.get_destination_page_number(node)
            except Exception:
                continue
            if title and page is not None:
                items.append((title, int(page)))

    try:
        walk(outline)
    except Exception:
        return []
    if len(items) < 2:
        return []
    items.sort(key=lambda x: x[1])
    n = len(reader.pages)
    ranges: list[tuple[str, int, int]] = []
    for i, (title, start) in enumerate(items):
        end = items[i + 1][1] if i + 1 < len(items) else n
        ranges.append((title, start, max(start + 1, end)))
    return ranges


# ------------------------------------------------------------------------- comics
def _comic_images(data: bytes, ext: str) -> list[tuple[str, bytes]]:
    """Return ordered [(name, bytes)] of page images inside a CBZ/CBR archive."""
    entries: list[tuple[str, bytes]] = []
    if ext == ".cbz":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if ext_of(n) in _IMAGE_EXTS]
            for name in _natural_sorted(names):
                entries.append((name, zf.read(name)))
    elif ext == ".cbr":
        try:
            import rarfile
        except Exception as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "CBR support needs the 'rarfile' package and an unrar/unar binary."
            ) from exc
        with rarfile.RarFile(io.BytesIO(data)) as rf:
            names = [n for n in rf.namelist() if ext_of(n) in _IMAGE_EXTS]
            for name in _natural_sorted(names):
                entries.append((name, rf.read(name)))
    return entries


def _natural_sorted(names: list[str]) -> list[str]:
    import re

    def key(s: str):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]

    return sorted(names, key=key)


def _parse_comic(data: bytes, filename: str) -> ParsedMedia:
    ext = ext_of(filename)
    images = _comic_images(data, ext)
    title = _stem(filename)
    if not images:
        return ParsedMedia(
            title=title, kind="comic",
            chapters=[ParsedChapter(index=1, title=title, body_html="<p>(no pages found)</p>")],
        )

    # Stable per-file key so re-syncing the same archive overwrites in place.
    key = hashlib.sha1(filename.encode("utf-8")).hexdigest()[:16]
    out_dir = comic_dir(key)
    cover: tuple[bytes, str] | None = None
    parts: list[str] = []
    for i, (name, blob) in enumerate(images, start=1):
        img_ext = ext_of(name) or ".jpg"
        fname = f"{i:04d}{img_ext}"
        (out_dir / fname).write_bytes(blob)
        if cover is None:
            cover = (blob, _IMAGE_MIME.get(img_ext, "image/jpeg"))
        parts.append(
            f'<figure class="comic-page"><img src="{comic_url(key, fname)}" '
            f'alt="Page {i}" loading="lazy"/></figure>'
        )

    body = '<div class="comic">' + "\n".join(parts) + "</div>"
    chapter = ParsedChapter(index=1, title=title, body_html=body)
    return ParsedMedia(
        title=title, kind="comic", cover=cover, chapters=[chapter],
        meta={"pages": len(images)},
    )


# --------------------------------------------------------------------------- API
def parse_media(data: bytes, filename: str) -> ParsedMedia:
    """Parse any supported media file from its bytes + filename."""
    ext = ext_of(filename)
    if ext in EPUB_EXTS:
        meta, chapters = chapterize_epub(data)
        cover = None
        try:
            cover = extract_epub_cover(data)
        except Exception:
            cover = None
        return ParsedMedia(
            title=meta.get("title") or _stem(filename),
            author=meta.get("author"),
            description=meta.get("description"),
            language=meta.get("language") or "en",
            chapters=chapters, cover=cover, kind="text",
        )
    if ext in PDF_EXTS:
        return _parse_pdf(data, filename)
    if ext in COMIC_EXTS:
        return _parse_comic(data, filename)
    # text / markdown / unknown-text
    text = data.decode("utf-8", errors="replace")
    meta, chapters = chapterize_text(text, is_markdown=ext in MD_EXTS)
    title = meta.get("title")
    if title in (None, "Imported document"):
        title = _stem(filename)
    return ParsedMedia(
        title=title, author=meta.get("author"),
        language=meta.get("language") or "en", chapters=chapters, kind="text",
    )
