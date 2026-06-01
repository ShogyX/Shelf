"""Tests for unified media parsing (TXT / comic CBZ)."""
from __future__ import annotations

import io
import zipfile

from app.ingestion.media import is_supported, parse_media


def test_is_supported():
    assert is_supported("a.epub") and is_supported("b.PDF") and is_supported("c.cbz")
    assert is_supported("d.txt") and is_supported("e.md")
    assert not is_supported("f.docx") and not is_supported("g")


def test_parse_text_chapterizes():
    text = "# Chapter One\nHello world.\n\n# Chapter Two\nMore text here."
    parsed = parse_media(text.encode(), "story.md")
    assert parsed.kind == "text"
    assert len(parsed.chapters) == 2
    assert "Hello world" in parsed.chapters[0].body_html


def test_parse_comic_cbz_builds_image_gallery():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # Two tiny fake images; natural sort must keep 1 before 10.
        zf.writestr("page1.jpg", b"\xff\xd8\xff\xe0fake-jpeg-1")
        zf.writestr("page10.jpg", b"\xff\xd8\xff\xe0fake-jpeg-10")
        zf.writestr("page2.jpg", b"\xff\xd8\xff\xe0fake-jpeg-2")
        zf.writestr("notes.txt", b"ignore me")
    parsed = parse_media(buf.getvalue(), "Cool Comic #1.cbz")
    assert parsed.kind == "comic"
    assert parsed.cover is not None
    body = parsed.chapters[0].body_html
    assert body.count("<img") == 3
    # Natural ordering: 0001 (page1) then 0002 (page2) then 0003 (page10).
    assert body.index("0001") < body.index("0002") < body.index("0003")
