"""A scanned/image-only PDF renders to a page gallery WITHOUT holding every page in RAM.

Regression for the watcher memory balloon: the old `_pdf_render_pages` accumulated every rendered
PNG in a list (a 600-page scan ≈ 600 MB), which over a folder of thousands of books drove the sync
thread into swap. `_pdf_render_gallery` streams each page to disk and frees the bitmap, so peak heap
is one page regardless of page count.
"""
import io
import shutil

import fitz  # PyMuPDF
import pytest

from app.ingestion import media as M


def _image_only_pdf(pages: int) -> bytes:
    """A multi-page PDF with NO extractable text (a drawn rectangle per page) — trips the scan path."""
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=200, height=300)
        page.draw_rect(fitz.Rect(20, 20, 180, 280), fill=(0.2, 0.4, 0.8))
    data = doc.tobytes()
    doc.close()
    return data


def test_scanned_pdf_becomes_streamed_gallery():
    data = _image_only_pdf(8)
    parsed = M.parse_media(data, "scan-test-fixture.pdf")
    assert parsed.kind == "comic", "image-only multi-page PDF should import as a page gallery"
    assert parsed.meta.get("pages") == 8
    assert parsed.cover is not None and parsed.cover[0], "first page should be the cover"

    # Pages were written to disk (streamed), not kept in the returned object.
    key = M.hashlib.sha1(b"scan-test-fixture.pdf", usedforsecurity=False).hexdigest()[:16]
    out_dir = M.comic_dir(key)
    try:
        pngs = sorted(p.name for p in out_dir.glob("*.png"))
        assert pngs == [f"{i:04d}.png" for i in range(1, 9)], pngs
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_render_streams_pages_to_disk_as_it_goes(monkeypatch):
    """Streaming property: each page is on disk BEFORE the next is rendered (so the doc is never
    held in RAM all at once). Assert page N+1's render only happens after page N's file exists."""
    data = _image_only_pdf(12)
    key = M.hashlib.sha1(b"stream-order-fixture.pdf", usedforsecurity=False).hexdigest()[:16]
    out_dir = M.comic_dir(key)
    real = fitz.Page.get_pixmap
    seen_files_at_render = []

    def _tracking_get_pixmap(self, *a, **k):
        seen_files_at_render.append(len(list(out_dir.glob("*.png"))))
        return real(self, *a, **k)

    monkeypatch.setattr(fitz.Page, "get_pixmap", _tracking_get_pixmap)
    try:
        parsed = M.parse_media(data, "stream-order-fixture.pdf")
        assert parsed.kind == "comic" and parsed.meta["pages"] == 12
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    # Page i is rendered only after i-1 files are already written → strictly incremental, never
    # buffered. (A buffering impl would show 0 files until the very end.)
    assert seen_files_at_render == list(range(12)), seen_files_at_render


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
