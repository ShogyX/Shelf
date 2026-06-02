"""Classification + media-kind + comic-image extraction for real site structures.

Locks in the crawler's handling of the structural variants exercised by webtoons
(query-string works/episodes + lazy-loaded comic image strips), MangaDex (id-segment
work URLs), and the comic vs. text media-kind decision.
"""
from __future__ import annotations

from app.ingestion.extract import (
    classify_page,
    detect_media_kind,
    extract_main_content,
    is_chapter_url,
    is_work_url,
    work_url_for,
)
from app.sanitize import sanitize_html


# --------------------------------------------------------------- URL structure
def test_mangadex_title_url_is_a_work():
    u = "https://mangadex.org/title/a1c7c817-4e59-43b7-9365-09675a149a6f/one-piece"
    assert is_work_url(u) is True          # id segment before the slug must not break it
    assert is_chapter_url(u) is False


def test_webtoons_list_is_work_viewer_is_chapter():
    series = "https://www.webtoons.com/en/action/omniscient-reader/list?title_no=2154"
    episode = "https://www.webtoons.com/en/action/omniscient-reader/ep-1/viewer?title_no=2154&episode_no=1"
    # The series page has 'list' in its path but is a WORK, not a browse listing.
    assert is_work_url(series) is True
    assert is_chapter_url(series) is False
    assert is_chapter_url(episode) is True
    # Both collapse to the canonical series URL (title_no preserved, episode_no dropped).
    assert work_url_for(series) == series
    assert work_url_for(episode) == series


# ------------------------------------------------------------------ media kind
def test_work_url_precision_no_false_positives():
    # Listing slices under a plural category are browse pages, not works.
    for u in ("https://x.org/titles/latest", "https://x.org/novels/popular",
              "https://x.org/manga/all", "https://x.org/comics/updated"):
        assert is_work_url(u) is False, u
    # A work's sub-pages (reviews/comments/…) are not the work itself.
    for u in ("https://ranobedb.org/book/23035/reviews",
              "https://x.org/novel/foo/comments", "https://x.org/title/abcdef-1234/foo/stats"):
        assert is_work_url(u) is False, u
    # Real single-work URLs still pass.
    assert is_work_url("https://x.org/novel/library-of-heavens-path") is True
    assert is_work_url(
        "https://mangadex.org/title/a1c7c817-4e59-43b7-9365-09675a149a6f/one-piece"
    ) is True


def test_royalroad_and_gutenberg_work_urls():
    # RoyalRoad: /fiction/<id>/<slug> is a work; /fictions/<filter> is a browse listing.
    assert is_work_url("https://www.royalroad.com/fiction/21220/mother-of-learning") is True
    assert is_work_url("https://www.royalroad.com/fictions/best-rated") is False
    assert is_work_url("https://www.royalroad.com/fictions/trending") is False
    # A chapter page is not itself a work, and collapses to the fiction landing URL.
    assert is_work_url("https://www.royalroad.com/fiction/21220/chapter/301778/1-x") is False
    assert work_url_for("https://www.royalroad.com/fiction/21220/chapter/301778/1-x") == \
        "https://www.royalroad.com/fiction/21220"
    # Gutenberg: /ebooks/<id> is a book; /ebooks/categories etc. are listings.
    assert is_work_url("https://www.gutenberg.org/ebooks/1342") is True
    assert is_work_url("https://www.gutenberg.org/ebooks/categories") is False

    # A URL merely *containing* the gutenberg literal must NOT spoof a work classification.
    assert is_work_url("https://evil.com/?x=gutenberg.org/ebooks/1") is False
    assert is_work_url("https://evil.com/gutenberg.org/ebooks/1") is False

    from app.ingestion.adapters.gutenberg import GutenbergAdapter
    assert GutenbergAdapter._book_id("https://www.gutenberg.org/ebooks/1342") == "1342"
    assert GutenbergAdapter._book_id("1342") == "1342"


def test_listing_page_with_cover_not_a_work():
    # A work-category URL with only og:image (no synopsis/og:type/chapters) must NOT be
    # cataloged as a work — covers alone don't make a listing page a work.
    html = ('<html><head><meta property="og:title" content="Browse"/>'
            '<meta property="og:image" content="https://x/cover.jpg"/></head><body></body></html>')
    pc = classify_page(html, "https://site.test/novels/by-genre")
    assert pc.kind != "work"


def test_work_subpage_not_classified_as_work():
    html = ('<html><head><meta property="og:title" content="Reviews"/>'
            '<meta property="og:type" content="book"/>'
            '<meta property="og:description" content="' + "x" * 60 +
            '"/></head><body></body></html>')
    pc = classify_page(html, "https://ranobedb.org/book/23035/reviews")
    assert pc.kind == "other"  # must not spawn a duplicate catalog row for the work


def test_detect_media_kind():
    assert detect_media_kind("https://x/a", og_type="com-linewebtoon:webtoon") == "comic"
    assert detect_media_kind(
        "https://mangadex.org/title/x/one-piece", site_name="MangaDex"
    ) == "comic"
    assert detect_media_kind("https://site/manga/foo") == "comic"
    assert detect_media_kind("https://novellunar.com/novel/foo", og_type="book") == "text"


# ----------------------------------------------------------- comic extraction
WEBTOON_EPISODE = """
<html><head>
  <meta property="og:title" content="Omniscient Reader"/>
  <meta property="og:type" content="com-linewebtoon:episode"/>
</head><body>
  <div class="viewer_img _img_viewer_area" id="_imageList">
    <img src="https://webtoons-static.pstatic.net/image/bg_transparency.png"
         data-url="https://webtoon-phinf.pstatic.net/p1.jpg?type=q90" alt="image" class="_images"/>
    <img src="https://webtoons-static.pstatic.net/image/bg_transparency.png"
         data-url="https://webtoon-phinf.pstatic.net/p2.jpg?type=q90" alt="image" class="_images"/>
    <img src="https://webtoons-static.pstatic.net/image/bg_transparency.png"
         data-url="https://webtoon-phinf.pstatic.net/p3.jpg?type=q90" alt="image" class="_images"/>
  </div>
  <div class="related"><img src="https://webtoons-static.pstatic.net/image/bg_transparency.png"
       data-url="https://x/thumb.jpg"/></div>
</body></html>
"""


def test_comic_strip_extraction_promotes_lazy_images():
    _title, body = extract_main_content(WEBTOON_EPISODE, "https://www.webtoons.com/en/x/ep-1/viewer")
    clean = sanitize_html(body)
    # Real (data-url) panels are promoted; transparent placeholders are gone.
    assert clean.count("webtoon-phinf.pstatic.net") == 3
    assert "bg_transparency" not in clean
    # Wrapped in the reader's comic markup, with classes surviving sanitization.
    assert 'class="comic"' in clean
    assert "comic-page" in clean


def test_sanitize_keeps_only_layout_classes():
    out = sanitize_html('<div class="comic evil"><figure class="comic-page hack"><img src="https://x/p.jpg"/></figure></div>')
    assert 'class="comic"' in out and "comic-page" in out
    assert "evil" not in out and "hack" not in out


def test_hotlink_image_proxy_rewrite():
    from app.routers.imgproxy import referer_for, rewrite_hotlinked

    # Webtoon CDN needs a Referer → routed through the proxy; MangaDex doesn't → untouched.
    assert referer_for("https://webtoon-phinf.pstatic.net/x.jpg") == "https://www.webtoons.com/"
    assert referer_for("https://cmdxd98sb0x3yprd.mangadex.network/x.png") is None
    html = ('<div class="comic"><figure class="comic-page">'
            '<img src="https://webtoon-phinf.pstatic.net/p1.jpg" alt="x"/></figure>'
            '<img src="https://cdn.mangadex.network/p.png"/></div>')
    out = rewrite_hotlinked(html)
    assert "/api/img?u=https%3A%2F%2Fwebtoon-phinf.pstatic.net%2Fp1.jpg" in out
    assert 'src="https://cdn.mangadex.network/p.png"' in out  # left as-is
    # Idempotent: re-running doesn't double-wrap.
    assert rewrite_hotlinked(out) == out


def test_prose_pages_still_reconstruct_paragraphs():
    # A text chapter (span/<br> soup, no images) must still become <p> paragraphs.
    html = "<div class='chapter-content'><span>Line one.</span><br><br><span>Line two.</span></div>"
    _t, body = extract_main_content(html, "https://novel.example/novel/x/chapter/1")
    assert body.count("<p>") >= 2
    assert "Line one." in body and "Line two." in body
