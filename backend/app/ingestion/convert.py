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
# Any ebook source Calibre can render into EPUB — used ON DEMAND for Storyteller (which is EPUB-only)
# when a stocked ebook isn't already EPUB. Broader than CONVERTIBLE_EXTS (which is just the formats
# the download-import path auto-converts). `.epub` is excluded: an EPUB needs no conversion.
EPUB_SOURCE_EXTS = CONVERTIBLE_EXTS | {
    ".pdf", ".txt", ".text", ".rtf", ".doc", ".docx", ".odt", ".html", ".htm",
    ".fb2", ".lit", ".pdb", ".cbz", ".cbr", ".djvu",
}
# Audio containers ffmpeg can fold into a single M4B (on demand; both targets accept mp3, so rare).
_AUDIO_SOURCE_EXTS = {".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma"}


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _rm(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _has_calibre() -> bool:
    return shutil.which("ebook-convert") is not None


def _has_mobi_lib() -> bool:
    try:
        import mobi  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


from functools import lru_cache


@lru_cache(maxsize=1)
def available() -> bool:
    """Whether ANY converter is usable (so the matcher can decide to accept mobi/azw3 candidates).
    Cached: is_supported() calls this per scanned file, and shutil.which / import are not free."""
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


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def to_epub_from(src: str, dst: str) -> str | None:
    """Convert ANY Calibre-supported ebook ``src`` (pdf/txt/mobi/azw3/…) to EPUB at ``dst`` — used
    on demand for Storyteller (EPUB-only). Returns ``dst`` or None. An ``.epub`` source returns None
    (the caller should use it directly, not re-convert). Prefers Calibre; falls back to the mobi lib
    only for Kindle formats."""
    if _ext(src) == ".epub":
        return None
    if _ext(src) not in EPUB_SOURCE_EXTS:
        return None
    out = _calibre_convert(src, dst) if _has_calibre() else None
    if out is None and _ext(src) in CONVERTIBLE_EXTS and _has_mobi_lib():
        out = _mobi_convert(src, dst)
    if out is None and os.path.exists(dst):
        try:
            os.remove(dst)
        except OSError:
            pass
    return out


def to_m4b(sources: list[str], dst: str) -> str | None:
    """Fold one or more audio files (in order) into a single M4B at ``dst`` via ffmpeg — on demand
    only (both targets accept mp3, so this is rarely needed). Returns ``dst`` or None."""
    audio = [s for s in (sources or []) if _ext(s) in _AUDIO_SOURCE_EXTS and os.path.isfile(s)]
    if not audio or not has_ffmpeg():
        return None
    try:
        if len(audio) == 1:
            cmd = ["ffmpeg", "-y", "-i", audio[0], "-c:a", "aac", "-b:a", "64k", dst]
            subprocess.run(cmd, check=True, capture_output=True, timeout=3600)
        else:
            import tempfile
            # concat demuxer list file; escape single quotes per ffmpeg's concat syntax.
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as lf:
                for s in audio:
                    esc = os.path.abspath(s).replace("'", "'\\''")
                    lf.write(f"file '{esc}'\n")
                listfile = lf.name
            try:
                cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                       "-c:a", "aac", "-b:a", "64k", dst]
                subprocess.run(cmd, check=True, capture_output=True, timeout=7200)
            finally:
                try:
                    os.remove(listfile)
                except OSError:
                    pass
    except Exception as exc:  # noqa: BLE001
        log.info("ffmpeg m4b convert failed: %s", exc)
        _rm(dst)
        return None
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return dst
    _rm(dst)  # ffmpeg exited 0 but produced nothing usable → don't leave an empty file
    return None


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
