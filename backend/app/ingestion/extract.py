"""Heuristic, adaptive content extraction for web-page chapter ingestion.

Two jobs:
  * find_chapter_links: given a table-of-contents / series page, discover the
    ordered list of chapter page URLs.
  * extract_main_content: given a chapter page, isolate the readable body
    (a lightweight readability heuristic) and return clean HTML + a title.

This is intentionally adaptive rather than site-specific, per the plan's
"figures out how to ingest chapter/page based content automatically".
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

_NOISE_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "aside",
               "form", "iframe", "svg", "button")
_CHAPTERY = re.compile(r"(chapter|episode|chap|ch[\s._-]*\d|part\s*\d|\bvol\b|\bc\d+)", re.I)
_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _same_host(a: str, b: str) -> bool:
    return urlparse(a).netloc == urlparse(b).netloc


def find_chapter_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return ordered, de-duplicated [(url, title)] of likely chapter links."""
    soup = BeautifulSoup(html, "lxml")
    candidates: list[tuple[str, str, float]] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(base_url, href)
        url = url.split("#", 1)[0]
        if not _same_host(url, base_url) or url in seen:
            continue
        text = a.get_text(" ", strip=True)
        score = 0.0
        if _CHAPTERY.search(text):
            score += 2.0
        if _CHAPTERY.search(href):
            score += 1.5
        if _NUM_RE.search(text) or _NUM_RE.search(href):
            score += 0.5
        # Links inside list/table structures are more likely a TOC.
        if a.find_parent(["li", "td", "tr", "ul", "ol"]):
            score += 0.5
        if score >= 2.0:
            seen.add(url)
            candidates.append((url, text or url, score))

    # Order by a number found in the link text/url when available, else document order.
    def sort_key(item: tuple[str, str, float], idx: int) -> tuple:
        url, text, _ = item
        m = _NUM_RE.search(text) or _NUM_RE.search(url)
        return (float(m.group(1)) if m else float(idx),)

    indexed = [(c, i) for i, c in enumerate(candidates)]
    indexed.sort(key=lambda ci: sort_key(ci[0], ci[1]))
    return [(c[0], c[1]) for c, _ in indexed]


_NEXT_TEXTS = ("next chapter", "next chap", "next", "›", "»", "→", "下一章", "下一页", "next page")
# Explicit in-chapter pagination markers only (NOT a bare /N, which is often the
# chapter number itself in /chapter/N style URLs).
_PAGE_SUFFIX = re.compile(r"(?:[/_-]page[-_/]?\d+|[?&](?:page|p)=\d+|[_-]p\d+)$", re.I)
# A chapter URL whose chapter id is a bare trailing integer (safe to increment).
_NUMERIC_CHAPTER = re.compile(r"^(.*?(?:chapter|chap|ch|episode|ep)[/_-]?)(\d+)(/?)$", re.I)
# A hyphenated volume/part/chapter suffix on the final slug, e.g. j-novel.club's
# '…-volume-1-part-2' reader pages or '…-chapter-5'. These are individual chapter/part
# pages of a work, not works in their own right.
_HYPHEN_CHAPTER = re.compile(
    r"-(?:volume|vol|part|pt|chapter|chap|ch|episode|ep)-\d+(?:-(?:part|pt|chapter|chap|ch|ep)-\d+)?/?$",
    re.I,
)
# j-novel.club reader slugs are '<series>-volume-<N>-…' (… = part/act/prologue/…). The series
# slug is everything before the FIRST volume/omnibus/season marker — a reliable split that the
# generic suffix-stripping above can't do (it chokes on 'act'/'prologue' and other non-…-N labels).
_JNOVEL_VOL = re.compile(r"-(?:volume|vol|omnibus|season)-", re.I)


def _is_jnovel(host: str) -> bool:
    host = host.lower()
    return host == "j-novel.club" or host.endswith(".j-novel.club")


def _is_comix(host: str) -> bool:
    host = host.lower()
    return host == "comix.to" or host.endswith(".comix.to")


def _comix_title_parts(path: str) -> list[str] | None:
    """For a comix.to path, the non-empty segments when it's under /title/, else None.
    ``/title/<slug>`` is the series (work) landing; ``/title/<slug>/<chapter-id>`` is a
    virtualized reader page (404s for a plain fetch)."""
    parts = [p for p in path.split("/") if p]
    return parts if parts[:1] == ["title"] else None


def chapter_number(url_or_text: str) -> float | None:
    """Best-effort numeric chapter index from a URL or label."""
    m = re.search(r"chapter[\s._-]*(\d+(?:\.\d+)?)", url_or_text, re.I)
    if m:
        return float(m.group(1))
    m = _NUM_RE.search(url_or_text)
    return float(m.group(1)) if m else None


def chapter_ref_number(title: str | None, source_ref: str | None, index: int) -> float:
    """The human chapter NUMBER for a discovered chapter — from its 'Chapter N' title, else its
    source ref, else its positional index. Used so 'hook from chapter N' means the chapter LABELLED
    N, not the Nth item: some adapters (e.g. comix) index chapters by list POSITION while the real
    number lives only in the title, so position 700 can be 'Chapter 677'."""
    n = chapter_number(title or "")
    if n is None:
        n = chapter_number(source_ref or "")
    return n if n is not None else float(index)


def chapter_base(url: str) -> str:
    """Strip an in-chapter page suffix so two pages of one chapter compare equal."""
    u = url.split("#", 1)[0].rstrip("/")
    prev = None
    while prev != u:
        prev = u
        u = _PAGE_SUFFIX.sub("", u).rstrip("/")
    return u


def _find_links_by_text(soup: BeautifulSoup, base_url: str, texts) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        low = label.lower()
        rel = " ".join(a.get("rel", [])).lower() if a.get("rel") else ""
        cls = " ".join(a.get("class", [])).lower() if a.get("class") else ""
        hay = f"{low} {rel} {cls} {a.get('id','').lower()} {a.get('title','').lower()}"
        if any(t in hay for t in texts) or low in ("›", "»", "→"):
            url = urljoin(base_url, a["href"]).split("#", 1)[0]
            if _same_host(url, base_url) and url.rstrip("/") != base_url.rstrip("/"):
                out.append((url, label))
    return out


def find_next_link(html: str, base_url: str) -> str | None:
    """Find any 'next' link for paginated serials (back-compat helper)."""
    soup = BeautifulSoup(html, "lxml")
    hits = _find_links_by_text(soup, base_url, _NEXT_TEXTS)
    return hits[0][0] if hits else None


def find_next_targets(html: str, current_url: str) -> tuple[str | None, str | None, str | None]:
    """Classify forward links on a chapter page.

    Returns (next_chapter_url, next_chapter_title, next_page_url) where
    next_page_url is an in-chapter pagination link (same chapter, different page).
    """
    soup = BeautifulSoup(html, "lxml")
    cur_base = chapter_base(current_url)
    cur_num = chapter_number(current_url)

    next_chapter: tuple[str, str] | None = None
    next_page: str | None = None

    for url, label in _find_links_by_text(soup, current_url, _NEXT_TEXTS):
        if url.rstrip("/") == current_url.rstrip("/"):
            continue
        num = chapter_number(url)
        # Classify primarily by chapter number when both are known.
        if cur_num is not None and num is not None:
            if num == cur_num:
                next_page = next_page or url
                continue
            if num < cur_num:
                continue
            if next_chapter is None:
                next_chapter = (url, label)
            continue
        # Otherwise fall back to comparing the page-stripped chapter base.
        if chapter_base(url) == cur_base:
            next_page = next_page or url
        elif next_chapter is None:
            next_chapter = (url, label)

    nc_url, nc_title = (next_chapter if next_chapter else (None, None))
    return nc_url, nc_title, next_page


def synthesize_next_chapter_url(url: str) -> str | None:
    """For numeric chapter URLs (…/chapter/5, …/chapter-5) or webtoon-style ?episode_no=N,
    return the URL with the number incremented. Used when the "next" link is JS-rendered
    (no <a href>), so a sequential crawl can still step forward deterministically."""
    # Webtoon (LINE) episode: bump ?episode_no=N and the matching /ep-N/ path slug. The slug
    # need not be exact — webtoons redirects /ep-<n>/viewer?…&episode_no=<n> to the real one.
    m = re.search(r"([?&]episode_no=)(\d+)", url, re.I)
    if m:
        n = int(m.group(2))
        nxt = url[: m.start(2)] + str(n + 1) + url[m.end(2):]
        nxt = re.sub(r"/ep-\d+(?:-[^/]*)?/", f"/ep-{n + 1}/", nxt, flags=re.I)
        return nxt
    u = url.split("#", 1)[0].split("?", 1)[0]
    m = _NUMERIC_CHAPTER.match(u)
    if not m:
        return None
    prefix, num, trail = m.group(1), int(m.group(2)), m.group(3)
    return f"{prefix}{num + 1}{trail}"


def series_prefix(url: str) -> str | None:
    """The chapter-URL prefix up to (and including) the chapter token, e.g.
    '…/novel/x/chapter/'. Two chapters of the same work share this prefix."""
    u = url.split("#", 1)[0].split("?", 1)[0]
    m = _NUMERIC_CHAPTER.match(u)
    return m.group(1) if m else None


def chapter_num_from_ref(url: str) -> int | None:
    """The chapter number from a numeric chapter URL (…/chapter/5 -> 5), taken right after
    the chapter token and anchored to the end — so a number elsewhere in the URL (e.g. a
    slug like …/library-of-heavens-path-v1/chapter/5) can't fool it like chapter_number()."""
    u = url.split("#", 1)[0].split("?", 1)[0]
    m = _NUMERIC_CHAPTER.match(u)
    return int(m.group(2)) if m else None


def is_chapter_url(url: str) -> bool:
    bare = url.split("#", 1)[0]
    if _QS_EPISODE.search(bare):  # webtoon-style ?episode_no=N
        return True
    pr = urlparse(bare)
    # j-novel.club has a clean split: /read/<slug> is ALWAYS a reader page (a volume/part of a
    # series, never a work), and /series/<slug> is ALWAYS the work landing — even when the series
    # name itself ends in a chaptery suffix (e.g. '…-manga-part-2', a distinct series). Decide by
    # path so the generic suffix regex below can't misread a series name as a chapter.
    if _is_jnovel(pr.netloc):
        if pr.path.startswith("/read/"):
            return True
        if pr.path.startswith("/series/"):
            return False
    # comix.to: /title/<slug> is the series landing; /title/<slug>/<chapter-id> is a virtualized
    # reader page that 404s for a plain crawler fetch — mark it a chapter so the indexer collapses
    # it to the series page instead of enqueuing thousands of dead reader URLs.
    if _is_comix(pr.netloc):
        parts = _comix_title_parts(pr.path)
        if parts is not None:
            return len(parts) >= 3
    path = bare.split("?", 1)[0]
    if _NUMERIC_CHAPTER.match(path):
        return True
    # Hyphenated volume/part/chapter suffix (e.g. j-novel.club /read/<slug>-volume-1-part-2).
    return bool(_HYPHEN_CHAPTER.search(path))


def og_title(html: str) -> str:
    """Prefer og:title, then <h1>, then <title> for a page's display title."""
    soup = BeautifulSoup(html, "lxml")
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        return og["content"].strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    return soup.title.get_text(" ", strip=True) if soup.title else ""


def advertised_chapter_count(html: str) -> int | None:
    """The total chapter count a novel page advertises (for sequential crawls where
    the full TOC can't be enumerated). Prefers a 'Chapters (1234)' tab label, else the
    first '1234 chapters' stat (which on these sites precedes recommendation lists)."""
    text = BeautifulSoup(html, "lxml").get_text(" ")
    m = re.search(r"chapters?\s*\(\s*([\d,]+)\s*\)", text, re.I)
    if not m:
        m = re.search(r"\b([\d,]{2,})\s*chapters?\b", text, re.I)
    if not m:
        return None
    n = int(m.group(1).replace(",", ""))
    return n if 1 < n < 100000 else None


# Chapter / episode / volume-part numbers embedded in a link or label.
_CHAP_NUM_RE = re.compile(
    r"(?:chapter|chapitre|chap|episode|ep|cap|ch|part|vol(?:ume)?)[\s._/#-]*(\d{1,6})", re.I
)
_EPISODE_QS_RE = re.compile(r"(?:episode_no|episode_id|chapter_no)=(\d{1,6})", re.I)


def highest_chapter_number(html: str, base_url: str = "") -> int | None:
    """The largest chapter/episode number referenced anywhere on a page.

    A work's landing/TOC page usually links its *latest* chapter ("Chapter 1532") even when
    it only renders a slice of the list — so the highest number is a far better count than
    enumerating links (which misses paginated/JS-loaded TOCs and, for webtoons, only sees the
    ~10 latest episodes). Scans chapter-ish link hrefs + labels and webtoon ?episode_no=N."""
    soup = BeautifulSoup(html, "lxml")
    best = 0
    for a in soup.find_all("a", href=True):
        hay = f"{a['href']} {a.get_text(' ', strip=True)}"
        for m in _CHAP_NUM_RE.finditer(hay):
            best = max(best, int(m.group(1)))
        for m in _EPISODE_QS_RE.finditer(a["href"]):
            best = max(best, int(m.group(1)))
    return best if 1 < best < 100000 else None


def og_image(html: str, base_url: str = "") -> str | None:
    """Best cover image for a page: og:image → twitter:image → link image_src →
    a cover-ish <img>. Returns an absolute URL."""
    soup = BeautifulSoup(html, "lxml")
    for sel, attr in [
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('meta[property="og:image:url"]', "content"),
        ('link[rel="image_src"]', "href"),
    ]:
        el = soup.select_one(sel)
        if el and el.get(attr):
            return urljoin(base_url, el[attr].strip())
    img = soup.select_one(
        'img[class*=cover], img[id*=cover], img[class*=poster], .cover img, .book-cover img'
    )
    if img and img.get("src"):
        return urljoin(base_url, img["src"].strip())
    return None


def _meta_content(soup: BeautifulSoup, *selectors: str) -> str | None:
    for sel in selectors:
        el = soup.select_one(sel)
        if el and el.get("content"):
            val = el["content"].strip()
            if val:
                return val
    return None


def page_metadata(html: str, base_url: str = "") -> dict:
    """Gather preview metadata for an indexed page: description, author, cover, site name,
    type, language. Used so the reader can preview what a discovered title is about."""
    soup = BeautifulSoup(html, "lxml")
    description = _meta_content(
        soup,
        'meta[property="og:description"]',
        'meta[name="description"]',
        'meta[name="twitter:description"]',
    )
    if not description:
        # Fall back to the first substantial paragraph.
        for p in soup.find_all("p"):
            t = p.get_text(" ", strip=True)
            if len(t) >= 60:
                description = t
                break
    author = _clean_author(
        _meta_content(
            soup,
            'meta[name="author"]',
            'meta[property="article:author"]',
            'meta[property="book:author"]',
            'meta[name="twitter:creator"]',
        )
    )
    if not author:  # visible author element (rel/itemprop/common class names)
        for sel in ('[rel="author"]', '[itemprop="author"]', '.author-name',
                    '.author a', '.series-author', '.writer'):
            el = soup.select_one(sel)
            if el:
                author = _clean_author(el.get_text(" ", strip=True))
                if author:
                    break
    if not author and description:  # "written by X" / "Novel by X" in the blurb
        author = _author_from_text(description)
    site_name = _meta_content(soup, 'meta[property="og:site_name"]')
    page_type = _meta_content(soup, 'meta[property="og:type"]')
    lang = None
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        lang = html_tag["lang"].strip()[:16] or None
    return {
        # Keep the whole synopsis (a few KB max) — the detail view shows it in full; a tight cap
        # here is what truncated long blurbs. Generous bound just guards against a runaway page.
        "description": (description or "").strip()[:8000] or None,
        "author": (author or "").strip()[:255] or None,
        "cover_url": og_image(html, base_url),
        "site_name": (site_name or "").strip()[:255] or None,
        "type": (page_type or "").strip()[:64] or None,
        "language": lang,
    }


# Keyword prefixes are case-insensitive (scoped), but the [A-Z] initial test stays
# case-SENSITIVE — under a global re.I it would match any letter, treating every
# "word." as an initial and over-capturing into the next sentence.
_AUTHOR_TEXT_RE = re.compile(
    r"(?i:written\s+by|novel\s+by|story\s+by|art\s+by|author[:\s])\s*(?i:the\s+author\s+)?"
    # Allow initials ("A. B. Smith", "J.R.R. Tolkien") but stop at a word-ending period.
    r"((?:[A-Z]\.\s*|[^|.\n]){2,70})"
)


# Aggregator / host names that scraped pages sometimes expose where the author should be
# (e.g. a "source: NovelBin" credit). These are never real authors — reject them so the
# card doesn't attribute a work to a website.
_NON_AUTHOR_NAMES = frozenset({
    "novelbin", "novelfull", "webnovel", "royalroad", "readnovel", "novelhall",
    "lightnovel", "wuxiaworld", "mtlnovel", "fanmtl", "novelupdates", "allnovel",
    "freewebnovel", "novellunar", "admin", "anonymous", "unknown", "n/a", "author",
})


def _clean_author(s: str | None) -> str | None:
    """Reject URL/path/handle-ish junk (and pure numbers) that isn't a real author name."""
    s = (s or "").strip().strip(",;|")
    if not s or len(s) > 80:
        return None
    if re.search(r"https?://|[/?=@<>]|search:", s, re.I):
        return None
    if not re.search(r"[^\W\d_]", s):  # needs at least one letter (any script)
        return None
    if re.sub(r"[^a-z0-9/]", "", s.lower()) in _NON_AUTHOR_NAMES:
        return None
    return s or None


def _author_from_text(text: str) -> str | None:
    """Best-effort 'by X' author from a synopsis/blurb (handles 'Novel by …')."""
    m = _AUTHOR_TEXT_RE.search(text or "")
    if not m:
        return None
    a = m.group(1).strip()
    # Cut trailing sentence continuations the blurb runs into.
    a = re.split(
        r",?\s*(?:this\s+book|the\s+synopsis|is\s+a\b|, and\b)", a, maxsplit=1, flags=re.I
    )[0]
    return _clean_author(a)


def chapter_title_from(title_text: str) -> str:
    """Pull a 'Chapter N: Subtitle' label out of a page/og title."""
    if not title_text:
        return ""
    m = re.search(r"chapter\s+(\d+(?:\.\d+)?)\s*[:：]\s*([^:|\-–—]+)", title_text, re.I)
    if m:
        return f"Chapter {m.group(1)}: {m.group(2).strip()}"
    m = re.search(r"(chapter\s+\d+(?:\.\d+)?[^|\-–—:]*)", title_text, re.I)
    return m.group(1).strip() if m else ""


def work_title_from(title: str) -> str:
    """Trim a chapter-page title down to the work title.
    'Library of Heaven's Path Chapter 1: … | Novellunar' -> "Library of Heaven's Path"."""
    if not title:
        return title
    cut = len(title)
    # NOTE: ": " (colon + SPACE), not bare ":" — a colon is a subtitle separator only when spaced
    # ("Dune: Part One" → "Dune"). A bare intrinsic colon must survive ("Re:Zero …" → "Re:Zero",
    # "WALL:E"), or the catalog row for the whole work is truncated to "Re" and groups/searches wrong.
    for marker in (" Chapter ", " chapter ", " - ", " | ", " — ", ": "):
        i = title.find(marker)
        if 0 < i < cut:
            cut = i
    out = title[:cut].strip(" -|—:") or title
    # Common trailing site noise on novel pages.
    out = re.sub(r"\s+(Novel|Light Novel|Web Novel)$", "", out, flags=re.I).strip()
    # Reader pages often title themselves "Read <Work>" (e.g. j-novel.club); drop the verb.
    out = re.sub(r"^(?:read|watch|listen to)\s+(?=\S)", "", out, flags=re.I).strip()
    return out or title


_BYLINE_RE = re.compile(r"^(.*\S)\s+by\s+(\S.*)$", re.I)  # greedy → splits on the LAST ' by '


def split_byline(title: str) -> tuple[str, str | None]:
    """Split a 'Work Title by Author Name' display title into (work_title, author).

    Project Gutenberg (and many catalog pages) put the byline in the page title with no
    separate author field. Returns (title, None) unchanged when there's no plausible
    byline — the author candidate must read like a name (handled by :func:`_clean_author`)
    and be reasonably short, so real titles that merely contain the word 'by' are left be."""
    if not title:
        return title, None
    m = _BYLINE_RE.match(title.strip())
    if not m:
        return title, None
    work, author = m.group(1).strip(" ,;|"), _clean_author(m.group(2))
    # A real byline is a handful of name tokens, not a trailing clause.
    if not work or not author or len(author.split()) > 8:
        return title, None
    return work, author


def looks_paginated_toc(html: str, found_links: int) -> bool:
    """Heuristic: the TOC only shows a slice (dropdown/select range jumper present,
    or it claims far more chapters than are linked)."""
    soup = BeautifulSoup(html, "lxml")
    range_re = re.compile(r"\bc\.?\s*\d+\s*[-–]\s*c?\.?\s*\d+", re.I)
    for sel in soup.find_all("select"):
        opts = sel.find_all("option")
        range_opts = sum(1 for o in opts if range_re.search(o.get_text(" ", strip=True)))
        if len(opts) >= 3 and range_opts >= 2:
            return True
    m = re.search(r"(\d[\d,]{2,})\s*chapters?", html, re.I)
    if m:
        claimed = int(m.group(1).replace(",", ""))
        if claimed > max(found_links * 2, found_links + 20):
            return True
    return False


def _density(node: Tag) -> int:
    """Approximate readable-text weight of a node (paragraph text length)."""
    paras = node.find_all("p")
    if paras:
        return sum(len(p.get_text(" ", strip=True)) for p in paras)
    return len(node.get_text(" ", strip=True))


def _promote_lazy_images(soup: BeautifulSoup) -> None:
    """Move lazy-load image URLs (data-url/data-src/srcset) into src so the real image
    (not a 1x1 placeholder) survives extraction. Comic/manga readers rely on this."""
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if src and not _IMG_PLACEHOLDER.search(src):
            continue
        for attr in _LAZY_IMG_ATTRS:
            if img.get(attr):
                img["src"] = img[attr].strip()
                break
        else:
            if img.get("srcset"):
                # take the last (largest) candidate
                img["src"] = img["srcset"].split(",")[-1].strip().split(" ")[0]


def _real_images(node: Tag) -> list[Tag]:
    return [
        im for im in node.find_all("img")
        if (im.get("src") or "") and not _IMG_PLACEHOLDER.search(im.get("src") or "")
    ]


_MAX_COMIC_PANELS = 600  # a single chapter shouldn't exceed this (bounds chapter HTML size)


def _image_strip_html(imgs: list[Tag], base_url: str) -> str:
    """Render a comic page's image sequence as readable HTML, matching the .comic /
    .comic-page markup the reader styles for full-width, gapless stacked pages (same
    shape produced by local CBZ/CBR imports)."""
    pages = []
    for im in imgs[:_MAX_COMIC_PANELS]:
        src = urljoin(base_url, im["src"]) if base_url else im["src"]
        alt = im.get("alt") or ""
        pages.append(
            f'<figure class="comic-page"><img src="{_esc(src)}" alt="{_esc(alt)}"/></figure>'
        )
    return '<div class="comic">' + "\n".join(pages) + "</div>"


def extract_main_content(html: str, base_url: str = "") -> tuple[str, str]:
    """Return (title, clean_html) for the main body of a chapter page.

    Handles both prose (reconstructs <p> from span/<br> soup) and image-based comics
    (preserves the lazy-loaded image strip, which prose reconstruction would discard)."""
    soup = BeautifulSoup(html, "lxml")
    _promote_lazy_images(soup)

    title = ""
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
    h1 = soup.find(["h1", "h2"])
    if h1:
        title = h1.get_text(" ", strip=True) or title

    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()

    # Comic/manga: if the page is a strip of images with little prose, keep the images.
    # Prefer a known comic-viewer container (e.g. webtoons #_imageList) so we capture the
    # panels, not unrelated thumbnail navigation; else fall back to the most-images block.
    img_container = None
    img_best = 0
    for node in soup.select(
        "#_imageList, [class*=viewer_img], [class*=chapter-images], [class*=chapter_images], "
        "[class*=reading-content], [class*=comic-page], [class*=webtoon], [id*=reader], "
        "[class*=reader-area], [class*=image-list]"
    ):
        n = len(_real_images(node))
        if n > img_best:
            img_best, img_container = n, node
    if img_container is None or img_best < 3:  # no explicit container → densest-by-images
        for node in soup.find_all(["div", "section", "article"]):
            n = len(_real_images(node))
            if n > img_best:
                img_best, img_container = n, node
    if img_container is not None and img_best >= 3:
        # Only treat as a comic strip when images dominate over prose in that block.
        if _density(img_container) < max(400, img_best * 40):
            return title or "Chapter", _image_strip_html(_real_images(img_container), base_url)

    # Prefer obvious content containers, else pick the densest block.
    best: Tag | None = None
    explicit = soup.select_one(
        "article, [class*=chapter-content], [class*=chapter_content], "
        "[class*=reading-content], [id*=chapter-content], .entry-content, .post-content, main"
    )
    if explicit and _density(explicit) > 200:
        best = explicit
    else:
        best_score = 0
        for node in soup.find_all(["article", "section", "div"]):
            score = _density(node)
            if score > best_score:
                best_score = score
                best = node
    if best is None:
        best = soup.body or soup

    # Resolve relative links/images.
    if base_url:
        for a in best.find_all("a", href=True):
            a["href"] = urljoin(base_url, a["href"])
        for img in best.find_all("img", src=True):
            img["src"] = urljoin(base_url, img["src"])

    # Many sites ship chapter text as a soup of <span>s / <br>s with no real
    # paragraphs — reconstruct <p> blocks so it reads well and is trackable. But if the
    # block carries real images (illustrated chapter), keep the original markup so they
    # aren't dropped by the text-only reconstruction.
    if len(best.find_all("p")) >= 2 or _real_images(best):
        return title or "Chapter", best.decode_contents()
    return title or "Chapter", _reconstruct_paragraphs(best)


def _esc(s: str) -> str:
    # Escape quotes too: this output is interpolated into double-quoted attributes
    # (img src/alt), so an unescaped " would allow attribute breakout.
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def _reconstruct_paragraphs(node) -> str:
    """Turn flat span/br/text content into clean <p> paragraphs.

    Line breaks come from <br> and from whitespace-only text nodes that contain a
    newline (a common 'paragraph separator' pattern). Block children also break."""
    for br in node.find_all("br"):
        br.replace_with("\n")
    for block in node.find_all(["div", "p", "h1", "h2", "h3", "h4", "li", "blockquote"]):
        block.insert_after("\n")
    text = node.get_text()  # concatenates strings; preserves embedded "\n" separators
    paras = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    if not paras:
        return node.decode_contents()
    return "\n".join(f"<p>{_esc(p)}</p>" for p in paras)


# ---------------------------------------------------------------------------
# Smart classification — tell apart literature pages (a work's landing/TOC page,
# or a chapter) from non-literature (browse/listing pages, account/legal junk).
# This is what turns a blind same-host crawl into a "find the actual books" crawl.
# ---------------------------------------------------------------------------

# A work's own landing page: a single slug after a literature-ish category segment,
# e.g. /novel/library-of-heavens-path, /book/foo, /series/bar, /comic/baz. An optional
# id segment before the slug is allowed so DB-style URLs work too, e.g.
# /title/<uuid>/one-piece.
_WORK_PATH_RE = re.compile(
    r"/(?:novel|novels|book|books|series|title|titles|story|stories|fiction|"
    r"manga|manhua|manhwa|comic|comics|webnovel|web-novel|ln|read)/"
    r"(?:[0-9a-f][0-9a-f-]{4,}/)?[^/]+/?$",
    re.I,
)
# Project Gutenberg book landing page path: /ebooks/<numeric id> (its /ebooks?page /
# browse pages are listings, so match only the numeric-id form). Matched against the
# parsed host + path so a URL merely *containing* the literal can't spoof it.
_GUTENBERG_HOST_RE = re.compile(r"(?:^|\.)gutenberg\.org$", re.I)
_GUTENBERG_PATH_RE = re.compile(r"^/ebooks/\d+/?$", re.I)
# A book's CONTENT/file-tree pages, not its catalog entry: /files/<id>/…, /cache/epub/<id>/…
_GUTENBERG_CONTENT_RE = re.compile(r"^/(?:files|cache)/", re.I)


def _is_gutenberg_book(url: str) -> bool:
    pr = urlparse(url)
    return bool(_GUTENBERG_HOST_RE.search(pr.netloc) and _GUTENBERG_PATH_RE.match(pr.path))


def is_noncatalog_content_url(url: str) -> bool:
    """True for a page that holds a work's CONTENT rather than its catalog/landing entry — the
    index only needs the landing URL the hooker resolves from, so crawling the full content is
    pure waste. Currently: Project Gutenberg's /files/ and /cache/ book trees (the hooker works
    off /ebooks/<id>, which listings always link directly)."""
    pr = urlparse(url)
    return bool(_GUTENBERG_HOST_RE.search(pr.netloc) and _GUTENBERG_CONTENT_RE.match(pr.path))
# A work's sub-pages (reviews/comments/stats/…) are NOT the work itself — skip them so
# they don't spawn duplicate catalog rows or clobber the work's title.
_WORK_SUBPAGE_RE = re.compile(
    r"/(?:reviews?|comments?|stats?|gallery|artworks?|art|fanart|similar|"
    r"recommend\w*|characters?|staff|credits|edit|history|discussions?|forum|"
    r"releases?|volumes?|also|downloads?)/?$",  # …/<id>/also = Gutenberg "Readers also downloaded" chrome
    re.I,
)
# Listing keywords that, as the FINAL path segment, make /<category>/<slug> a browse
# page rather than a single work (e.g. /novels/latest, /manga/popular).
_LISTING_SLUG_RE = re.compile(
    r"^(?:latest|popular|trending|new|newest|top|all|completed|ongoing|updated|"
    r"updates|ranking|rankings|hot|recommended|index|a-z|az|browse|search)$",
    re.I,
)
# Query-string works/chapters (LINE Webtoon & similar readers key off ?title_no / ?episode_no
# instead of path segments). A title_no with NO episode_no is the series (work) page; an
# episode_no is a chapter.
_QS_TITLE = re.compile(r"[?&](?:title_no|titleId|title_id|comic_id|seriesId)=\d+", re.I)
_QS_EPISODE = re.compile(r"[?&](?:episode_no|episode_id|chapter_no|chapter_id)=\d+", re.I)
# Comic / manga signals (og:type, og:site_name, title, or URL) → media_kind="comic".
_COMIC_HINT = re.compile(r"webtoon|manhwa|manhua|manga|comic|toon|\bmanhua\b", re.I)
_COMIC_PATH = re.compile(
    r"/(?:manga|manhua|manhwa|comic|comics|webtoon|webtoons|toons?)(?:/|\b)|title_no=", re.I
)
# Manga-only sites whose work URLs don't carry a /manga/-style path (e.g. comix.to/title/<hid>).
_COMIC_DOMAIN = re.compile(r"://(?:www\.)?comix\.to/", re.I)
# A src that is really a lazy-load placeholder, not the actual image.
# High-confidence lazy-load placeholders only. Loose tokens (loading/blank/1x1/lazy)
# were dropping real comic panels whose filenames happen to contain them.
_IMG_PLACEHOLDER = re.compile(
    r"(?:data:image|transparen|placeholder|/spacer\.|/px\.|/pixel\.|/blank\.|dummy)", re.I
)
_LAZY_IMG_ATTRS = (
    "data-url", "data-src", "data-original", "data-lazy-src", "data-echo", "data-image",
)


def detect_media_kind(
    url: str, og_type: str | None = None, site_name: str | None = None, title: str | None = None
) -> str:
    """Best-effort 'comic' vs 'text' from cheap signals. Defaults to 'text'."""
    # NB: title is deliberately EXCLUDED from the signal blob — a prose work whose title merely
    # contains a comic word ("The Comic Latin Grammar", "Comic Arithmetic") was being mis-flagged
    # comic. Rely on og:type / site_name / URL path / domain, which describe the source, not the work.
    blob = " ".join(x for x in (og_type, site_name) if x)
    if _COMIC_HINT.search(blob):
        return "comic"
    if _COMIC_PATH.search(url):
        return "comic"
    if _COMIC_DOMAIN.search(url):  # manga-only sites whose URLs lack a /manga/ path
        return "comic"
    return "text"
# Pages that *list* many works (good to crawl, but not themselves a work).
_LISTING_PATH_RE = re.compile(
    r"/(?:browse|latest|popular|trending|ranking|rankings|rank|top|genre|genres|"
    r"tag|tags|search|list|lists|category|categories|catalog|catalogue|completed|"
    r"ongoing|all|library|index|directory|az|a-z|novels|books|series|comics|manga|"
    # Author / subject / bookshelf landing pages list a person's or topic's works — they
    # point AT works (crawl them) but are not themselves a single work (e.g. Project
    # Gutenberg's /ebooks/author/<id>, /ebooks/subject/<id>, /ebooks/bookshelf/<id>).
    r"authors?|bookshel(?:f|ves)|subjects?)\b",
    re.I,
)
# Pages that are never literature — don't catalog, don't waste crawl budget.
_JUNK_PATH_RE = re.compile(
    r"/(?:login|log-in|signin|sign-in|register|signup|sign-up|logout|account|"
    r"accounts|profile|profiles|user|users|member|members|cart|checkout|donate|"
    r"support|faq|about|about-us|contact|privacy|terms|tos|dmca|policy|policies|"
    r"advertise|ads|bookmark|bookmarks|history|setting|settings|preferences|"
    r"dashboard|admin|wp-admin|wp-login|comment|comments|report|password|forgot)\b",
    re.I,
)


def work_url_for(url: str) -> str:
    """Canonical work/landing URL for a chapter (or sub) page.

    '…/novel/x/chapter/5' -> '…/novel/x'; '…/book/x/chapter-5' -> '…/book/x'.
    Webtoon-style '…/ep-1/viewer?title_no=N&episode_no=M' -> '…/list?title_no=N'.
    Returns the cleaned URL unchanged when there's nothing chapter-ish to strip."""
    # Query-string readers (webtoons): the work is keyed by title_no. Collapse any
    # viewer/episode URL to the canonical series (list) page, preserving title_no (the
    # work key) and dropping episode_no — and keep title_no on the series page itself
    # (don't let the generic query-stripping below discard it).
    if _QS_TITLE.search(url):
        pr = urlparse(url)
        qs = parse_qs(pr.query)
        tno = next((qs[k][0] for k in ("title_no", "titleId", "title_id", "comic_id", "seriesId")
                    if qs.get(k)), None)
        if tno:
            new_path = re.sub(r"/(?:[^/]+/)?viewer/?$", "/list", pr.path)
            if not new_path.rstrip("/").endswith("/list"):
                new_path = new_path.rstrip("/") + "/list"
            return f"{pr.scheme}://{pr.netloc}{new_path}?title_no={tno}"
    clean = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    # j-novel.club reader pages live under /read/<series>-volume-<…>; the work landing is
    # /series/<series>. Derive the series slug as everything before the first volume/omnibus/
    # season marker — reliable across part/act/prologue/… labels, unlike the generic stripping
    # below (which produced wrong '/series/…-volume-12-act-1' URLs that 404 and waste a request).
    pr0 = urlparse(clean)
    if pr0.path.startswith("/read/") and _is_jnovel(pr0.netloc):
        slug = pr0.path[len("/read/"):]
        m = _JNOVEL_VOL.search(slug)
        if m:
            return f"{pr0.scheme}://{pr0.netloc}/series/{slug[:m.start()]}"
        return clean  # no volume marker → can't derive the series; the caller skips this dead-end
    # comix.to: collapse a /title/<slug>/<chapter-id> reader URL to the series landing /title/<slug>.
    if _is_comix(pr0.netloc):
        parts = _comix_title_parts(pr0.path)
        if parts is not None and len(parts) >= 2:
            return f"{pr0.scheme}://{pr0.netloc}/title/{parts[1]}"
    stripped = re.sub(r"/chapters?(?:[/_-].*)?$", "", clean, flags=re.I)
    stripped = re.sub(
        r"/(?:chapter|chap|ch|episode|ep|vol|volume|part)[/_-]?\d.*$", "", stripped, flags=re.I
    )
    # Hyphenated volume/part/chapter suffix on the final slug (similar LN readers):
    # '…-magic-volume-1-part-2' -> '…-magic'. Loop so '-volume-1-part-2' fully strips.
    prev = None
    while prev != stripped:
        prev = stripped
        stripped = _HYPHEN_CHAPTER.sub("", stripped)
    return stripped or clean


def is_work_url(url: str) -> bool:
    """True when the URL looks like a single work's landing page."""
    # An individual chapter / volume-part reader page is never the work's landing page
    # (e.g. j-novel.club /read/<slug>-volume-1-part-2). Its parent is work_url_for(url).
    if is_chapter_url(url):
        return False
    # The site root / homepage is never a single work, even if it advertises a chapter
    # count and links to chapters (it's a directory of works — crawl it, don't catalog it).
    if not urlparse(url).path.strip("/"):
        return _QS_TITLE.search(url) is not None and not _QS_EPISODE.search(url)
    # Webtoon-style series page: a title_no with no episode_no.
    if _QS_TITLE.search(url) and not _QS_EPISODE.search(url):
        return not _JUNK_PATH_RE.search(urlparse(url).path)
    if _is_gutenberg_book(url):  # Gutenberg /ebooks/<id>
        return True
    path = urlparse(url).path
    if _WORK_SUBPAGE_RE.search(path):  # /work/x/reviews etc. → not the work
        return False
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if _LISTING_SLUG_RE.match(last):  # /novels/latest etc. → a browse page
        return False
    return bool(_WORK_PATH_RE.search(path)) and not _JUNK_PATH_RE.search(path)


def is_listing_url(url: str) -> bool:
    """True for browse/genre/ranking pages that point AT works (worth crawling)."""
    path = urlparse(url).path
    return bool(_LISTING_PATH_RE.search(path)) and not is_work_url(url)


def is_junk_url(url: str) -> bool:
    """True for pages that are never literature (account/legal/cart/etc.)."""
    path = urlparse(url).path
    return bool(_JUNK_PATH_RE.search(path)) and not is_work_url(url)


def link_priority(url: str) -> int:
    """Crawl priority for a discovered link: work landing=2, listing=1, other=0.
    (Junk/chapter links are handled by the caller before this is consulted.)"""
    if is_work_url(url):
        return 2
    if is_listing_url(url):
        return 1
    return 0


def is_latin_title(s: str | None) -> bool:
    """True if the title is predominantly Latin-script — i.e. it reads as English/romanized rather than
    Greek/Cyrillic/CJK/etc. Digits, spaces and punctuation don't count; an empty or letterless title is
    treated as Latin (there's nothing better to prefer). Used to pick the English display name for a work
    that was discovered in several languages."""
    letters = [c for c in (s or "") if c.isalpha()]
    if not letters:
        return True
    latin = sum(1 for c in letters if "LATIN" in unicodedata.name(c, ""))
    return latin / len(letters) >= 0.6


def norm_title(title: str) -> str:
    """A normalized key for grouping the SAME work discovered on different sites.

    Lowercase, drop apostrophes, strip medium/qualifier words and volume markers,
    collapse to alnum tokens. 'Library of Heaven's Path (Novel)' and
    'library of heavens path - web novel' both -> 'library of heavens path'."""
    # Fold accents so "My Ántonia" / "Abel Sánchez" match the ASCII forms usenet releases use
    # ("My Antonia") — decompose then drop combining marks (á→a, ö→o, é→e). Without this the
    # non-ASCII letter is later deleted ("ántonia"→"ntonia") and never matches.
    t = unicodedata.normalize("NFKD", title or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = t.replace("’", "").replace("‘", "").replace("'", "")
    t = re.sub(
        r"\b(?:the|a|an|light\s+novel|web\s+novel|novel|wn|ln|manga|manhua|manhwa|"
        r"comic|webtoon|raw|raws|english|official|complete|completed|ongoing)\b",
        " ",
        t,
    )
    # Volume/chapter markers → stripped so a series ingested as per-volume titles collapses to one
    # grouping key ("Berserk Vol 1" / "Berserk vol.2" / "Berserk #3" → "berserk"). Only EXPLICIT
    # markers (a keyword/symbol + number, parenthesized trailing number, or a CJK 巻/卷/권/話 marker)
    # — NEVER a bare trailing number, which would corrupt real titles ("Catch 22", "2001").
    before_markers = t
    t = re.sub(r"\b(?:vol(?:ume)?|book|part|season|s|v|ch(?:apter)?|c|ep(?:isode)?)\.?\s*#?\s*\d+\b",
               " ", t)
    t = re.sub(r"#\s*\d+\b", " ", t)                          # "#3"
    t = re.sub(r"\(\s*0*\d+\s*\)\s*$", " ", t)                # trailing "(3)"
    t = re.sub(r"\s[-–]\s+0*\d{1,3}\s*$", " ", t)             # trailing " - 03" / " – 3" vol marker
    t = re.sub(r"第\s*\d+\s*[巻卷话話章节節]", " ", t)        # CJK 第N巻 / 第N話 …
    t = re.sub(r"\d+\s*[巻卷권]", " ", t)                     # N巻 / N권
    # A standalone "V 2" / "S 1" / "C 137" / "Apollo - 13" is its OWN title, not a volume of
    # something — but the single-letter (s/v/c) and trailing "- NN" markers above would erase it
    # whole, leaving a BLANK grouping key (which the union-find guard then refuses to group, silently
    # stranding the title). Never let marker-stripping empty a non-empty title: fall back to the
    # pre-strip form so the number stays part of the key.
    if not re.sub(r"[\W_]+", " ", t, flags=re.UNICODE).strip():
        t = before_markers
    # Keep Unicode word characters (CJK / Cyrillic / Hangul / Arabic …), not just [a-z0-9]: the old
    # ASCII-only strip deleted every non-Latin codepoint, collapsing a CJK/native title to "" — which
    # gave it a blank grouping key (so every native-only title merged into one bogus group) and made
    # native-language release queries empty. Latin is already ASCII-folded above, so \w here is the
    # CJK/native scripts we want to preserve; underscore is treated as a separator (E1).
    t = re.sub(r"[\W_]+", " ", t, flags=re.UNICODE)
    # Re-compose: NFKD above decomposed Hangul syllables into conjoining jamo (나 → ㄴㅏ). Recompose
    # to the canonical NFC form so the key is the natural composed script (Han/Cyrillic are
    # unaffected; Latin is already ASCII). Consistency matters more than the form, but composed is
    # the sane stored key.
    return unicodedata.normalize("NFC", " ".join(t.split()))


def strip_trailing_parens(title: str) -> str:
    """Peel trailing parenthetical qualifiers off a title — providers append series/edition tags
    ("Desert Tales (Wicked Lovely Series)", "Radiant Shadows (Wicked Lovely)",
    "… (French Edition)")
    that split the same work into distinct grouping keys. Groups stack and nest, so peel repeatedly,
    scanning back to the BALANCED open paren; never empty a non-empty title (a fully-parenthesized
    title is kept). This is NOT part of norm_title: within a group, per-edition rows must keep
    distinct keys ("One Piece" vs "One Piece (Official Colored)") — the stripped form is only used
    as an ALTERNATE identity when clustering (see catalog._union_find_groups)."""
    t = title or ""
    while t.rstrip().endswith(")"):
        s = t.rstrip()
        depth, cut = 0, -1
        for idx in range(len(s) - 1, -1, -1):
            if s[idx] == ")":
                depth += 1
            elif s[idx] == "(":
                depth -= 1
                if depth == 0:
                    cut = idx
                    break
        if cut <= 0 or not s[:cut].strip():
            break   # unbalanced, or the title IS the parenthetical → keep as-is
        t = s[:cut]
    return t.strip()


# Bare page titles that are a site's own name or a generic chrome page, never a work.
# Compared against norm_title() output (apostrophes/medium words already stripped).
_GENERIC_TITLES = frozenset({
    "project gutenberg", "gutenberg", "standard ebooks", "novellunar",
    "home", "homepage", "index", "search", "browse", "library", "catalog", "catalogue",
    "login", "log in", "sign in", "register", "sign up", "dashboard", "account",
    "page not found", "not found", "error", "403 forbidden", "404",
    "read online novels stories for free", "read free novels online",
    # Nav-chrome / boilerplate that was leaking in as bogus "works" (each collapsed thousands of
    # unrelated rows into one mega-group): Gutenberg's "Readers also downloaded" panel, test/blank
    # pages, and standalone front-matter labels (a real book is ~never titled exactly these).
    "readers also downloaded", "test", "untitled", "prologue", "epilogue",
    "contents", "table of contents",
})
# Chapter-listing chrome as a bare title ("Chapter 12", "Episode 3") — not a work. Exact-set can't
# catch the numbered variants, so match the shape.
_CHROME_TITLE_RE = re.compile(r"^(?:chapter|episode|ch|ep)\s*\d+\s*$", re.I)


def _is_site_name_title(title: str, site_name: str | None) -> bool:
    """True when a page's title is just the site's own name or a generic chrome label —
    so the site name (e.g. 'Project Gutenberg') never becomes a catalog entry."""
    nt = norm_title(title)
    if not nt:
        return True  # no usable title → not a work
    if nt in _GENERIC_TITLES or _CHROME_TITLE_RE.match(nt):
        return True
    sn = norm_title(site_name or "")
    return bool(sn) and nt == sn


def looks_garbled(s: str | None) -> bool:
    """True when a string is binary/encoding garbage rather than a real title — e.g. a
    Kindle/MOBI flow HTML blob fetched as text ('Table�f�ontents…kindle:flow:0003…').
    Such pages must never become catalog entries."""
    s = s or ""
    if not s:
        return False
    if "�" in s:  # any Unicode replacement char → decoding failed
        return True
    if "kindle:flow" in s or "calibre_generated" in s:  # MOBI/AZW internal markup
        return True
    # A high share of control / non-text bytes (excluding normal whitespace) is garbage.
    ctrl = sum(1 for ch in s if ord(ch) < 32 and ch not in "\t\n\r")
    return len(s) > 0 and ctrl / max(1, len(s)) > 0.05


def _author_norm(a: str | None) -> str:
    # Fold accents (José → jose) then keep Unicode word chars — mirrors norm_title. The old
    # ASCII-only strip (`[^a-z0-9 ]`) deleted EVERY non-Latin codepoint, so any CJK/Cyrillic/Hangul
    # author name collapsed to "" and authors_compatible() then blindly returned True for two
    # DIFFERENT native-script authors, mis-merging same-title-different-author works across the
    # manga/CJK corpus this app targets.
    s = unicodedata.normalize("NFKD", a or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[\W_]+", " ", s, flags=re.UNICODE).strip()


def authors_compatible(a: str | None, b: str | None) -> bool:
    """Authors match when at least one is unknown, or they share a name token.
    Used to AVOID merging same-title-different-author works across sources."""
    na, nb = _author_norm(a), _author_norm(b)
    if not na or not nb:
        return True  # unknown on either side → don't block a title match
    return bool(set(na.split()) & set(nb.split()))


def media_compatible(a: str | None, b: str | None) -> bool:
    """Two media kinds may be HOOKED or metadata-MATCHED together only when they're the same bucket.
    A missing kind defaults to prose ("text"); "audio" and "comic" are each distinct from prose and
    from each other. This is the hard rule that stops a prose crawl/provider catalog entry from being
    hooked onto an audiobook (pasting its cover/description) or a comic onto a novel — the class of bug
    behind the "Harry Potter audiobook got a novellunar web-crawl cover" report. A downloaded audiobook
    therefore never matches indexed/crawled prose content, which is all "text"."""
    return (a or "text") == (b or "text")


# Words that mark a different EDITION of the same work (vs a distinct work). When two titles
# differ ONLY by these, they're the same work in another edition — e.g. 'One Piece' and 'One Piece
# (Official Colored)' (→ 'one piece' vs 'one piece colored') — and should group together as
# selectable editions. (norm_title already strips 'official'/'complete'/medium words, so these are
# the qualifiers that survive into a comparison.)
_EDITION_MARKERS = frozenset({
    "colored", "coloured", "colour", "color", "fullcolor", "fullcolour", "recolored", "full",
    "digital", "digitally", "remaster", "remastered", "hd", "uhd", "uncensored", "uncut",
    "deluxe", "definitive", "anniversary", "omnibus", "collected", "collectors", "collector",
    "edition", "editions", "version",
})


def titles_match(
    a_norm: str, a_author: str | None, b_norm: str, b_author: str | None
) -> bool:
    """Strong cross-source title match: equal normalized titles, the same work in a different
    EDITION, or a high Jaccard token overlap — but never when the authors are known and disjoint.
    Lets 'Library of Heaven's Path' from a web crawl, Readarr and Kapowarr collapse into one entry,
    and 'One Piece' + 'One Piece (Official Colored)' into one (selectable) card, while keeping
    distinct works — including same-franchise spin-offs ('One Piece' vs 'One Piece Party') — apart.

    Uses Jaccard (|∩| / |∪|), NOT one-sided containment: a short title fully contained in a
    longer one (e.g. 'My Life' inside 'My Next Life as a Villainess') scored 1.0 under
    containment and wrongly merged unrelated works."""
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return authors_compatible(a_author, b_author)
    ta, tb = set(a_norm.split()), set(b_norm.split())
    # Same work in a different EDITION: identical once edition-qualifier words (colored, full,
    # digital, deluxe, …) are removed — e.g. 'one piece' vs 'one piece full color'. Checked before
    # Jaccard because the qualifiers can drag the raw overlap below the fuzzy bar.
    core_a, core_b = ta - _EDITION_MARKERS, tb - _EDITION_MARKERS
    if core_a and core_a == core_b:
        return authors_compatible(a_author, b_author)
    if len(ta) < 2 or len(tb) < 2:
        return False  # don't loosely merge one-word titles in the fuzzy branch
    if not authors_compatible(a_author, b_author):
        return False  # known-disjoint authors → never merge (keeps spin-offs/same-title apart)
    # Fuzzy cross-source variation. STRONG token overlap (Jaccard ≥ 0.8) merges as before. OR a
    # MODERATE overlap (≥ 0.55) BACKED by a high char-level token_set_ratio (≥ 90) — this catches
    # transliteration/punctuation/plural/OCR variants ("Re:Zero" vs "Re Zero", "Spider-Man" vs
    # "Spiderman") that pure Jaccard misses, without loosening enough to bundle a real spin-off
    # (which shares fewer tokens AND scores lower char-similarity). Author gate already applied. (E2)
    jacc = len(ta & tb) / len(ta | tb)
    if jacc >= 0.8:
        return True
    if jacc >= 0.55:
        # token_SORT_ratio (NOT token_set): the full sorted strings are compared, so a spin-off's
        # EXTRA tokens drag the score down (One Piece vs One Piece Party ≈ 75, rejected). token_set
        # would score a subset 100 and wrongly merge it. The high bar (≥ 92) only admits char-level
        # variants — plural/minor-spelling — that share most characters, not distinct works.
        from .fuzzy import token_sort_ratio
        return token_sort_ratio(a_norm, b_norm) >= 92
    return False


@dataclass
class PageClass:
    """Result of classifying a fetched page for the smart crawl / catalog."""

    kind: str  # "work" | "chapter" | "toc" | "listing" | "other"
    score: float
    title: str
    work_url: str | None  # canonical landing/TOC URL of the work this page belongs to
    advertised: int | None  # source-advertised chapter total, if stated
    listed: int  # number of own-work chapter links enumerated on this page
    signals: list[str] = field(default_factory=list)

    @property
    def is_literature(self) -> bool:
        return self.kind in ("work", "chapter", "toc")


def classify_page(html: str, url: str, *, meta: dict | None = None,
                  title: str | None = None) -> PageClass:
    """Decide what KIND of page this is so the crawler can be smart, not blind.

    - "chapter": an individual chapter page (its parent work is what we catalog).
    - "work":    a single work's landing/TOC page (the thing we want to catalog).
    - "toc":     a page enumerating many chapter links but not obviously a landing page.
    - "listing": a browse/genre/ranking page (crawl it to FIND works, don't catalog).
    - "other":   everything else (account pages, legal, home — low value).

    ``meta``/``title`` let a caller pass already-extracted values (e.g. when reconciling from a
    stored page whose *sanitized* HTML no longer has a ``<head>``, so og: tags can't be re-read);
    when omitted they're extracted from ``html`` exactly as before."""
    title = title if title is not None else (og_title(html) or "")
    path = urlparse(url).path.rstrip("/")
    meta = meta if meta is not None else page_metadata(html, url)
    synopsis = meta.get("description") or ""
    og_type = (meta.get("type") or "").lower()

    links = find_chapter_links(html, url)
    if path:
        own = [u for (u, _t) in links if urlparse(u).path.startswith(path + "/")]
    else:
        own = [u for (u, _t) in links]
    listed = len(own) if own else len(links)
    # Chapter count = the HIGHEST chapter/episode number referenced (e.g. "Chapter 1532"),
    # which beats counting links: TOCs are paginated/JS-loaded and webtoons only show the
    # latest ~10 episodes, so the top number is the real total. Fall back to a stated total.
    adv = max(advertised_chapter_count(html) or 0, highest_chapter_number(html, url) or 0) or None

    # Binary/encoding garbage (e.g. a Kindle/MOBI blob fetched as text) is never a work.
    if looks_garbled(title):
        return PageClass("other", 0.0, title, None, adv, listed, ["garbled-title"])

    # The site root / homepage is a directory of works, never a work itself — even when it
    # advertises a chapter count and links to chapters. Treat it as a listing to crawl.
    if not path:
        return PageClass("listing", float(listed), title, None, adv, listed, ["site-root"])

    # Chapter pages: a numeric chapter URL, or an og:title that reads "Chapter N: …".
    if is_chapter_url(url) or chapter_title_from(title):
        return PageClass(
            "chapter", 1.0, title, work_url_for(url), adv, listed, ["chapter-url-or-title"]
        )

    # A work's sub-page (reviews/comments/stats/…) inherits the work's og:type+synopsis
    # and would otherwise clear the work bar — skip it so it can't spawn a duplicate entry.
    if _WORK_SUBPAGE_RE.search(path):
        return PageClass("other", 0.0, title, None, adv, listed, ["work-subpage"])

    # Use the query-aware helpers so webtoon-style ?title_no works pages aren't mistaken
    # for "listing" pages just because their path contains the word "list".
    work_path = is_work_url(url)
    listing = bool(_LISTING_PATH_RE.search(path)) and not work_path
    junk = bool(_JUNK_PATH_RE.search(path))

    signals: list[str] = []
    score = 0.0
    if work_path:
        score += 2.0
        signals.append("work-path")
    if og_type in ("book", "novel", "books.book", "video.tv_show") or _COMIC_HINT.search(og_type):
        score += 2.0
        signals.append(f"og:type={og_type}")
    if adv:
        score += 1.5
        signals.append(f"advertised={adv}")
    if listed >= 3 and not listing:
        score += 1.5
        signals.append(f"chapter-links={listed}")
    if synopsis and len(synopsis) >= 40:
        score += 1.0
        signals.append("has-synopsis")

    # A browse/genre/ranking page: valuable to crawl (it points at works) but is not
    # itself a work — unless its path ALSO matches a single-work slug.
    if listing and not work_path:
        return PageClass("listing", float(listed), title, None, adv, listed, ["listing-path"])
    if junk and not work_path:
        return PageClass("other", 0.0, title, None, adv, listed, ["junk-path"])
    # A bare site name / generic chrome title ("Project Gutenberg", "Home", "Search") must
    # never become a catalog work, even if it otherwise scores like one — so the site's own
    # name doesn't show up as a discovered title.
    if _is_site_name_title(title, meta.get("site_name")):
        return PageClass("other", score, title, None, adv, listed, signals + ["site-name-title"])
    if score >= 3.0:
        return PageClass(
            "work", score, work_title_from(title) or title, work_url_for(url), adv, listed, signals
        )
    # Enumerates many chapter links but didn't clear the work bar → treat as a TOC.
    if listed >= 5:
        return PageClass(
            "toc", float(listed), work_title_from(title) or title, work_url_for(url),
            adv, listed, signals + ["many-chapter-links"],
        )
    return PageClass("other", score, title, None, adv, listed, signals)
