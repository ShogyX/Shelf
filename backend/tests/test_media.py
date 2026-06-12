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
    # No ComicInfo.xml → title falls back to the filename stem.
    assert parsed.title == "Cool Comic #1"


def test_decode_text_detects_encoding():
    """13C: non-UTF-8 text is decoded with its detected codec (no mojibake), BOMs stripped."""
    from app.ingestion.media import _decode_text
    assert _decode_text("Héllo wörld".encode("utf-8")) == "Héllo wörld"
    assert _decode_text("Héllo — wörld".encode("cp1252")) == "Héllo — wörld"   # 1252, not utf-8
    assert _decode_text("日本語".encode("utf-16")) == "日本語"                  # BOM stripped
    assert _decode_text("こんにちは".encode("shift_jis")) == "こんにちは"
    assert _decode_text(b"") == ""


def test_parse_comic_uses_comicinfo_metadata():
    """13C: ComicInfo.xml (the CBZ metadata standard) drives title/series/author/language/cover —
    not just the filename stem."""
    buf = io.BytesIO()
    comicinfo = (
        '<?xml version="1.0"?><ComicInfo>'
        "<Series>Berserk</Series><Number>12</Number><Writer>Kentaro Miura</Writer>"
        "<Summary>The Eclipse.</Summary><LanguageISO>ja</LanguageISO>"
        '<Pages><Page Image="1" Type="FrontCover"/></Pages>'
        "</ComicInfo>"
    )
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("000.jpg", b"\xff\xd8\xff\xe0page0")
        zf.writestr("001.jpg", b"\xff\xd8\xff\xe0COVER")   # declared FrontCover (Image=1, 0-based)
        zf.writestr("ComicInfo.xml", comicinfo)
    parsed = parse_media(buf.getvalue(), "whatever-filename.cbz")
    assert parsed.title == "Berserk 12"                  # Series + Number, NOT the filename
    assert parsed.author == "Kentaro Miura" and parsed.language == "ja"
    assert parsed.description == "The Eclipse."
    assert parsed.meta.get("series") == "Berserk"
    assert parsed.cover is not None and b"COVER" in parsed.cover[0]   # the declared front cover


def test_html_book_images_resolved_to_absolute():
    """Illustrated HTML books (e.g. Gutenberg) carry relative <img src> that won't load in
    the reader; splitting with a base_url makes them absolute so images actually show."""
    from app.ingestion.adapters.local_import import _split_html_by_headings
    html = """<html><body>
      <h2>Chapter 1</h2>
      <p>Some text with a figure.</p>
      <img src="images/fig1.jpg" alt="fig"/>
      <p>More text so the chapter has real content and isn't dropped as empty.</p>
    </body></html>"""
    base = "https://www.gutenberg.org/cache/epub/12345/pg12345-images.html"
    chapters = _split_html_by_headings(html, base_url=base)
    assert chapters
    assert "https://www.gutenberg.org/cache/epub/12345/images/fig1.jpg" in chapters[0].body_html
    # Without a base_url the original relative src is preserved (back-compat).
    plain = _split_html_by_headings(html)
    assert 'src="images/fig1.jpg"' in plain[0].body_html
