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


def test_comic_page_order_preserves_stored_order_for_non_numeric_names():
    """CC5: archives whose page names aren't reliably numeric (no zero-padding / arbitrary stems)
    must keep the archive's STORED order — a name sort mis-sequences them. Numeric names still
    natural-sort (1 before 10)."""
    from app.ingestion.media import _comic_page_order
    # <80% numeric → preserve insertion/stored order verbatim, do NOT alpha-sort.
    stored = ["intro.jpg", "aaa.jpg", "middle.jpg", "zzz.jpg", "end.jpg"]
    assert _comic_page_order(stored) == stored
    # Reliably numeric → natural sort (page2 before page10).
    assert _comic_page_order(["page10.jpg", "page2.jpg", "page1.jpg"]) == \
        ["page1.jpg", "page2.jpg", "page10.jpg"]


def test_pdf_outline_ranges_dedup_shared_start_page():
    """CC5: two bookmarks pointing at the same start page (a chapter and its first sub-section)
    must collapse to one range — otherwise the shared page is duplicated and an end<=start range
    is force-widened, repeating a page."""
    from app.ingestion.media import _pdf_outline_ranges

    class _Node:
        def __init__(self, title):
            self.title = title

    class _Reader:
        """Minimal pypdf-shaped reader: an outline of bookmark nodes + a page→number map."""
        pages = list(range(10))

        def __init__(self):
            self._nodes = [_Node("Ch1"), _Node("Ch1.1"), _Node("Ch2"), _Node("Ch3")]
            self._pages = {id(self._nodes[0]): 0, id(self._nodes[1]): 0,   # share start page 0
                           id(self._nodes[2]): 4, id(self._nodes[3]): 7}

        @property
        def outline(self):
            return self._nodes

        def get_destination_page_number(self, node):
            return self._pages[id(node)]

    ranges = _pdf_outline_ranges(_Reader())
    # The duplicate start (Ch1 / Ch1.1 both at page 0) collapses to ONE range; first title kept.
    assert [(t, s) for t, s, _ in ranges] == [("Ch1", 0), ("Ch2", 4), ("Ch3", 7)]
    # No zero/negative-width range, and starts are strictly ascending (no page repeated).
    assert all(end > start for _, start, end in ranges)
    assert [s for _, s, _ in ranges] == sorted({s for _, s, _ in ranges})


def test_norm_lang_keeps_whole_subtags():
    """CC5: language normalization keeps whole BCP-47 subtags (the old [:8] truncation cut
    'zh-Hant-HK' mid-subtag → invalid xml:lang). Falls back to a clean primary subtag."""
    from app.epub_export import _norm_lang
    assert _norm_lang("zh-Hant-HK") == "zh-Hant-HK"
    assert _norm_lang("en_US") == "en-US"            # underscore → hyphen
    assert _norm_lang("en-US-x-private-extra") == "en-US-x"   # capped at 3 subtags
    assert _norm_lang("") == "en"
    assert _norm_lang(None) == "en"
    assert _norm_lang("123") == "en"                 # non-alpha primary → fallback


def test_xhtml_body_escapes_unparseable_fallback():
    """CC5: when a chapter body can't be parsed into well-formed XHTML, the fallback ESCAPES it
    (renders as readable text) instead of embedding raw markup that corrupts the EPUB document."""
    from app.epub_export import _xhtml_body
    # A control character that lxml's fragment parser rejects forces the fallback branch.
    out = _xhtml_body("a < b & c > d")
    assert "&lt;" in out and "&amp;" in out and "&gt;" in out


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
