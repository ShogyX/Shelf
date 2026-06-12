"""Build an EPUB from a work's stored (sanitized) chapters — for download or
Send-to-Kindle. Reuses ebooklib (already a dependency)."""
from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass

from lxml import etree
from lxml import html as lxml_html

from .config import get_settings
from .covers import covers_dir
from .ingestion.netguard import safe_get
from .media import media_dir

settings = get_settings()

_IMG_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp", "svg": "image/svg+xml",
}


def _local_image_bytes(src: str) -> tuple[bytes, str] | None:
    """Resolve a local /media/* or /covers/* image URL to (bytes, mime), if it exists."""
    try:
        if src.startswith("/media/"):
            path = media_dir() / src[len("/media/"):]
        elif src.startswith("/covers/"):
            path = covers_dir() / os.path.basename(src)
        else:
            return None
        if not path.is_file():
            return None
        ext = path.suffix.lstrip(".").lower()
        return path.read_bytes(), _IMG_MIME.get(ext, "image/jpeg")
    except Exception:
        return None


@dataclass
class EpubChapter:
    index: int
    title: str
    body_html: str


def _ext_for_mime(mime: str) -> str:
    m = (mime or "").lower()
    for key in ("png", "webp", "gif", "bmp"):
        if key in m:
            return key
    if "svg" in m:
        return "svg"
    return "jpg"


def extract_image_srcs(body_html: str) -> list[str]:
    """Image URLs referenced by a chapter body, in document order (comic/manga pages)."""
    if not body_html or "<img" not in body_html:
        return []
    try:
        frag = lxml_html.fragment_fromstring(body_html, create_parent="div")
    except Exception:
        return []
    out: list[str] = []
    for img in frag.iter("img"):
        src = (img.get("src") or "").strip()
        if src:
            out.append(src)
    return out


def resolve_image_bytes(
    src: str, cache: dict[str, tuple[bytes, str, str] | None]
) -> tuple[bytes, str, str] | None:
    """Resolve an image URL to (bytes, ext, mime). Local /media|/covers are read from disk;
    remote http(s) are fetched once and cached. Returns None if unavailable."""
    if src in cache:
        return cache[src]
    res: tuple[bytes, str, str] | None = None
    local = _local_image_bytes(src)
    if local:
        data, mime = local
        res = (data, _ext_for_mime(mime), mime)
    elif src.startswith("http"):
        try:
            # SSRF-guarded: chapter bodies can reference arbitrary remote URLs; safe_get blocks
            # internal targets and re-validates every redirect hop.
            r = safe_get(src, timeout=20, headers={"User-Agent": settings.user_agent})
            if r.status_code == 200 and r.content:
                mime = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                res = (r.content, _ext_for_mime(mime), mime)
        except Exception:
            res = None
    cache[src] = res
    return res


def _xhtml_body(body_html: str) -> str:
    """Re-serialize possibly-loose HTML into well-formed XHTML for EPUB readers."""
    try:
        frag = lxml_html.fragment_fromstring(body_html or "", create_parent="div")
        return etree.tostring(frag, method="xml", encoding="unicode")
    except Exception:
        return f"<div>{body_html or ''}</div>"


def _load_cover(cover_url: str | None) -> tuple[bytes, str] | None:
    if not cover_url:
        return None
    try:
        if cover_url.startswith("/covers/"):
            path = covers_dir() / os.path.basename(cover_url)
            if path.is_file():
                return path.read_bytes(), "image/jpeg"
            return None
        if cover_url.startswith("http"):
            # SSRF-guarded (cover_url can be user/source-supplied); redirects re-validated per hop.
            r = safe_get(cover_url, timeout=20, headers={"User-Agent": settings.user_agent})
            if r.status_code == 200 and r.content:
                ct = r.headers.get("content-type", "image/jpeg").split(";")[0]
                return r.content, ct
    except Exception:
        return None
    return None


def _embed_images(body_html: str, book, cache: dict[str, str]) -> str:
    """Rewrite local <img src=/media/..|/covers/..> to embedded EPUB images.

    Comics (CBZ/CBR) store their pages under /media and reference them from chapter
    HTML; without embedding, those pages would be missing in the exported EPUB.
    `cache` maps an original src -> the in-EPUB path (deduped across chapters)."""
    from ebooklib import epub

    if not body_html or "<img" not in body_html:
        return body_html
    try:
        frag = lxml_html.fragment_fromstring(body_html, create_parent="div")
    except Exception:
        return body_html
    changed = False
    for img in frag.iter("img"):
        src = (img.get("src") or "").strip()
        if not src or not src.startswith(("/media/", "/covers/")):
            continue
        internal = cache.get(src)
        if internal is None:
            loaded = _local_image_bytes(src)
            if loaded is None:
                continue
            data, mime = loaded
            ext = mime.split("/")[-1].replace("jpeg", "jpg").replace("svg+xml", "svg")
            internal = f"images/img_{len(cache) + 1:05d}.{ext}"
            book.add_item(
                epub.EpubImage(uid=f"img{len(cache) + 1}", file_name=internal,
                               media_type=mime, content=data)
            )
            cache[src] = internal
        img.set("src", internal)
        changed = True
    if not changed:
        return body_html
    return "".join(
        etree.tostring(c, method="xml", encoding="unicode") for c in frag.iterchildren()
    ) or body_html


def build_epub(
    *,
    title: str,
    author: str | None,
    language: str,
    cover_url: str | None,
    chapters: list[EpubChapter],
    identifier: str,
) -> bytes:
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier(identifier)
    book.set_title(title)
    book.set_language((language or "en")[:8])
    if author:
        book.add_author(author)

    cover = _load_cover(cover_url)
    if cover:
        ext = "jpg" if "png" not in cover[1] else "png"
        try:
            book.set_cover(f"cover.{ext}", cover[0])
        except Exception:
            pass

    items = []
    image_cache: dict[str, str] = {}
    for ch in chapters:
        item = epub.EpubHtml(
            title=ch.title or f"Chapter {ch.index}",
            file_name=f"chap_{ch.index:05d}.xhtml",
            lang=(language or "en")[:8],
        )
        heading = f"<h1>{_escape(ch.title)}</h1>" if ch.title else ""
        body_html = _embed_images(ch.body_html, book, image_cache)
        # NB: do NOT prepend an <?xml?> prolog — ebooklib adds its own declaration and
        # silently writes an empty document if the content already starts with one.
        item.content = (
            f'<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            f"<title>{_escape(ch.title or 'Chapter')}</title></head>"
            f"<body>{heading}{_xhtml_body(body_html)}</body></html>"
        )
        book.add_item(item)
        items.append(item)

    book.toc = items
    book.add_item(epub.EpubNcx())  # NCX TOC (well supported, incl. Kindle)
    book.spine = list(items)

    # ebooklib writes to a path; round-trip through a temp file.
    fd, path = tempfile.mkstemp(suffix=".epub")
    os.close(fd)
    try:
        epub.write_epub(path, book)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _escape(s: str | None) -> str:
    s = s or ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- Kindle comics
# Send-to-Kindle accepts EPUB but not CBZ/WebP, so comics go as a *fixed-layout* EPUB with one
# JPEG image per page (the format Kindle Comic Converter produces). Pages are grayscaled +
# downscaled to a Kindle-ish resolution to stay under the ~50MB email cap, and tall webtoon
# strips are sliced into screen-height pages so they don't render as one unreadable sliver.
_K_PAGE_W = 1264      # target width for sliced webtoon pages
_K_PAGE_H = 1680      # slice height ≈ a Kindle screen
_K_MAX_EDGE = 1680    # cap the long edge of normal pages
_K_WEBTOON_RATIO = 2.5  # height/width above this ⇒ treat as a vertical strip and slice


def _jpeg(im) -> tuple[bytes, int, int]:
    b = io.BytesIO()
    im.save(b, format="JPEG", quality=85, optimize=True)
    return b.getvalue(), im.width, im.height


def kindle_pages_from_image(img_bytes: bytes) -> list[tuple[bytes, int, int]]:
    """Turn one source page image into one or more Kindle-ready JPEG pages
    ``(jpeg_bytes, w, h)``. Webtoon strips are sliced; normal pages are downscaled. Returns
    ``[]`` if the bytes can't be decoded."""
    from PIL import Image, ImageOps

    try:
        im = Image.open(io.BytesIO(img_bytes))
        im = ImageOps.exif_transpose(im)
        im = im.convert("L")  # grayscale — Kindle is e-ink; halves the file size
    except Exception:
        return []
    w, h = im.size
    if w == 0 or h == 0:
        return []
    if h / w >= _K_WEBTOON_RATIO:
        # Vertical strip (webtoon/manhwa): normalize width, slice top→bottom into pages.
        if w != _K_PAGE_W:
            im = im.resize((_K_PAGE_W, max(1, round(h * _K_PAGE_W / w))))
            w, h = im.size
        out: list[tuple[bytes, int, int]] = []
        y = 0
        while y < h:
            out.append(_jpeg(im.crop((0, y, w, min(y + _K_PAGE_H, h)))))
            y += _K_PAGE_H
        return out
    # Normal page: downscale the long edge, keep aspect, one page.
    scale = min(1.0, _K_MAX_EDGE / max(w, h))
    if scale < 1.0:
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))))
    return [_jpeg(im)]


def build_kindle_comic_epub(
    *,
    title: str,
    author: str | None,
    language: str,
    identifier: str,
    images: list[bytes],
    reading_direction: str | None = None,
) -> tuple[bytes, int] | None:
    """Build a fixed-layout EPUB of comic pages for Send-to-Kindle. ``images`` is the ordered
    list of raw page-image bytes (any format Pillow reads, incl. WebP). Returns
    ``(epub_bytes, page_count)`` or ``None`` if no page decoded."""
    from ebooklib import epub

    pages: list[tuple[bytes, int, int]] = []
    sliced = False
    for raw in images:
        produced = kindle_pages_from_image(raw)
        if len(produced) > 1:
            sliced = True
        pages.extend(produced)
    if not pages:
        return None

    book = epub.EpubBook()
    book.set_identifier(identifier)
    book.set_title(title)
    book.set_language((language or "en")[:8])
    if author:
        book.add_author(author)
    # Fixed-layout (pre-paginated) so Kindle shows one full image per page with real page turns.
    opf = "http://www.idpf.org/2007/opf"
    book.add_metadata(opf, "meta", "pre-paginated", {"property": "rendition:layout"})
    book.add_metadata(opf, "meta", "portrait", {"property": "rendition:orientation"})
    book.add_metadata(opf, "meta", "none", {"property": "rendition:spread"})
    book.add_metadata(None, "meta", "true", {"name": "fixed-layout", "content": "true"})
    book.add_metadata(None, "meta", "false", {"name": "original-resolution", "content": "false"})
    # Manga reads right-to-left; sliced webtoons read left-to-right (vertical order is preserved
    # by page order either way — this only sets the horizontal page-turn direction).
    book.direction = reading_direction or ("ltr" if sliced else "rtl")

    css = epub.EpubItem(
        uid="style", file_name="style.css", media_type="text/css",
        content=("html,body{margin:0;padding:0;background:#000;}"
                 "img{display:block;width:100%;height:100%;object-fit:contain;}"),
    )
    book.add_item(css)

    spine: list = []
    for i, (jpg, w, h) in enumerate(pages, 1):
        img_name = f"images/p{i:05d}.jpg"
        book.add_item(epub.EpubImage(uid=f"img{i}", file_name=img_name,
                                     media_type="image/jpeg", content=jpg))
        page = epub.EpubHtml(uid=f"page{i}", file_name=f"p{i:05d}.xhtml",
                             lang=(language or "en")[:8])
        page.content = (
            f'<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            f'<meta name="viewport" content="width={w}, height={h}"/>'
            f'<link rel="stylesheet" href="style.css" type="text/css"/>'
            f"<title>{i}</title></head>"
            f'<body><img src="{img_name}" alt=""/></body></html>'
        )
        page.add_item(css)
        book.add_item(page)
        spine.append(page)

    book.set_cover("cover.jpg", pages[0][0])
    book.toc = tuple(spine)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine

    fd, path = tempfile.mkstemp(suffix=".epub")
    os.close(fd)
    try:
        epub.write_epub(path, book)
        with open(path, "rb") as f:
            return f.read(), len(pages)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
