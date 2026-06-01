"""Tests for adaptive web-extraction helpers (sequential crawling)."""
from __future__ import annotations

from app.ingestion.extract import (
    chapter_base,
    chapter_number,
    chapter_title_from,
    extract_main_content,
    find_chapter_links,
    find_next_targets,
    looks_paginated_toc,
    synthesize_next_chapter_url,
    work_title_from,
)


def test_og_image():
    from app.ingestion.extract import og_image

    html = '<html><head><meta property="og:image" content="/img/c.webp"></head></html>'
    assert og_image(html, "https://s.com/x") == "https://s.com/img/c.webp"
    assert og_image("<html><head></head></html>") is None


def test_page_metadata_gathers_preview():
    from app.ingestion.extract import page_metadata

    html = (
        '<html lang="en"><head>'
        '<meta property="og:title" content="Library of Heaven\'s Path">'
        '<meta property="og:description" content="A teacher enters a cultivation world.">'
        '<meta name="author" content="Heng Sao Tian Ya">'
        '<meta property="og:image" content="/cover.webp">'
        '<meta property="og:site_name" content="Novellunar">'
        '<meta property="og:type" content="book">'
        "</head><body><p>chapter</p></body></html>"
    )
    m = page_metadata(html, "https://novellunar.com/novel/x")
    assert m["description"].startswith("A teacher enters")
    assert m["author"] == "Heng Sao Tian Ya"
    assert m["cover_url"] == "https://novellunar.com/cover.webp"
    assert m["site_name"] == "Novellunar"
    assert m["type"] == "book"
    assert m["language"] == "en"


def test_page_metadata_falls_back_to_first_paragraph():
    from app.ingestion.extract import page_metadata

    html = "<html><body><p>hi</p><p>" + "x " * 40 + "</p></body></html>"
    m = page_metadata(html)
    assert m["description"] and len(m["description"]) >= 60


def test_reconstruct_paragraphs_from_spans():
    from app.ingestion.extract import extract_main_content

    # span-blob with newline-only separators (no <p>) -> reconstructed paragraphs
    html = (
        "<article><span>First paragraph here.</span><span>\n</span>"
        "<span>Second paragraph follows.</span><span>\n</span>"
        "<span>Third one too, with enough text to matter.</span></article>"
    )
    _t, body = extract_main_content(html, "https://s/x/chapter/1")
    assert body.count("<p>") == 3
    assert "First paragraph here." in body


def test_chapter_title_from():
    og = "Library of Heaven's Path Chapter 1: Swindler: Chapter 1 - Read Free | Novellunar"
    assert chapter_title_from(og) == "Chapter 1: Swindler"
    assert chapter_title_from("Some Novel Chapter 42 - site") == "Chapter 42"
    assert chapter_title_from("no chapter here") == ""


def test_work_title_from():
    og = "Library of Heaven's Path Chapter 1: Swindler | Site"
    assert work_title_from(og) == "Library of Heaven's Path"
    assert work_title_from("Eighteen's Bed - Site") == "Eighteen's Bed"


def test_chapter_number():
    assert chapter_number("/book/x/chapter-41-the-title") == 41
    assert chapter_number("/novel/y/chapter/7") == 7
    assert chapter_number("Chapter 12: Dawn") == 12
    assert chapter_number("no numbers here") is None


def test_synthesize_next_chapter_url():
    assert synthesize_next_chapter_url("https://s/novel/x/chapter/5") == "https://s/novel/x/chapter/6"
    assert synthesize_next_chapter_url("https://s/x/chapter-9") == "https://s/x/chapter-10"
    assert synthesize_next_chapter_url("https://s/x/chapter-9/") == "https://s/x/chapter-10/"
    # Non-numeric chapter slug → cannot safely synthesize.
    assert synthesize_next_chapter_url("https://s/x/chapter-9-some-title") is None


def test_find_next_targets_classifies_by_number():
    html = """
      <a href="/book/x/chapter-2" class="next">Next Chapter</a>
      <a href="/book/x/chapter-1?page=2">Next Page</a>
    """
    nc, _t, npage = find_next_targets(html, "https://s/book/x/chapter-1")
    assert nc and nc.endswith("/book/x/chapter-2")
    assert npage and "page=2" in npage


def test_extract_main_content_picks_densest_block():
    html = """
      <html><body>
        <nav>menu links here</nav>
        <div class="reading-content"><p>This is the real chapter body, long enough to win.</p>
        <p>Another paragraph of actual story content for density.</p></div>
        <footer>copyright</footer>
      </body></html>
    """
    title, body = extract_main_content(html, "https://s/x/chapter-1")
    assert "real chapter body" in body
    assert "menu links" not in body
    assert "copyright" not in body


def test_looks_paginated_toc_detects_range_select():
    html = """
      <select id="indexselect">
        <option value="1">C.1 - C.40</option>
        <option value="2">C.41 - C.80</option>
        <option value="3">C.81 - C.120</option>
      </select>
      <a href="/book/x/chapter-1">Ch 1</a>
    """
    assert looks_paginated_toc(html, 1) is True
    assert looks_paginated_toc("<a href='/x/chapter-1'>1</a>", 1) is False


def test_chapter_base_keeps_numeric_chapter_id():
    # A bare /N chapter id must NOT be stripped (it's the chapter, not a page).
    assert chapter_base("https://s/x/chapter/5") == "https://s/x/chapter/5"
    # Explicit page markers are stripped.
    assert chapter_base("https://s/x/chapter-5?page=2") == "https://s/x/chapter-5"


def test_find_chapter_links_filters_to_chapterish():
    html = """
      <ul>
        <li><a href="/book/x/chapter-1">Chapter 1</a></li>
        <li><a href="/book/x/chapter-2">Chapter 2</a></li>
        <li><a href="/about">About us</a></li>
      </ul>
    """
    links = find_chapter_links(html, "https://s/book/x")
    hrefs = [u for u, _ in links]
    assert any("chapter-1" in h for h in hrefs)
    assert not any("/about" in h for h in hrefs)
