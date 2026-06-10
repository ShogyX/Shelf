"""Convert non-importable ebook formats (Kindle mobi / azw3 / …) to EPUB so the importer can ingest
them. Prefers Calibre's ``ebook-convert`` when present (best fidelity); otherwise uses the
pure-Python ``mobi`` library (a kindleunpack port) which unpacks a modern KF8/azw3 straight to an
EPUB, or an older mobi to HTML that we repackage into a minimal EPUB.

The downloader calls :func:`ensure_epub` after a download: a mobi/azw3 file is transparently turned
into an EPUB (which then goes through the same integrity + content-verification gate as any other
download); anything already importable, or unconvertible, is left/returned untouched.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import zipfile

log = logging.getLogger("shelf.convert")

# Kindle / other containers we can turn into EPUB.
CONVERTIBLE_EXTS = {".mobi", ".azw", ".azw3", ".azw4", ".prc", ".kf8", ".kfx"}


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _has_calibre() -> bool:
    return shutil.which("ebook-convert") is not None


def _has_mobi_lib() -> bool:
    try:
        import mobi  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def available() -> bool:
    """Whether ANY converter is usable (so the matcher can decide to accept mobi/azw3 candidates)."""
    return _has_calibre() or _has_mobi_lib()


def can_convert(path: str) -> bool:
    return _ext(path) in CONVERTIBLE_EXTS and available()


def _valid_epub(path: str | None) -> bool:
    return bool(path) and os.path.isfile(path) and zipfile.is_zipfile(path) \
        and zipfile.ZipFile(path).testzip() is None


def _calibre_convert(src: str, dst: str) -> str | None:
    try:
        subprocess.run(["ebook-convert", src, dst], check=True, capture_output=True, timeout=300)
    except Exception as exc:  # noqa: BLE001
        log.info("ebook-convert failed for %s: %s", src, exc)
        return None
    return dst if _valid_epub(dst) else None


def _wrap_html_as_epub(html_path: str, dst: str, title: str) -> str | None:
    """Build a minimal valid EPUB around an extracted HTML body (old-mobi fallback)."""
    try:
        from ebooklib import epub
        with open(html_path, "rb") as fh:
            html = fh.read().decode("utf-8", "replace")
        book = epub.EpubBook()
        book.set_title(title or "Untitled")
        ch = epub.EpubHtml(title=title or "Text", file_name="text.xhtml", content=html)
        book.add_item(ch)
        book.spine = ["nav", ch]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub.write_epub(dst, book)
    except Exception as exc:  # noqa: BLE001
        log.info("html→epub wrap failed: %s", exc)
        return None
    return dst if _valid_epub(dst) else None


def _mobi_convert(src: str, dst: str) -> str | None:
    try:
        import mobi
        tdir, result = mobi.extract(src)
    except Exception as exc:  # noqa: BLE001
        log.info("mobi.extract failed for %s: %s", src, exc)
        return None
    try:
        if result and result.lower().endswith(".epub") and _valid_epub(result):
            shutil.copyfile(result, dst)      # modern KF8/azw3 → already an EPUB
            return dst if _valid_epub(dst) else None
        if result and result.lower().endswith((".html", ".htm", ".xhtml")):
            return _wrap_html_as_epub(result, dst, os.path.splitext(os.path.basename(src))[0])
        return None
    finally:
        shutil.rmtree(tdir, ignore_errors=True)


def to_epub(src: str) -> str | None:
    """Convert a mobi/azw3/… file to a NEW epub next to it; return its path or None. The caller owns
    (imports + cleans) the returned file."""
    if _ext(src) not in CONVERTIBLE_EXTS:
        return None
    dst = src + ".converted.epub"
    out = _calibre_convert(src, dst) if _has_calibre() else None
    if out is None and _has_mobi_lib():
        out = _mobi_convert(src, dst)
    if out is None and os.path.exists(dst):   # a partial/invalid converter output → don't leave it
        try:
            os.remove(dst)
        except OSError:
            pass
    return out


def convert_in_dir(root: str) -> int:
    """Convert every mobi/azw3/… in a folder to a sibling .epub (for the usenet import path, which
    scans a download directory). The originals are left in place. Returns how many were converted."""
    if not available():
        return 0
    n = 0
    for dp, _dirs, files in os.walk(root):
        for f in files:
            if _ext(f) in CONVERTIBLE_EXTS:
                final = os.path.join(dp, os.path.splitext(f)[0] + ".epub")
                if os.path.exists(final):
                    continue
                out = to_epub(os.path.join(dp, f))
                if out:
                    try:
                        shutil.move(out, final)
                        n += 1
                    except OSError:
                        pass
    return n


def ensure_epub(path: str) -> str:
    """If `path` is a convertible Kindle format and a converter is available, return a converted EPUB
    path (the original is left in place for the caller to clean); otherwise return `path` unchanged.
    Conversion failures fall through to the original (which the integrity gate will then reject)."""
    if _ext(path) in CONVERTIBLE_EXTS and available():
        out = to_epub(path)
        if out:
            log.info("converted %s → epub", os.path.basename(path))
            return out
    return path
