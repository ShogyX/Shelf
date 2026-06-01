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
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

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


def chapter_number(url_or_text: str) -> float | None:
    """Best-effort numeric chapter index from a URL or label."""
    m = re.search(r"chapter[\s._-]*(\d+(?:\.\d+)?)", url_or_text, re.I)
    if m:
        return float(m.group(1))
    m = _NUM_RE.search(url_or_text)
    return float(m.group(1)) if m else None


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
    """For numeric chapter URLs (…/chapter/5, …/chapter-5), return the URL with the
    chapter number incremented. Used when next-links are JS-rendered (no <a href>)."""
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


def is_chapter_url(url: str) -> bool:
    return bool(_NUMERIC_CHAPTER.match(url.split("#", 1)[0].split("?", 1)[0]))


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
    author = _meta_content(
        soup,
        'meta[name="author"]',
        'meta[property="article:author"]',
        'meta[property="book:author"]',
        'meta[name="twitter:creator"]',
    )
    site_name = _meta_content(soup, 'meta[property="og:site_name"]')
    page_type = _meta_content(soup, 'meta[property="og:type"]')
    lang = None
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        lang = html_tag["lang"].strip()[:16] or None
    return {
        "description": (description or "").strip()[:1000] or None,
        "author": (author or "").strip()[:255] or None,
        "cover_url": og_image(html, base_url),
        "site_name": (site_name or "").strip()[:255] or None,
        "type": (page_type or "").strip()[:64] or None,
        "language": lang,
    }


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
    for marker in (" Chapter ", " chapter ", " - ", " | ", " — ", ":"):
        i = title.find(marker)
        if 0 < i < cut:
            cut = i
    out = title[:cut].strip(" -|—:") or title
    # Common trailing site noise on novel pages.
    out = re.sub(r"\s+(Novel|Light Novel|Web Novel)$", "", out, flags=re.I).strip()
    return out or title


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


def extract_main_content(html: str, base_url: str = "") -> tuple[str, str]:
    """Return (title, clean_html) for the main article body of a chapter page."""
    soup = BeautifulSoup(html, "lxml")

    title = ""
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
    h1 = soup.find(["h1", "h2"])
    if h1:
        title = h1.get_text(" ", strip=True) or title

    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()

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
    # paragraphs — reconstruct <p> blocks so it reads well and is trackable.
    if len(best.find_all("p")) >= 2:
        return title or "Chapter", best.decode_contents()
    return title or "Chapter", _reconstruct_paragraphs(best)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
# e.g. /novel/library-of-heavens-path, /book/foo, /series/bar, /comic/baz.
_WORK_PATH_RE = re.compile(
    r"/(?:novel|novels|book|books|series|title|titles|story|stories|"
    r"manga|manhua|manhwa|comic|comics|webnovel|web-novel|ln|read)/"
    r"[^/]+/?$",
    re.I,
)
# Pages that *list* many works (good to crawl, but not themselves a work).
_LISTING_PATH_RE = re.compile(
    r"/(?:browse|latest|popular|trending|ranking|rankings|rank|top|genre|genres|"
    r"tag|tags|search|list|lists|category|categories|catalog|catalogue|completed|"
    r"ongoing|all|library|index|directory|az|a-z|novels|books|series|comics|manga)\b",
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
    Returns the cleaned URL unchanged when there's nothing chapter-ish to strip."""
    clean = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    stripped = re.sub(r"/chapters?(?:[/_-].*)?$", "", clean, flags=re.I)
    stripped = re.sub(
        r"/(?:chapter|chap|ch|episode|ep|vol|volume|part)[/_-]?\d.*$", "", stripped, flags=re.I
    )
    return stripped or clean


def is_work_url(url: str) -> bool:
    """True when the URL path looks like a single work's landing page."""
    path = urlparse(url).path
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


def norm_title(title: str) -> str:
    """A normalized key for grouping the SAME work discovered on different sites.

    Lowercase, drop apostrophes, strip medium/qualifier words and volume markers,
    collapse to alnum tokens. 'Library of Heaven's Path (Novel)' and
    'library of heavens path - web novel' both -> 'library of heavens path'."""
    t = (title or "").lower()
    t = t.replace("’", "").replace("‘", "").replace("'", "")
    t = re.sub(
        r"\b(?:the|a|an|light\s+novel|web\s+novel|novel|wn|ln|manga|manhua|manhwa|"
        r"comic|webtoon|raw|raws|english|official|complete|completed|ongoing)\b",
        " ",
        t,
    )
    t = re.sub(r"\b(?:vol(?:ume)?|book|part|season|s)\.?\s*\d+\b", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


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


def classify_page(html: str, url: str) -> PageClass:
    """Decide what KIND of page this is so the crawler can be smart, not blind.

    - "chapter": an individual chapter page (its parent work is what we catalog).
    - "work":    a single work's landing/TOC page (the thing we want to catalog).
    - "toc":     a page enumerating many chapter links but not obviously a landing page.
    - "listing": a browse/genre/ranking page (crawl it to FIND works, don't catalog).
    - "other":   everything else (account pages, legal, home — low value)."""
    title = og_title(html) or ""
    path = urlparse(url).path.rstrip("/")
    adv = advertised_chapter_count(html)
    meta = page_metadata(html, url)
    synopsis = meta.get("description") or ""
    og_type = (meta.get("type") or "").lower()

    links = find_chapter_links(html, url)
    if path:
        own = [u for (u, _t) in links if urlparse(u).path.startswith(path + "/")]
    else:
        own = [u for (u, _t) in links]
    listed = len(own) if own else len(links)

    # Chapter pages: a numeric chapter URL, or an og:title that reads "Chapter N: …".
    if is_chapter_url(url) or chapter_title_from(title):
        return PageClass(
            "chapter", 1.0, title, work_url_for(url), adv, listed, ["chapter-url-or-title"]
        )

    listing = bool(_LISTING_PATH_RE.search(path))
    junk = bool(_JUNK_PATH_RE.search(path))
    work_path = bool(_WORK_PATH_RE.search(path))

    signals: list[str] = []
    score = 0.0
    if work_path:
        score += 2.0
        signals.append("work-path")
    if og_type in ("book", "novel", "books.book", "video.tv_show"):
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
