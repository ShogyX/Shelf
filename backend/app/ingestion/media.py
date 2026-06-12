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

from ..media import book_dir, book_url, comic_dir, comic_url
from ..sanitize import text_to_html
from .adapters.local_import import (
    ParsedChapter,
    chapterize_epub,
    chapterize_text,
    extract_epub_cover,
    extract_epub_images,
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


def _externalize_epub_images(data: bytes, filename: str, chapters: list[ParsedChapter]) -> None:
    """Extract an EPUB's inline images to /media/books/<key>/ and rewrite each chapter's
    <img src> (originally an EPUB-internal href) to the served URL, matched by basename.
    Mutates the chapters' body_html in place. No-op when the book has no images."""
    from bs4 import BeautifulSoup

    images = extract_epub_images(data)
    if not images:
        return
    key = hashlib.sha1(filename.encode("utf-8")).hexdigest()[:16]
    out_dir = book_dir(key)
    url_by_name: dict[str, str] = {}
    for name, (blob, _mime) in images.items():
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in name)[:120]
        (out_dir / safe).write_bytes(blob)
        url_by_name[name] = book_url(key, safe)
    for ch in chapters:
        if not ch.body_html or "<img" not in ch.body_html:
            continue
        soup = BeautifulSoup(ch.body_html, "lxml")
        changed = False
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("xlink:href") or ""
            base = src.split("#", 1)[0].split("?", 1)[0].rsplit("/", 1)[-1]
            if base in url_by_name:
                img["src"] = url_by_name[base]
                changed = True
        if changed:
            ch.body_html = soup.body.decode_contents() if soup.body else str(soup)


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
    ext = ext_of(filename)
    if ext in SUPPORTED_EXTS:
        return True
    # Kindle/other formats (mobi/azw3/…) are supported ONLY when a converter is installed —
    # parse_media transparently converts them to EPUB. Gated on availability so a system without
    # calibre/mobi doesn't log an import error for every such file it scans (E4).
    from . import convert
    return ext in convert.CONVERTIBLE_EXTS and convert.available()


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

    # Image-only / scanned PDF: pypdf extracts ~nothing per page. Rather than store a blank work,
    # render each page to an image and import it as a comic-style gallery so it's actually readable
    # (13C). Conservative threshold (≈16 chars/page) → only genuinely image-only PDFs trip it, and it
    # needs PyMuPDF; without the extra (or on a render failure) we fall through to the text path.
    total_text = sum(len(t.strip()) for t in page_text)
    pages = len(page_text)
    # Require ≥2 pages: a 1-page no-text PDF is usually a blank/degenerate doc (nothing to render),
    # whereas a multi-page no-text PDF is almost always a scan.
    if pages >= 2 and total_text < 16 * pages:
        images = _pdf_render_pages(data)
        if images:
            return _image_gallery_media(images, filename, title, author)

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
        # No bookmarks (the common case). A short doc/article → one chapter; a LONG one → fixed
        # page-range chapters so it's navigable instead of a single unreadable blob (13C). PDF text
        # has no reliable heading structure, so page-range chunking is the robust split.
        nonempty = [i for i, t in enumerate(page_text) if t.strip()]
        if len(nonempty) > 25:
            _PAGES_PER = 20
            for start in range(0, len(page_text), _PAGES_PER):
                end = min(start + _PAGES_PER, len(page_text))
                body = "\n\n".join(t for t in page_text[start:end] if t.strip())
                if not body.strip():
                    continue
                label = f"Pages {start + 1}–{end}"
                chapters.append(ParsedChapter(index=len(chapters) + 1, title=label,
                                              body_html=text_to_html(body)))
        if not chapters:
            body = "\n\n".join(t for t in page_text if t.strip())
            chapters = [ParsedChapter(index=1, title=title, body_html=text_to_html(body))]

    return ParsedMedia(title=title, author=author, chapters=chapters, kind="text")


def _pdf_render_pages(data: bytes, *, max_pages: int = 600, zoom: float = 2.0
                      ) -> list[tuple[str, bytes]]:
    """Render each PDF page to a PNG via PyMuPDF (for image-only/scanned PDFs). Returns
    ``[(name, png_bytes)]`` in page order, or ``[]`` when PyMuPDF isn't installed or rendering
    fails (the caller then falls back to the text path). ``zoom`` 2.0 ≈ 144 DPI — readable without
    bloating the gallery; a corrupt page is skipped rather than aborting the whole document."""
    try:
        import fitz  # PyMuPDF (optional 'pdf-scan' extra)
    except Exception:  # noqa: BLE001
        return []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[str, bytes]] = []
    try:
        mat = fitz.Matrix(zoom, zoom)
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            try:
                pix = page.get_pixmap(matrix=mat)
                out.append((f"{i + 1:04d}.png", pix.tobytes("png")))
            except Exception:  # noqa: BLE001 — skip a bad page, keep the rest
                continue
    finally:
        doc.close()
    return out


def _image_gallery_media(images: list[tuple[str, bytes]], filename: str,
                         title: str, author: str | None) -> ParsedMedia:
    """Build a comic-style image gallery ParsedMedia from rendered/extracted page images — used for
    scanned PDFs (and shareable with the comic path). Writes pages under a stable per-file key so a
    re-import overwrites in place; the first page is the cover."""
    key = hashlib.sha1(filename.encode("utf-8")).hexdigest()[:16]
    out_dir = comic_dir(key)
    cover: tuple[bytes, str] | None = None
    parts: list[str] = []
    for i, (name, blob) in enumerate(images, start=1):
        img_ext = ext_of(name) or ".png"
        fname = f"{i:04d}{img_ext}"
        (out_dir / fname).write_bytes(blob)
        if cover is None:
            cover = (blob, _IMAGE_MIME.get(img_ext, "image/png"))
        parts.append(
            f'<figure class="comic-page"><img src="{comic_url(key, fname)}" '
            f'alt="Page {i}" loading="lazy"/></figure>'
        )
    body = '<div class="comic">' + "\n".join(parts) + "</div>"
    return ParsedMedia(
        title=title, kind="comic", cover=cover, author=author,
        chapters=[ParsedChapter(index=1, title=title, body_html=body)],
        meta={"pages": len(images)},
    )


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
    # Dedup bookmarks sharing a start page (a chapter + its first sub-section both point at the same
    # page) — keep the first so each page belongs to ONE chapter; otherwise the shared page's text is
    # duplicated across chapters and an end<=start range gets force-widened, repeating a page (CC5).
    deduped: list[tuple[str, int]] = []
    seen_starts: set[int] = set()
    for title, page in items:
        if page in seen_starts:
            continue
        seen_starts.add(page)
        deduped.append((title, page))
    items = deduped
    n = len(reader.pages)
    ranges: list[tuple[str, int, int]] = []
    for i, (title, start) in enumerate(items):
        end = items[i + 1][1] if i + 1 < len(items) else n
        ranges.append((title, start, max(start + 1, end)))
    return ranges


# ------------------------------------------------------------------------- comics
def _read_comicinfo(data: bytes, ext: str) -> dict:
    """Parse ComicInfo.xml (the CBZ/CBR metadata standard) from the archive, if present. Returns a
    dict with any of title/series/number/author/description/language/cover_index — best-effort,
    {} on absence or any error. Without this a comic's title is always just the filename stem."""
    raw: bytes | None = None
    try:
        if ext == ".cbz":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                name = next((n for n in zf.namelist() if n.lower().endswith("comicinfo.xml")), None)
                if name:
                    raw = zf.read(name)
        elif ext == ".cbr":
            import rarfile
            with rarfile.RarFile(io.BytesIO(data)) as rf:
                name = next((n for n in rf.namelist() if n.lower().endswith("comicinfo.xml")), None)
                if name:
                    raw = rf.read(name)
    except Exception:  # noqa: BLE001 — metadata is optional; never fail the import on it
        return {}
    if not raw:
        return {}
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(raw)
    except Exception:  # noqa: BLE001
        return {}

    def _t(tag: str) -> str | None:
        el = root.find(tag)
        return el.text.strip() if (el is not None and el.text and el.text.strip()) else None

    out: dict = {}
    series, number, volume = _t("Series"), _t("Number"), _t("Volume")
    title = _t("Title")
    # Identity = "Series Number" (e.g. "Berserk 12") — the real title; the bare <Title> is usually
    # a chapter name. Fall back through what's present.
    if series and number:
        out["title"] = f"{series} {number}"
    elif series and volume:
        out["title"] = f"{series} Vol {volume}"
    elif series:
        out["title"] = series
    elif title:
        out["title"] = title
    if series:
        out["series"] = series
    author = _t("Writer") or _t("Penciller")
    if author:
        out["author"] = author.split(",")[0].strip()
    if _t("Summary"):
        out["description"] = _t("Summary")
    lang = _t("LanguageISO")
    if lang:
        out["language"] = lang.lower()[:5]
    # Front-cover page index (1-based in our page list), if the metadata marks one.
    try:
        for p in root.findall("./Pages/Page"):
            if (p.get("Type") or "").lower() == "frontcover":
                out["cover_index"] = int(p.get("Image", "0")) + 1  # ComicInfo Image is 0-based
                break
    except Exception:  # noqa: BLE001
        pass
    return out


def _comic_images(data: bytes, ext: str) -> list[tuple[str, bytes]]:
    """Return ordered [(name, bytes)] of page images inside a CBZ/CBR archive."""
    entries: list[tuple[str, bytes]] = []
    if ext == ".cbz":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if ext_of(n) in _IMAGE_EXTS]
            for name in _comic_page_order(names):
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
            for name in _comic_page_order(names):
                entries.append((name, rf.read(name)))
    return entries


def _natural_sorted(names: list[str]) -> list[str]:
    import re

    def key(s: str):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]

    return sorted(names, key=key)


def _comic_page_order(names: list[str]) -> list[str]:
    """Order comic page images. Natural-sort when the filenames are reliably numeric (the usual
    '001.jpg', 'page-12.png'); otherwise PRESERVE the archive's STORED order — archives without
    zero-padding, with non-numeric or mixed-folder names, or that rely on insertion order are
    mis-sequenced by a name sort (CC5)."""
    import re
    numeric = sum(1 for n in names if re.search(r"\d", n.rsplit("/", 1)[-1]))
    if names and numeric / len(names) >= 0.8:
        return _natural_sorted(names)
    return names  # stored (namelist) order


def _decode_text(data: bytes) -> str:
    """Decode a TXT/MD file to str, DETECTING the encoding instead of assuming UTF-8 — a
    1252/Latin-1/UTF-16/Shift-JIS file decoded as utf-8(errors=replace) becomes mojibake (13C).
    BOM sniff first (definitive), then charset-normalizer; falls back to utf-8(replace)."""
    if not data:
        return ""
    # BOM-aware codecs (utf-16/utf-32) consume + STRIP the BOM; check the 4-byte BOMs before the
    # 2-byte ones (utf-32-LE starts with the utf-16-LE BOM bytes).
    for bom, enc in ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                     (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"),
                     (b"\xef\xbb\xbf", "utf-8-sig")):
        if data.startswith(bom):
            try:
                return data.decode(enc)
            except Exception:  # noqa: BLE001
                break
    # Plain ASCII / valid UTF-8 is the common case — try it before the heavier detector.
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    except Exception:  # noqa: BLE001 — detector failure → safe fallback below
        pass
    return data.decode("utf-8", errors="replace")


def _parse_comic(data: bytes, filename: str) -> ParsedMedia:
    ext = ext_of(filename)
    images = _comic_images(data, ext)
    info = _read_comicinfo(data, ext)        # ComicInfo.xml (CBZ/CBR metadata standard), if present
    title = info.get("title") or _stem(filename)
    if not images:
        return ParsedMedia(
            title=title, kind="comic", author=info.get("author"),
            description=info.get("description"), language=info.get("language") or "en",
            chapters=[ParsedChapter(index=1, title=title, body_html="<p>(no pages found)</p>")],
        )

    # Stable per-file key so re-syncing the same archive overwrites in place.
    key = hashlib.sha1(filename.encode("utf-8")).hexdigest()[:16]
    out_dir = comic_dir(key)
    cover: tuple[bytes, str] | None = None
    cover_idx = info.get("cover_index")      # ComicInfo-declared front cover (1-based), if any
    parts: list[str] = []
    for i, (name, blob) in enumerate(images, start=1):
        img_ext = ext_of(name) or ".jpg"
        fname = f"{i:04d}{img_ext}"
        (out_dir / fname).write_bytes(blob)
        # Use the ComicInfo-declared cover when present, else the first page.
        if cover is None and (cover_idx == i or (cover_idx is None and i == 1)):
            cover = (blob, _IMAGE_MIME.get(img_ext, "image/jpeg"))
        parts.append(
            f'<figure class="comic-page"><img src="{comic_url(key, fname)}" '
            f'alt="Page {i}" loading="lazy"/></figure>'
        )
    if cover is None and images:             # declared cover index out of range → fall back to page 1
        first_ext = ext_of(images[0][0]) or ".jpg"
        cover = (images[0][1], _IMAGE_MIME.get(first_ext, "image/jpeg"))

    body = '<div class="comic">' + "\n".join(parts) + "</div>"
    chapter = ParsedChapter(index=1, title=title, body_html=body)
    meta = {"pages": len(images)}
    if info.get("series"):
        meta["series"] = info["series"]
    return ParsedMedia(
        title=title, kind="comic", cover=cover, author=info.get("author"),
        description=info.get("description"), language=info.get("language") or "en",
        chapters=[chapter], meta=meta,
    )


# --------------------------------------------------------------------------- API
def _convert_to_epub_bytes(data: bytes, filename: str) -> tuple[bytes, str]:
    """Write bytes to a temp file, convert to EPUB (mobi/azw3/…), and return the EPUB bytes + name.
    Raises on failure so the caller surfaces a clear import error rather than storing garbage."""
    import os
    import tempfile

    from . import convert
    with tempfile.TemporaryDirectory(prefix="shelf-convert-") as tmp:
        src = os.path.join(tmp, os.path.basename(filename) or "book" + ext_of(filename))
        with open(src, "wb") as fh:
            fh.write(data)
        out = convert.to_epub(src)
        if not out or not os.path.isfile(out):
            raise ValueError(f"could not convert {filename!r} to EPUB")
        with open(out, "rb") as fh:
            epub_bytes = fh.read()
    return epub_bytes, _stem(filename) + ".epub"


def parse_media(data: bytes, filename: str) -> ParsedMedia:
    """Parse any supported media file from its bytes + filename."""
    ext = ext_of(filename)
    # Kindle/other convertible formats → transparently convert to EPUB first, then parse as one, so
    # a locally-uploaded or watched-folder .mobi/.azw3 actually imports (E4).
    if ext not in SUPPORTED_EXTS:
        from . import convert
        if ext in convert.CONVERTIBLE_EXTS and convert.available():
            data, filename = _convert_to_epub_bytes(data, filename)
            ext = ".epub"
    if ext in EPUB_EXTS:
        meta, chapters = chapterize_epub(data)
        cover = None
        try:
            cover = extract_epub_cover(data)
        except Exception:
            cover = None
        # Re-serve inline illustrations from /media so the reader can load them (their
        # EPUB-internal src paths don't resolve over HTTP).
        try:
            _externalize_epub_images(data, filename, chapters)
        except Exception:
            pass
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
    text = _decode_text(data)
    meta, chapters = chapterize_text(text, is_markdown=ext in MD_EXTS)
    title = meta.get("title")
    if title in (None, "Imported document"):
        title = _stem(filename)
    return ParsedMedia(
        title=title, author=meta.get("author"),
        language=meta.get("language") or "en", chapters=chapters, kind="text",
    )
