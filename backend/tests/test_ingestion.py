"""Function tests for the ingestion adapters: verify the pulled content is real,
chapterized correctly, and free of publisher boilerplate.

Covers the bug report: Gutenberg "complete works" returned 2 chapters with no
content because chapters were nested in container <div>s the splitter ignored.
"""
from __future__ import annotations

import io
import zipfile

import pytest
from bs4 import BeautifulSoup

from app.ingestion.adapters import gutenberg as gut
from app.ingestion.adapters.gutenberg import GutenbergAdapter
from app.ingestion.adapters.local_import import (
    _split_html_by_headings,
    chapterize_epub,
    chapterize_text,
)
from app.ingestion.base import ChapterRef


# --------------------------------------------------------------- fake network
class FakeResp:
    def __init__(self, text="", status=200, content=b""):
        self.text = text
        self.status_code = status
        self.content = content or text.encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeFetcher:
    def __init__(self, pages: dict):
        self.pages = pages

    async def get(self, key, url):
        return self.pages.get(url, FakeResp(status=404))

    async def allowed(self, key, url):
        return url in self.pages


@pytest.fixture(autouse=True)
def _clear_caches():
    gut._BOOK_CACHE.clear()
    yield
    gut._BOOK_CACHE.clear()


# ----------------------------------------------------------- the heading split
# A faithfully Gutenberg-shaped HTML edition: license header/footer + chapters
# nested inside <div class="chapter"> wrappers (which the old splitter missed).
GUTENBERG_HTML = """
<html><head><title>The Adventures of Tom Sawyer</title></head><body>
  <section id="pg-header">
    <p>The Project Gutenberg eBook of The Adventures of Tom Sawyer</p>
    <p>This ebook is for the use of anyone anywhere... LICENSE BOILERPLATE.</p>
  </section>
  <h1>The Adventures of Tom Sawyer</h1>
  <div class="chapter">
    <h2>CHAPTER I</h2>
    <p>"TOM!" No answer.</p>
    <p>"TOM!" Still no answer. The old lady pulled her spectacles down.</p>
  </div>
  <div class="chapter">
    <h2>CHAPTER II</h2>
    <p>Saturday morning was come, and all the summer world was bright and fresh.</p>
  </div>
  <div class="chapter">
    <h2>CHAPTER III</h2>
    <p>Tom presented himself before Aunt Polly, who was sitting by an open window.</p>
  </div>
  <section id="pg-footer">
    <p>*** END OF THE PROJECT GUTENBERG EBOOK ... *** more license boilerplate.</p>
  </section>
</body></html>
"""


def test_split_nested_chapters_with_content():
    chapters = _split_html_by_headings(GUTENBERG_HTML, fallback_title="Book 74")
    assert len(chapters) == 3, [c.title for c in chapters]
    assert [c.title for c in chapters] == ["CHAPTER I", "CHAPTER II", "CHAPTER III"]
    # Every chapter has REAL text (the reported bug was empty bodies).
    for c in chapters:
        assert BeautifulSoup(c.body_html, "lxml").get_text(strip=True)
    assert "No answer" in chapters[0].body_html
    assert "Saturday morning" in chapters[1].body_html
    # License boilerplate is gone.
    full = " ".join(c.body_html for c in chapters)
    assert "LICENSE BOILERPLATE" not in full
    assert "Project Gutenberg" not in full


def test_split_flat_chapters_still_work():
    html = (
        "<body><h2>Chapter 1</h2><p>Alpha body text here.</p>"
        "<h2>Chapter 2</h2><p>Beta body text here.</p></body>"
    )
    chapters = _split_html_by_headings(html)
    assert len(chapters) == 2
    assert "Alpha" in chapters[0].body_html and "Beta" in chapters[1].body_html


def test_split_never_emits_empty_chapters():
    # Headings with nothing but whitespace/empty markup between them.
    html = "<body><h2>Empty One</h2>   <h2>Empty Two</h2><p></p></body>"
    chapters = _split_html_by_headings(html)
    assert chapters == [] or all(
        BeautifulSoup(c.body_html, "lxml").get_text(strip=True) for c in chapters
    )


# -------------------------------------------------- gutenberg adapter, end-to-end
TOM_LANDING = """
<html><head>
  <meta property="og:title" content="The Adventures of Tom Sawyer by Mark Twain">
  <meta property="og:image" content="/cache/epub/74/pg74.cover.medium.jpg">
</head><body>
  <h1 itemprop="name">The Adventures of Tom Sawyer by Mark Twain</h1>
  <a itemprop="creator">Mark Twain</a>
  <span itemprop="inLanguage">Language English</span>
</body></html>
"""


@pytest.mark.asyncio
async def test_gutenberg_adapter_pulls_real_chapters():
    book_id = "74"
    pages = {
        f"{gut.GUT}/ebooks/{book_id}": FakeResp(TOM_LANDING),
        f"{gut.GUT}/files/{book_id}/{book_id}-h/{book_id}-h.htm": FakeResp(GUTENBERG_HTML),
    }
    adapter = GutenbergAdapter(FakeFetcher(pages))

    meta = await adapter.discover_work(book_id)
    assert meta.title == "The Adventures of Tom Sawyer"
    assert meta.author == "Mark Twain"
    assert meta.language == "English"

    refs = await adapter.list_chapters(meta)
    assert len(refs) == 3, [r.title for r in refs]

    raw = await adapter.fetch_chapter(ChapterRef(source_chapter_ref="74#1", index=1, title="x"))
    assert "No answer" in raw.body and "Project Gutenberg" not in raw.body


@pytest.mark.asyncio
async def test_gutenberg_text_edition_strips_license_and_splits():
    # Only a plain-text edition is available (HTML candidates 404).
    book_id = "9999"
    text = (
        "The Project Gutenberg eBook of Demo\nLICENSE HEADER LINE\n"
        "*** START OF THE PROJECT GUTENBERG EBOOK DEMO ***\n\n"
        "CHAPTER I\n\nThis is the first chapter body, with plenty of words to read.\n\n"
        "CHAPTER II\n\nThis is the second chapter body, equally full of words.\n\n"
        "*** END OF THE PROJECT GUTENBERG EBOOK DEMO ***\n"
        "LICENSE FOOTER that must not appear.\n"
    )
    txt_url = f"{gut.GUT}/cache/epub/{book_id}/pg{book_id}.txt.utf8"
    adapter = GutenbergAdapter(FakeFetcher({txt_url: FakeResp(text)}))

    refs = await adapter.list_chapters(
        type("M", (), {"source_work_ref": book_id})()
    )
    assert len(refs) == 2
    raw = await adapter.fetch_chapter(
        ChapterRef(source_chapter_ref=f"{book_id}#1", index=1, title="x")
    )
    assert "first chapter body" in raw.body
    assert "LICENSE" not in raw.body and "Project Gutenberg" not in raw.body


# ------------------------------------------------------------ epub (SE + local)
def test_chapterize_text_splits_on_chapter_lines():
    text = (
        "Prologue\n\nIn the beginning there were words and they were good.\n\n"
        "Chapter 1\n\nThe story truly begins on a cold morning.\n\n"
        "Chapter 2\n\nAnd it continues into the afternoon light.\n"
    )
    _meta, chapters = chapterize_text(text)
    assert len(chapters) == 3
    assert "beginning" in chapters[0].body_html
    assert "cold morning" in chapters[1].body_html


def test_chapterize_epub_roundtrip_has_real_content():
    from app.epub_export import EpubChapter, build_epub

    data = build_epub(
        title="Demo Book", author="A. Writer", language="en", cover_url=None,
        chapters=[
            EpubChapter(index=1, title="Chapter One",
                        body_html="<p>" + "First chapter sentence. " * 8 + "</p>"),
            EpubChapter(index=2, title="Chapter Two",
                        body_html="<p>" + "Second chapter sentence. " * 8 + "</p>"),
        ],
        identifier="urn:test:demo",
    )
    # Sanity: it's a real EPUB zip.
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert any(n.endswith(".xhtml") for n in zf.namelist())

    meta, chapters = chapterize_epub(data)
    assert meta["title"] == "Demo Book" and meta["author"] == "A. Writer"
    assert len(chapters) == 2
    assert "First chapter sentence" in chapters[0].body_html
    assert "Second chapter sentence" in chapters[1].body_html
