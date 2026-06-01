"""Build an EPUB from a work's stored (sanitized) chapters — for download or
Send-to-Kindle. Reuses ebooklib (already a dependency)."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

import httpx
from lxml import etree
from lxml import html as lxml_html

from .config import get_settings
from .covers import covers_dir
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
            r = httpx.get(cover_url, timeout=20, follow_redirects=True,
                          headers={"User-Agent": settings.user_agent})
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
