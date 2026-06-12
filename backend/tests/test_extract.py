"""Tests for adaptive web-extraction helpers (sequential crawling)."""
from __future__ import annotations

from app.ingestion.extract import (
    chapter_base,
    chapter_number,
    chapter_title_from,
    classify_page,
    extract_main_content,
    find_chapter_links,
    find_next_targets,
    is_work_url,
    looks_paginated_toc,
    norm_title,
    synthesize_next_chapter_url,
    work_title_from,
    work_url_for,
)

# A novellunar-shaped novel landing page: og metadata + a chapter list. This is the
# REGRESSION fixture — the content getter must keep recognizing/parsing it as we
# improve the engine. (Trimmed but structurally faithful to the real page.)
NOVELLUNAR_NOVEL_HTML = """
<html lang="en"><head>
  <meta property="og:title" content="Library of Heaven's Path Novel">
  <meta property="og:type" content="book">
  <meta property="og:site_name" content="Novellunar">
  <meta property="og:image" content="/media/covers/lohp.webp">
  <meta property="og:description"
        content="Zhang Xuan traverses into a foreign world and becomes a teacher in an academy.
                 With the mysterious Library of Heaven's Path in his mind, he sets out.">
  <title>Library of Heaven's Path Novel - Read Free | Novellunar</title>
</head><body>
  <h1>Library of Heaven's Path</h1>
  <div class="novel-stats">Chapters (2271)</div>
  <ul class="chapter-list">
    <li><a href="/novel/library-of-heavens-path-v1/chapter/1">Chapter 1: Swindler</a></li>
    <li><a href="/novel/library-of-heavens-path-v1/chapter/2">Chapter 2: A Genius</a></li>
    <li><a href="/novel/library-of-heavens-path-v1/chapter/3">Chapter 3: Heaven's Path</a></li>
    <li><a href="/novel/library-of-heavens-path-v1/chapter/4">Chapter 4: The Skill</a></li>
  </ul>
  <aside class="recommendations">
    <a href="/novel/some-other-novel">Some Other Novel</a>
  </aside>
</body></html>
"""

NOVELLUNAR_CHAPTER_HTML = """
<html lang="en"><head>
  <meta property="og:title" content="Library of Heaven's Path Chapter 1: Swindler | Novellunar">
  <meta property="og:type" content="article">
</head><body>
  <article class="chapter-content">
    <h2>Chapter 1: Swindler</h2>
    <p>Zhang Xuan opened his eyes to an unfamiliar ceiling, the morning light spilling in.</p>
    <p>"Where am I?" he muttered, pushing himself upright as the memories flooded his mind.</p>
    <p>The world had changed, and with it, everything he thought he knew about teaching.</p>
  </article>
  <a class="next" href="/novel/library-of-heavens-path-v1/chapter/2">Next Chapter ›</a>
</body></html>
"""


def test_classify_novel_landing_page_is_work():
    base = "https://novellunar.com/novel/library-of-heavens-path-v1"
    pc = classify_page(NOVELLUNAR_NOVEL_HTML, base)
    assert pc.kind == "work", pc.signals
    assert pc.work_url == base
    assert pc.advertised == 2271
    assert pc.listed >= 4
    assert "Library of Heaven's Path" in pc.title


def test_classify_chapter_page_points_at_its_work():
    url = "https://novellunar.com/novel/library-of-heavens-path-v1/chapter/1"
    pc = classify_page(NOVELLUNAR_CHAPTER_HTML, url)
    assert pc.kind == "chapter"
    assert pc.work_url == "https://novellunar.com/novel/library-of-heavens-path-v1"


def test_classify_listing_and_junk_pages():
    listing = classify_page("<html><body><a href='/novel/a'>A</a></body></html>",
                            "https://s.com/browse/popular")
    assert listing.kind == "listing"
    junk = classify_page("<html><body>hi</body></html>", "https://s.com/account/settings")
    assert junk.kind == "other"


def test_work_url_for_strips_chapter():
    assert work_url_for("https://s/novel/x/chapter/5") == "https://s/novel/x"
    assert work_url_for("https://s/book/x/chapter-5") == "https://s/book/x"
    assert work_url_for("https://s/novel/x") == "https://s/novel/x"


def test_is_work_url():
    assert is_work_url("https://novellunar.com/novel/library-of-heavens-path-v1")
    assert not is_work_url("https://novellunar.com/novel/x/chapter/3")
    assert not is_work_url("https://novellunar.com/browse/popular")
    assert not is_work_url("https://novellunar.com/account/login")


def test_norm_title_groups_same_work_across_sites():
    a = norm_title("Library of Heaven's Path (Novel)")
    b = norm_title("library of heavens path - Web Novel")
    c = norm_title("The Library of Heaven's Path")
    assert a == b == c
    assert norm_title("Library of Heaven's Path") != norm_title("Martial God Asura")


def test_norm_title_folds_accents():
    # Accents are FOLDED to ASCII (not deleted) so accented titles match the ASCII forms usenet
    # releases use — the "My Ántonia"→"ntonia" false-negative found in the availability audit.
    assert norm_title("My Ántonia") == norm_title("My Antonia") == "my antonia"
    assert norm_title("Abel Sánchez") == "abel sanchez"
    assert norm_title("Les Misérables") == norm_title("Les Miserables")
    assert norm_title("Étranger") == "etranger"


def test_norm_title_preserves_non_latin_script():
    """E1: CJK / Cyrillic / Hangul titles must NOT collapse to an empty key (which gave every
    native title the same blank grouping key + empty release queries). Latin folding is unchanged."""
    assert norm_title("進撃の巨人") == "進撃の巨人"
    assert norm_title("Метро 2033") == "метро 2033"
    assert norm_title("나 혼자만 레벨업") == "나 혼자만 레벨업"
    # mixed: Latin folds/lowercases, native script preserved, volume marker stripped
    assert norm_title("Attack on Titan 進撃の巨人 Vol 3") == "attack on titan 進撃の巨人"
    # two different native titles get DIFFERENT keys (no empty-key collision)
    assert norm_title("進撃の巨人") != norm_title("鬼滅の刃")


def test_norm_title_strips_volume_markers_safely():
    """14A: per-volume titles collapse to ONE series key via EXPLICIT volume/chapter markers, but a
    bare trailing number (a real part of the title) is never stripped."""
    base = norm_title("Berserk")
    for v in ("Berserk Vol 1", "Berserk vol.2", "Berserk Volume 3", "Berserk #4", "Berserk (5)",
              "Berserk Book 2", "Berserk Ch. 12"):
        assert norm_title(v) == base, v
    # CJK volume/chapter markers
    assert norm_title("進撃の巨人 第3巻") == norm_title("進撃の巨人")
    # must NOT corrupt titles whose number is part of the name
    assert norm_title("Catch 22") == "catch 22"
    assert "2001" in norm_title("2001 A Space Odyssey")
    assert norm_title("Chapter House") == "chapter house"   # 'chapter' word w/o a number is kept


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


# --------------------------------------------------------------------------
# Regression tests for index-tab bugs (j-novel /read parts, Gutenberg bylines,
# site-root homepages cataloged as works).
# --------------------------------------------------------------------------

def test_jnovel_read_parts_collapse_to_series_work():
    """j-novel.club reader pages (/read/<slug>-volume-N-part-M) are chapters of the
    /series/<slug> work, not standalone works — so all parts share one work_url."""
    p1 = "https://j-novel.club/read/reborn-to-reign-imposing-my-rules-with-my-mastery-of-magic-volume-1-part-1"
    p2 = "https://j-novel.club/read/reborn-to-reign-imposing-my-rules-with-my-mastery-of-magic-volume-1-part-7"
    series = "https://j-novel.club/series/reborn-to-reign-imposing-my-rules-with-my-mastery-of-magic"
    assert is_work_url(p1) is False
    assert work_url_for(p1) == series
    assert work_url_for(p2) == series
    # The real series landing page is left untouched.
    assert work_url_for(series) == series


def test_hyphenated_volume_part_is_a_chapter_url():
    from app.ingestion.extract import is_chapter_url
    assert is_chapter_url("https://x/read/some-slug-volume-2-part-5") is True
    assert is_chapter_url("https://x/read/some-slug-chapter-12") is True
    # A plain work slug that merely contains a number is NOT a chapter.
    assert is_chapter_url("https://x/series/mob-psycho-100") is False


def test_split_byline_extracts_author_from_title():
    from app.ingestion.extract import split_byline
    assert split_byline("Moby Dick; Or, The Whale by Herman Melville") == (
        "Moby Dick; Or, The Whale", "Herman Melville")
    assert split_byline("Pride and Prejudice by Jane Austen") == ("Pride and Prejudice", "Jane Austen")
    # No plausible byline → unchanged.
    assert split_byline("Eighteen's Bed") == ("Eighteen's Bed", None)


def test_read_prefix_stripped_from_work_title():
    assert work_title_from("Read Reborn to Reign") == "Reborn to Reign"
    # Doesn't eat a legitimate leading word that isn't the reader verb.
    assert work_title_from("Ready Player One") == "Ready Player One"


def test_site_root_is_not_a_work():
    """A homepage that advertises a chapter count and links to chapters is a directory of
    works (a listing to crawl), never a work itself."""
    html = """
      <html><head>
        <meta property="og:title" content="Novellunar - Read Online Novels for Free">
        <meta property="og:description" content="Read thousands of translated web novels online for free.">
      </head><body>
        <div>Chapters (138)</div>
        <a href="/novel/a/chapter/1">Ch 1</a>
        <a href="/novel/b/chapter/1">Ch 1</a>
        <a href="/novel/c/chapter/1">Ch 1</a>
      </body></html>
    """
    pc = classify_page(html, "https://novellunar.com/")
    assert pc.kind == "listing", pc.signals
    assert is_work_url("https://novellunar.com/") is False


def test_gutenberg_author_page_is_a_listing_not_a_work():
    """Gutenberg /ebooks/author/<id> ("Books by X") lists an author's works — it points at
    works to crawl but is not itself a single work."""
    from app.ingestion.extract import is_listing_url
    url = "https://www.gutenberg.org/ebooks/author/61"
    assert is_work_url(url) is False
    assert is_listing_url(url) is True
    # A real Gutenberg book is still a work.
    assert is_work_url("https://www.gutenberg.org/ebooks/2701") is True
    assert is_listing_url("https://www.gutenberg.org/ebooks/2701") is False


def test_highest_chapter_number_beats_link_counting():
    html = """<html><head><meta property="og:title" content="X"></head><body>
      <a href="/novel/x/chapter/1">Chapter 1</a>
      <a href="/novel/x/chapter/1532">Chapter 1532: Latest</a>
    </body></html>"""
    from app.ingestion.extract import highest_chapter_number
    assert highest_chapter_number(html) == 1532


def test_classify_rejects_bare_site_name_title():
    # A page whose title is just the site's own name must not become a work.
    html = """<html><head>
      <meta property="og:title" content="Project Gutenberg">
      <meta property="og:site_name" content="Project Gutenberg">
      <meta property="og:type" content="book">
      <meta property="og:description" content="Project Gutenberg is a library of free ebooks.">
    </head><body><a href="/ebooks/1">A</a><a href="/ebooks/2">B</a><a href="/ebooks/3">C</a>
    </body></html>"""
    pc = classify_page(html, "https://www.gutenberg.org/ebooks/0")
    assert pc.kind == "other", pc.signals
    assert "site-name-title" in pc.signals


def test_synthesize_next_handles_webtoon_episode_no():
    from app.ingestion.extract import synthesize_next_chapter_url
    u = "https://www.webtoons.com/en/slice-of-life/bluechair/ep-1/viewer?title_no=199&episode_no=1"
    nxt = synthesize_next_chapter_url(u)
    assert nxt == "https://www.webtoons.com/en/slice-of-life/bluechair/ep-2/viewer?title_no=199&episode_no=2"
    # Real (longer) episode slug still steps to the next episode.
    u2 = "https://www.webtoons.com/en/slice-of-life/bluechair/ep-1-unstoppable/viewer?title_no=199&episode_no=1"
    assert "episode_no=2" in synthesize_next_chapter_url(u2)
    assert "/ep-2/" in synthesize_next_chapter_url(u2)
    # Numeric chapter URLs still work.
    assert synthesize_next_chapter_url("https://s/novel/x/chapter/5") == "https://s/novel/x/chapter/6"


def test_titles_match_rejects_short_subset_titles():
    from app.ingestion.extract import titles_match, norm_title
    # "My Life" must NOT merge into "My Next Life as a Villainess" (subset, but different work).
    a = norm_title("My Life"); b = norm_title("My Next Life as a Villainess")
    assert titles_match(a, None, b, None) is False
    # Genuine near-duplicate titles across sources still merge.
    c = norm_title("The 100th Regression of the Max-Level Player")
    d = norm_title("100th Regression of the Max Level Player")
    assert titles_match(c, None, d, None) is True
    # Exact normalized match still merges (compatible authors).
    e = norm_title("Library of Heaven's Path (Novel)")
    f = norm_title("library of heavens path web novel")
    assert titles_match(e, None, f, None) is True
